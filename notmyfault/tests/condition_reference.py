import math

from notmyfault.core.conditions import (
    _absence_nodes, _condition_children, _condition_op, _event_key, _is_event_leaf,
    _positive_events, check_event_params, config_fingerprint, iter_condition_events,
)


class ReferenceConditionRuntime:
    def __init__(self, rule):
        self.node = rule["_matcher"]
        self.records = {}
        self.absences = {}
        self.sequence = 0
        self.schedule_order = 0
        self.fired = ()
        for node in _absence_nodes(self.node):
            self.schedule_order += 1
            self.absences["not:" + _event_key(node["children"][0])] = [float(node["within_seconds"]), False, self.schedule_order]

    def _lifetimes(self, node, limits=()):
        op = _condition_op(node)
        if _is_event_leaf(node) or op == "not":
            key = _event_key(node) if _is_event_leaf(node) else "not:" + _event_key(node["children"][0])
            yield key, min(limits or ((3600, False),), key=lambda item: (item[0], item[1]))
            return
        if op == "all":
            if "within_seconds" in node:
                limits = (*limits, (float(node["within_seconds"]), True))
                if "ttl_seconds" in node:
                    limits = (*limits, (float(node["ttl_seconds"]), False))
            else:
                limits = (*limits, (float(node.get("ttl_seconds", 3600)), False))
        for child in _condition_children(node):
            yield from self._lifetimes(child, limits)

    def _expire(self, now):
        for key, (duration, inclusive) in self._lifetimes(self.node):
            if key not in self.records:
                continue
            deadline = self.records[key]["event"]["timestamp"] + duration
            if now > deadline if inclusive else now >= deadline:
                self.records.pop(key)

    def _record(self, key, event):
        self.sequence += 1
        self.records[key] = {"event": event, "sequence": self.sequence}

    def poll(self, now):
        self._expire(now)
        changed = False
        nodes = sorted(_absence_nodes(self.node), key=lambda node: (
            self.absences["not:" + _event_key(node["children"][0])][0],
            self.absences["not:" + _event_key(node["children"][0])][2],
        ))
        for node in nodes:
            key = "not:" + _event_key(node["children"][0])
            deadline, emitted, order = self.absences[key]
            if emitted or now < deadline:
                continue
            seconds = float(node["within_seconds"])
            self._record(key, {"binding_id": node["children"][0].get("binding_id"), "absence": True, "event": {"type": "absence", "params": {}},
                               "payload": {"wait_seconds": seconds}, "timestamp": now})
            if node.get("repeat", "once_until_seen") == "every_window":
                self.schedule_order += 1
                self.absences[key] = [deadline + (math.floor((now - deadline) / seconds) + 1) * seconds, False, self.schedule_order]
            else:
                self.absences[key] = [deadline, True, order]
            changed = True
        return self._take() if changed else None

    def match(self, kind, payload, now, instance=None):
        leaves = [leaf for leaf in iter_condition_events(self.node) if leaf["type"] == kind and
                  (config_fingerprint(leaf.get("params")) == config_fingerprint(instance["config"])
                   if instance is not None else check_event_params(leaf, payload))]
        if not leaves:
            return None
        self._expire(now)
        names = {_event_key(leaf) for leaf in leaves}
        for node in _absence_nodes(self.node):
            name = _event_key(node["children"][0])
            if name in names:
                self.schedule_order += 1
                self.absences["not:" + name] = [now + float(node["within_seconds"]), False, self.schedule_order]
                self.records.pop("not:" + name, None)
        positives = {_event_key(leaf) for leaf in _positive_events(self.node)}
        for leaf in leaves:
            key = _event_key(leaf)
            if key in positives:
                self._record(key, {"binding_id": leaf.get("binding_id"), "event": {
                    "type": kind, "params": dict(leaf.get("params", {})),
                }, "timestamp": now, "payload": dict(payload)})
        return self._take()

    def _owned(self, node):
        if _is_event_leaf(node):
            return {_event_key(node)}
        if _condition_op(node) == "not":
            return {"not:" + _event_key(node["children"][0])}
        return set().union(*(self._owned(child) for child in _condition_children(node)))

    def _evaluate(self, node):
        if _is_event_leaf(node) or _condition_op(node) == "not":
            key = _event_key(node) if _is_event_leaf(node) else "not:" + _event_key(node["children"][0])
            if key not in self.records:
                return None
            return {"keys": {key}, "marks": [self.records[key]["event"]["timestamp"]],
                    "consume": {key}, "retain": set()}
        results = [self._evaluate(child) for child in _condition_children(node)]
        if _condition_op(node) == "any":
            choices = [result for result in results if result is not None]
            if not choices:
                return None
            if node.get("pick", "latest") == "first":
                return choices[0]
            choices.sort(key=lambda result: (max(result["marks"]), max(self.records[key]["sequence"] for key in result["keys"])), reverse=True)
            return choices[0]
        if not results or None in results:
            return None
        marks = [mark for result in results for mark in result["marks"]]
        if "within_seconds" in node and max(marks) - min(marks) > float(node["within_seconds"]):
            return None
        keys = set().union(*(result["keys"] for result in results))
        retain = set().union(*(result["retain"] for result in results))
        consume = set().union(*(result["consume"] for result in results))
        if node.get("consume", "matched") == "none":
            retain, consume = self._owned(node), set()
        elif node.get("consume") == "all":
            consume = self._owned(node) - retain
        return {"keys": keys, "marks": marks, "consume": consume, "retain": retain}

    def _take(self):
        result = self._evaluate(self.node)
        if result is None:
            return None
        signature = tuple(sorted((key, self.records[key]["sequence"]) for key in result["keys"]))
        if signature == self.fired:
            return None
        self.fired = signature
        events = [self.records[key]["event"] for key in sorted(result["keys"])]
        for key in result["consume"]:
            self.records.pop(key, None)
        return events
