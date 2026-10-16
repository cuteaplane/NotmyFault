import math

from notmyfault.core.conditions import check_event_params, config_fingerprint


class ReferenceConditionRuntime:
    def __init__(self, rule):
        graph = rule['workflow']
        self.nodes = {node['binding_id']: node for node in graph['nodes']}
        self.inputs = {}
        incoming = set()
        sources = set()
        for edge in graph['edges']:
            if edge['channel'] == 'condition':
                self.inputs.setdefault(edge['target'], []).append(edge['source'])
            else:
                incoming.add(edge['target'])
                sources.add(edge['source'])
        self.root = next(identity for identity in sorted(sources) if identity not in incoming)
        self.leaves = [node for node in graph['nodes'] if node['type'] == 'trigger']
        self.absent = [node for node in self.leaves if node.get('occurrence') == 'absent']
        self.records = {}
        self.absences = {}
        self.sequence = 0
        self.schedule_order = 0
        self.fired = ()
        for node in self.absent:
            self.schedule_order += 1
            self.absences['absence:' + node['binding_id']] = [float(node['duration_seconds']), False, self.schedule_order]

    def _key(self, node):
        return 'absence:' + node['binding_id'] if node.get('occurrence') == 'absent' else 'id:' + node['trigger']['binding_id']

    def _lifetimes(self, identity, limits=()):
        node = self.nodes[identity]
        if node['type'] == 'trigger':
            yield self._key(node), min(limits or ((3600, False),), key=lambda item: (item[0], item[1]))
            return
        if node.get('op') == 'all':
            if 'within_seconds' in node:
                limits = (*limits, (float(node['within_seconds']), True))
            if 'ttl_seconds' in node or 'within_seconds' not in node:
                limits = (*limits, (float(node.get('ttl_seconds', 3600)), False))
        for child in self.inputs.get(identity, ()):
            yield from self._lifetimes(child, limits)

    def _expire(self, now):
        for key, (duration, inclusive) in self._lifetimes(self.root):
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
        nodes = sorted(self.absent, key=lambda node: (
            self.absences['absence:' + node['binding_id']][0],
            self.absences['absence:' + node['binding_id']][2],
        ))
        for node in nodes:
            key = 'absence:' + node['binding_id']
            deadline, emitted, order = self.absences[key]
            if emitted or now < deadline:
                continue
            seconds = float(node['duration_seconds'])
            self._record(key, {"binding_id": node['trigger']['binding_id'], "absence": True, "event": {"type": "absence", "params": {}},
                               "payload": {"wait_seconds": seconds}, "timestamp": now})
            if node.get("repeat", "once_until_seen") == "every_window":
                self.schedule_order += 1
                self.absences[key] = [deadline + (math.floor((now - deadline) / seconds) + 1) * seconds, False, self.schedule_order]
            else:
                self.absences[key] = [deadline, True, order]
            changed = True
        return self._take() if changed else None

    def match(self, kind, payload, now, instance=None):
        leaves = [leaf for leaf in self.leaves if leaf['trigger']['type'] == kind and
                  (config_fingerprint(leaf['trigger'].get('params')) == config_fingerprint(instance["config"])
                   if instance is not None else check_event_params(leaf['trigger'], payload))]
        if not leaves:
            return None
        self._expire(now)
        names = {'id:' + leaf['trigger']['binding_id'] for leaf in leaves}
        for node in self.absent:
            name = 'id:' + node['trigger']['binding_id']
            if name in names:
                self.schedule_order += 1
                self.absences['absence:' + node['binding_id']] = [now + float(node['duration_seconds']), False, self.schedule_order]
                self.records.pop('absence:' + node['binding_id'], None)
        positives = {self._key(leaf) for leaf in (node for node in self.leaves if node.get('occurrence') != 'absent')}
        for leaf in leaves:
            key = self._key(leaf)
            if key in positives:
                self._record(key, {"binding_id": leaf['trigger']['binding_id'], "event": {
                    "type": kind, "params": dict(leaf['trigger'].get('params', {})),
                }, "timestamp": now, "payload": dict(payload)})
        return self._take()

    def _owned(self, identity):
        node = self.nodes[identity]
        if node['type'] == 'trigger':
            return {self._key(node)}
        return set().union(*(self._owned(child) for child in self.inputs.get(identity, ())))

    def _evaluate(self, identity):
        node = self.nodes[identity]
        if node['type'] == 'trigger':
            key = self._key(node)
            if key not in self.records:
                return None
            return {'keys': {key}, 'marks': [self.records[key]['event']['timestamp']],
                    'consume': {key}, 'retain': set()}
        results = [self._evaluate(child) for child in self.inputs.get(identity, ())]
        if node.get('op') == 'any':
            choices = [result for result in results if result is not None]
            if not choices:
                return None
            if node.get('pick', 'latest') == 'first':
                return choices[0]
            choices.sort(key=lambda result: (max(result['marks']), max(self.records[key]['sequence'] for key in result['keys'])), reverse=True)
            return choices[0]
        if not results or None in results:
            return None
        marks = [mark for result in results for mark in result['marks']]
        if 'within_seconds' in node and max(marks) - min(marks) > float(node['within_seconds']):
            return None
        keys = set().union(*(result['keys'] for result in results))
        retain = set().union(*(result['retain'] for result in results))
        consume = set().union(*(result['consume'] for result in results))
        if node.get('consume', 'matched') == 'none':
            retain, consume = self._owned(identity), set()
        elif node.get('consume') == 'all':
            consume = self._owned(identity) - retain
        return {'keys': keys, 'marks': marks, 'consume': consume, 'retain': retain}

    def _take(self):
        result = self._evaluate(self.root)
        if result is None:
            return None
        signature = tuple(sorted((key, self.records[key]["sequence"]) for key in result["keys"]))
        if signature == self.fired:
            return None
        self.fired = signature
        events = [{**self.records[key]["event"], "root_id": self.root} for key in sorted(result["keys"])]
        for key in result["consume"]:
            self.records.pop(key, None)
        return events
