from __future__ import annotations

import copy
import heapq
import itertools
import math
import threading
from dataclasses import dataclass, field
from typing import Any

from notmyfault.core.clock import Clock, SystemClock
from notmyfault.core.condition_program import DEFAULT_TTL_SECONDS, build_matcher
from notmyfault.core.conditions import (
    _condition_children, _condition_op, _is_event_leaf, check_event_params,
    config_fingerprint, get_rule_condition,
)


@dataclass
class Hit:
    event: dict[str, Any]
    sequence: int
    expires_at: float


@dataclass
class AbsenceState:
    deadline: float
    token: int
    emitted: bool = False


@dataclass
class MatcherState:
    seen: dict[str, Hit] = field(default_factory=dict)
    absences: dict[str, AbsenceState] = field(default_factory=dict)
    fired: tuple[tuple[str, int], ...] = ()
    last_match: list[dict] = field(default_factory=list)
    consume: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Evaluation:
    signature: tuple[tuple[str, int], ...] = ()
    marks: tuple[float, ...] = ()
    consume: frozenset[str] = frozenset()
    retain: frozenset[str] = frozenset()


class ConditionRuntime:
    def __init__(self, *, clock: Clock | None = None, default_ttl: float = DEFAULT_TTL_SECONDS):
        if not math.isfinite(default_ttl) or default_ttl <= 0:
            raise ValueError("条件保留时长必须是大于零的有限数字")
        self._clock = clock or SystemClock()
        self._default_ttl = default_ttl
        self._lock = threading.RLock()
        self._programs = {}
        self._states: dict[str, MatcherState] = {}
        self._heap = []
        self._heap_limit = 64
        self._sequence = itertools.count(1)
        self._active = False
        self._wake = lambda: None

    @property
    def clock(self):
        return self._clock

    def set_waker(self, wake):
        self._wake = wake

    def configure(self, rules):
        with self._lock:
            self._programs = {key: rule.matcher_spec for key, rule in rules.items()}
            self._heap_limit = 64 + 2 * sum(len(program.retention) + len(program.absences) for program in self._programs.values())

    def register_rule(self, rule_key, rule, now=None):
        with self._lock:
            if rule_key not in self._programs:
                self._programs[rule_key] = build_matcher(get_rule_condition(rule), self._default_ttl)
                program = self._programs[rule_key]
                self._heap_limit += 2 * (len(program.retention) + len(program.absences))
            self._arm_absences(rule_key, self._timestamp(now))
        self._wake()

    def activate(self, now=None):
        with self._lock:
            if self._active:
                return
            timestamp = self._timestamp(now)
            for key, program in self._programs.items():
                if program.absences:
                    self._arm_absences(key, timestamp)
            self._active = True

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._states)

    def migrate_from(self, previous, unchanged):
        states = previous.snapshot()
        with self._lock:
            self._states = {key: value for key, value in states.items() if key in unchanged}
            self._clock = previous.clock
            self._heap.clear()
            largest = 0
            for key, state in self._states.items():
                largest = max(largest, *(sequence for _name, sequence in state.fired), 0)
                for name, hit in state.seen.items():
                    largest = max(largest, hit.sequence)
                    self._push(hit.expires_at, key, "expiry", name, hit.sequence)
                for name, absence in state.absences.items():
                    largest = max(largest, absence.token)
                    if not absence.emitted:
                        self._push(absence.deadline, key, "absence", name, absence.token)
            self._sequence = itertools.count(largest + 1)

    def reset(self):
        with self._lock:
            self._states.clear()
            self._heap.clear()
            self._active = False
        self._wake()

    def _timestamp(self, now):
        return self._clock.monotonic() if now is None else now

    @staticmethod
    def _future(timestamp, seconds):
        return max(timestamp + seconds, math.nextafter(timestamp, math.inf))

    def _arm_absences(self, rule_key, timestamp):
        program = self._programs[rule_key]
        if not program.absences:
            return
        state = self._states.setdefault(rule_key, MatcherState())
        for node in program.absences:
            name = "not:" + program.keys[id(_condition_children(node)[0])]
            if name not in state.absences:
                self._rearm(rule_key, name, self._future(timestamp, float(node["within_seconds"])))

    def _rearm(self, rule_key, name, deadline):
        token = next(self._sequence)
        self._states[rule_key].absences[name] = AbsenceState(deadline, token)
        self._push(deadline, rule_key, "absence", name, token)

    def _push(self, deadline, rule_key, kind, name, token):
        heapq.heappush(self._heap, (deadline, token, rule_key, kind, name, token))

    def _valid(self, item):
        deadline, _order, key, kind, name, token = item
        state = self._states.get(key)
        if state is None:
            return False
        entry = state.seen.get(name) if kind == "expiry" else state.absences.get(name)
        return entry is not None and (entry.sequence == token if kind == "expiry" else
                                      entry.token == token and not entry.emitted and entry.deadline == deadline)

    def _compact(self):
        if len(self._heap) > self._heap_limit:
            self._heap = [item for item in self._heap if self._valid(item)]
            heapq.heapify(self._heap)

    def next_deadline(self):
        with self._lock:
            while self._heap and not self._valid(self._heap[0]):
                heapq.heappop(self._heap)
            return self._heap[0][0] if self._heap else None

    def _hit(self, rule_key, name, event, timestamp):
        retention = self._programs[rule_key].retention[name]
        deadline = timestamp + retention.seconds
        if retention.inclusive:
            deadline = math.nextafter(deadline, math.inf)
        hit = Hit(event, next(self._sequence), deadline)
        self._states.setdefault(rule_key, MatcherState()).seen[name] = hit
        self._push(deadline, rule_key, "expiry", name, hit.sequence)

    def take_due(self, now=None):
        timestamp = self._timestamp(now)
        with self._lock:
            self.activate(timestamp)
            affected = set()
            while self._heap and self._heap[0][0] <= timestamp:
                item = heapq.heappop(self._heap)
                if not self._valid(item):
                    continue
                deadline, _order, key, kind, name, _token = item
                state = self._states[key]
                if kind == "expiry":
                    state.seen.pop(name)
                    continue
                program = self._programs[key]
                node = next(node for node in program.absences if "not:" + program.keys[id(_condition_children(node)[0])] == name)
                state.absences[name].emitted = True
                seconds = float(node["within_seconds"])
                self._hit(key, name, {"event": {"type": "absence", "params": {}},
                                     "payload": {"wait_seconds": seconds}, "timestamp": timestamp}, timestamp)
                if node.get("repeat", "once_until_seen") == "every_window":
                    remaining = seconds - math.fmod(timestamp - deadline, seconds)
                    self._rearm(key, name, self._future(timestamp, remaining))
                affected.add(key)
            result = []
            for key in sorted(affected):
                if self._record_match(key):
                    result.append((key, self.take_last_match(key)))
            self._compact()
            return result

    def process_event(self, rules, event_type, event_payload, *, now=None, instance=None):
        with self._lock:
            timestamp = self._timestamp(now)
            due = self.take_due(timestamp)
            matches = []
            for key, rule in rules:
                events = self.match_and_take(key, rule, event_type, event_payload, now=timestamp, instance=instance)
                if events is not None:
                    matches.append((key, events))
            return due, matches

    def last_match(self, rule_key):
        with self._lock:
            state = self._states.get(rule_key)
            return [{**entry, "event": dict(entry["event"]), "payload": dict(entry["payload"])}
                    for entry in state.last_match] if state is not None else []

    def take_last_match(self, rule_key):
        with self._lock:
            result = self.last_match(rule_key)
            state = self._states.get(rule_key)
            if state is not None:
                for name in state.consume:
                    state.seen.pop(name, None)
                state.last_match = []
                state.consume = frozenset()
            return result

    def match_and_take(self, rule_key, rule, event_type, event_payload, now=None, instance=None):
        with self._lock:
            if not self.match(rule_key, rule, event_type, event_payload, now=now, instance=instance):
                return None
            return self.take_last_match(rule_key)

    def match(self, rule_key, rule, event_type, event_payload, now=None, instance=None):
        timestamp = self._timestamp(now)
        with self._lock:
            if rule_key not in self._programs:
                self._programs[rule_key] = build_matcher(get_rule_condition(rule), self._default_ttl)
                program = self._programs[rule_key]
                self._heap_limit += 2 * (len(program.retention) + len(program.absences))
            program = self._programs[rule_key]
            fingerprint = (instance.get("fingerprint") or config_fingerprint(instance.get("config"))) if instance is not None else None
            leaves = [leaf for leaf in program.leaves.get(event_type, ()) if
                      (program.fingerprints[id(leaf)] == fingerprint if instance is not None else check_event_params(leaf, event_payload))]
            if not leaves:
                return False
            self._arm_absences(rule_key, timestamp)
            state = self._states.setdefault(rule_key, MatcherState())
            for name, hit in list(state.seen.items()):
                if hit.expires_at <= timestamp:
                    state.seen.pop(name)
            names = {program.keys[id(leaf)] for leaf in leaves}
            for node in program.absences:
                name = program.keys[id(_condition_children(node)[0])]
                if name in names:
                    self._rearm(rule_key, "not:" + name, self._future(timestamp, float(node["within_seconds"])))
                    state.seen.pop("not:" + name, None)
            for leaf in leaves:
                name = program.keys[id(leaf)]
                if name in program.positive_keys:
                    self._hit(rule_key, name, {"binding_id": leaf.get("binding_id"),
                                              "event": {"type": leaf["type"], "params": dict(leaf.get("params", {}))},
                                              "timestamp": timestamp, "payload": dict(event_payload)}, timestamp)
            matched = self._record_match(rule_key)
            self._compact()
        self._wake()
        return matched

    def _record_match(self, rule_key):
        program, state = self._programs[rule_key], self._states[rule_key]
        result = self._evaluate(program.node, program, state.seen)
        if result is None or result.signature == state.fired:
            return False
        state.fired = result.signature
        state.last_match = [dict(state.seen[name].event) for name, _sequence in result.signature]
        state.consume = result.consume
        return True

    def _evaluate(self, node, program, seen):
        if node is None:
            return None
        if _is_event_leaf(node) or _condition_op(node) == "not":
            name = program.keys[id(node)] if _is_event_leaf(node) else "not:" + program.keys[id(_condition_children(node)[0])]
            hit = seen.get(name)
            return Evaluation(((name, hit.sequence),), (hit.event["timestamp"],), frozenset({name})) if hit else None
        children = _condition_children(node)
        results = [self._evaluate(child, program, seen) for child in children]
        if _condition_op(node) == "any":
            candidates = [item for item in results if item is not None]
            if not candidates:
                return None
            if node.get("pick", "latest") == "first":
                return candidates[0]
            return max(candidates, key=lambda item: (max(item.marks), max(sequence for _name, sequence in item.signature)))
        if not results or any(item is None for item in results):
            return None
        marks = tuple(mark for item in results for mark in item.marks)
        window = node.get("within_seconds")
        if window is not None and max(marks) - min(marks) > float(window):
            return None
        signature = tuple(sorted({part for item in results for part in item.signature}))
        retain = frozenset(name for item in results for name in item.retain)
        consume = frozenset(name for item in results for name in item.consume)
        policy = node.get("consume", "matched")
        if policy == "none":
            return Evaluation(signature, marks, frozenset(), program.subtree_keys[id(node)])
        if policy == "all":
            consume = program.subtree_keys[id(node)] - retain
        return Evaluation(signature, marks, consume, retain)
