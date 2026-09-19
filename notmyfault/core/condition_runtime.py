from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

from notmyfault.core.conditions import (
    get_rule_condition, _absence_nodes, _positive_events, _event_key,
    iter_condition_events, _condition_op, _condition_children, _is_event_leaf,
    check_event_params, config_fingerprint,
)


class ConditionRuntime:
    """维护条件树的命中状态并判断新的组合"""

    def __init__(self) -> None:
        self._seen: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._fired: Dict[str, tuple[tuple[str, float], ...]] = {}
        self._last_matches: Dict[str, List[Dict[str, Any]]] = {}
        self._absence_deadlines: Dict[str, Dict[str, tuple[float, bool]]] = {}
        self._lock = threading.RLock()

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()
            self._fired.clear()
            self._last_matches.clear()
            self._absence_deadlines.clear()

    def poll_absences(self, rule_key, rule, now=None):
        timestamp = time.monotonic() if now is None else now
        node = get_rule_condition(rule)
        with self._lock:
            deadlines = self._absence_deadlines.setdefault(rule_key, {})
            seen = self._seen.setdefault(rule_key, {})
            changed = False
            for absence in _absence_nodes(node):
                children = _condition_children(absence)
                if len(children) != 1 or not _is_event_leaf(children[0]):
                    continue
                seconds = float(absence["within_seconds"])
                key = "not:" + _event_key(children[0])
                deadline, emitted = deadlines.setdefault(key, (timestamp + seconds, False))
                if not emitted and timestamp >= deadline:
                    deadlines[key] = (deadline, True)
                    seen[key] = {
                        "event": {"type": "absence", "params": {}},
                        "payload": {"wait_seconds": seconds}, "timestamp": timestamp,
                    }
                    changed = True
            if changed and self._record_match(rule_key, node, seen):
                return self.take_last_match(rule_key)
            return None

    def last_match(self, rule_key: str) -> List[Dict[str, Any]]:
        """返回最近一次命中的事件供动作执行"""
        with self._lock:
            return [
                {
                    **item,
                    "event": dict(item["event"]),
                    "payload": dict(item["payload"]),
                }
                for item in self._last_matches.get(rule_key, [])
            ]

    def take_last_match(self, rule_key: str) -> List[Dict[str, Any]]:
        with self._lock:
            result = self.last_match(rule_key)
            signature = self._fired.pop(rule_key, ())
            seen = self._seen.get(rule_key, {})
            for key, _timestamp in signature:
                seen.pop(key, None)
            if not seen:
                self._seen.pop(rule_key, None)
            self._last_matches.pop(rule_key, None)
            return result

    def match_and_take(
        self,
        rule_key: str,
        rule: Dict[str, Any],
        event_type: str,
        event_payload: Dict[str, Any],
        now: float | None = None,
        instance: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]] | None:
        with self._lock:
            if not self.match(
                rule_key,
                rule,
                event_type,
                event_payload,
                now=now,
                instance=instance,
            ):
                return None
            return self.take_last_match(rule_key)

    def match(
        self,
        rule_key: str,
        rule: Dict[str, Any],
        event_type: str,
        event_payload: Dict[str, Any],
        now: float | None = None,
        instance: Dict[str, Any] | None = None,
    ) -> bool:
        """记录事件并按 event-v1 或 event-v2 规则判断新的命中组合"""
        node = get_rule_condition(rule)
        if node is None:
            return False
        timestamp = time.monotonic() if now is None else now
        if instance is not None:
            fingerprint = config_fingerprint(instance.get("config"))
            matching_leaves = [
                leaf for leaf in iter_condition_events(node)
                if leaf.get("type") == event_type
                and config_fingerprint(leaf.get("params")) == fingerprint
            ]
        else:
            matching_leaves = [
                leaf for leaf in iter_condition_events(node)
                if leaf.get("type") == event_type and check_event_params(leaf, event_payload)
            ]
        if not matching_leaves:
            return False

        with self._lock:
            seen = self._seen.setdefault(rule_key, {})
            deadlines = self._absence_deadlines.setdefault(rule_key, {})
            matching_keys = {_event_key(leaf) for leaf in matching_leaves}
            for absence in _absence_nodes(node):
                children = _condition_children(absence)
                if len(children) == 1 and _event_key(children[0]) in matching_keys:
                    key = "not:" + _event_key(children[0])
                    deadlines[key] = (timestamp + float(absence["within_seconds"]), False)
                    seen.pop(key, None)
            positive_keys = {_event_key(leaf) for leaf in _positive_events(node)}
            matching_leaves = [leaf for leaf in matching_leaves if _event_key(leaf) in positive_keys]
            if not matching_leaves:
                return False
            fired = self._fired.get(rule_key, ())
            if any(key not in seen for key, _fired_at in fired):
                self._fired.pop(rule_key, None)
            for leaf in matching_leaves:
                seen[_event_key(leaf)] = {
                    "binding_id": leaf.get("binding_id"),
                    "event": {
                        "type": leaf.get("type", ""),
                        "params": dict(leaf.get("params", {})),
                    },
                    "timestamp": timestamp,
                    "payload": dict(event_payload),
                }

            return self._record_match(rule_key, node, seen)

    def _record_match(self, rule_key, node, seen):
        matched, signature = self._evaluate(node, seen)
        if not matched or self._fired.get(rule_key) == signature:
            return False
        self._fired[rule_key] = signature
        self._last_matches[rule_key] = [dict(seen[key]) for key, _timestamp in signature if key in seen]
        return True

    def _evaluate(
        self,
        node: Dict[str, Any],
        seen: Dict[str, Dict[str, Any]],
    ) -> tuple[bool, tuple[tuple[str, float], ...]]:
        if _is_event_leaf(node):
            key = _event_key(node)
            entry = seen.get(key)
            return (
                entry is not None,
                ((key, float(entry["timestamp"])),) if entry else (),
            )

        children = _condition_children(node)
        if not children:
            return False, ()
        if _condition_op(node) == "not":
            key = "not:" + _event_key(children[0])
            entry = seen.get(key)
            return entry is not None, ((key, float(entry["timestamp"])),) if entry else ()
        states = [self._evaluate(child, seen) for child in children]
        op = _condition_op(node)
        if op == "all":
            if not all(ok for ok, _signature in states):
                return False, ()
            signature = tuple(item for _ok, part in states for item in part)
            window = node.get("within_seconds")
            if window not in (None, ""):
                try:
                    limit = float(window)
                except (TypeError, ValueError):
                    return False, ()
                timestamps = [item[1] for item in signature]
                if limit < 0 or (timestamps and max(timestamps) - min(timestamps) > limit):
                    return False, ()
            return True, tuple(sorted(signature))

        # any 条件组选最近命中的分支，缓存顺序不再决定 OR 组合
        matches = [signature for ok, signature in states if ok]
        if matches:
            latest = max(
                matches,
                key=lambda signature: max((item[1] for item in signature), default=float("-inf")),
            )
            return True, latest
        return False, ()

