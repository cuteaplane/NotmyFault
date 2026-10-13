from __future__ import annotations

import threading

from notmyfault.core.clock import SystemClock
from notmyfault.core.conditions import check_event_params, config_fingerprint
from notmyfault.core.condition_runtime import ConditionRuntime
from notmyfault.core.workflow_graph import get_all_rule_events
from notmyfault.values import hold_value


def has_event_predicate(node):
    if not isinstance(node, dict):
        return False
    children = node.get('children')
    return node.get('op') == 'event_occurred' or (
        isinstance(children, list) and any(has_event_predicate(child) for child in children)
    )


class EventWait:
    def __init__(self, observation, identity, activation, timeout):
        self.observation = observation
        self.identity = identity
        self.rule = {'_matcher': activation}
        self.runtime = ConditionRuntime(clock=observation.clock)
        now = observation.clock.monotonic()
        self.runtime.register_rule(identity, self.rule, now)
        self.deadline = None if timeout is None else now + float(timeout)
        self.ready = False
        self.timed_out = False
        self.matches = []
        self.callback = None

    def arm(self, callback):
        with self.observation._lock:
            self.callback = callback
            ready = self.ready or self.observation._closed
        if ready:
            callback()
        self.observation.wake()

    def check(self, now):
        if self.ready:
            return
        due = self.runtime.take_due(now)
        if due:
            self.matches = due[0][1]
            self.ready = True
        elif self.deadline is not None and now >= self.deadline:
            self.ready = True
            self.timed_out = True

    def next_deadline(self):
        if self.ready:
            return None
        values = [value for value in (self.deadline, self.runtime.next_deadline()) if value is not None]
        return min(values) if values else None

    def close(self):
        with self.observation._lock:
            self.observation._waits.pop(self.identity, None)
            self.callback = None


class RunEventObservation:
    def __init__(self, compiled, occurred=(), *, clock=None, wake=None):
        self.clock = clock or SystemClock()
        self.wake = wake or (lambda: None)
        self._leaves = {}
        self._fingerprints = {}
        for leaf in get_all_rule_events(compiled.source):
            self._leaves.setdefault(leaf['type'], []).append(leaf)
            self._fingerprints[leaf['binding_id']] = config_fingerprint(leaf.get('params', {}))
        identities = set(self._fingerprints)
        self._occurred = set(occurred) & identities
        self._started_at = self.clock.monotonic()
        self._absence_completed = {}
        self._latest = {}
        self._waits = {}
        self._lock = threading.RLock()
        self._closed = False

    def seed(self, events):
        with self._lock:
            for entry in events:
                identity = entry.get('binding_id')
                if identity in self._fingerprints:
                    if entry.get('absence'):
                        self._absence_completed[identity] = float(entry['payload']['wait_seconds'])
                        continue
                    self._occurred.add(identity)
                    self._latest[identity] = hold_value(entry)

    def record(self, event_type, payload, instance=None):
        fingerprint = (instance.get('fingerprint') or config_fingerprint(instance.get('config'))) if instance is not None else None
        callbacks = []
        with self._lock:
            if self._closed:
                return
            now = self.clock.monotonic()
            for leaf in self._leaves.get(event_type, ()):
                matches = self._fingerprints[leaf['binding_id']] == fingerprint if instance is not None else check_event_params(leaf, payload)
                if matches:
                    identity = leaf['binding_id']
                    self._absence_completed.pop(identity, None)
                    self._occurred.add(identity)
                    self._latest[identity] = hold_value({'binding_id': identity, 'event': {'type': event_type, 'params': leaf.get('params', {})}, 'payload': payload, 'timestamp': now})
            for waiter in self._waits.values():
                if waiter.ready:
                    continue
                due, matches = waiter.runtime.process_event(((waiter.identity, waiter.rule),), event_type, payload, now=now, instance=instance)
                if due or matches:
                    waiter.matches = (due or matches)[0][1]
                    waiter.ready = True
                else:
                    waiter.check(now)
                if waiter.ready and waiter.callback is not None:
                    callbacks.append(waiter.callback)
        for callback in callbacks:
            callback()
        self.wake()

    def wait_for(self, identity, activation, timeout=None):
        with self._lock:
            existing = self._waits.get(identity)
            if existing is not None:
                return existing
            waiter = EventWait(self, identity, activation, timeout)
            self._waits[identity] = waiter
            from notmyfault.core.condition_program import build_matcher
            program = build_matcher(activation)
            now = self.clock.monotonic()
            retention_by_id = {leaf['binding_id']: program.retention.get(program.keys[id(leaf)]) for leaves in program.leaves.values() for leaf in leaves}
            for entry in sorted(self._latest.values(), key=lambda event: event['timestamp']):
                retention = retention_by_id.get(entry['binding_id'])
                if retention is not None and (now > entry['timestamp'] + retention.seconds or now == entry['timestamp'] + retention.seconds and not retention.inclusive):
                    continue
                event = entry['event']
                matches = waiter.runtime.match_and_take(identity, waiter.rule, event['type'], entry['payload'], now=entry['timestamp'], instance={'config': event.get('params', {})})
                if matches:
                    waiter.matches = matches
            now = self.clock.monotonic()
            if waiter.matches:
                waiter.ready = True
            waiter.check(now)
        self.wake()
        return waiter

    def process_timers(self, now=None):
        callbacks = []
        with self._lock:
            timestamp = self.clock.monotonic() if now is None else now
            for waiter in self._waits.values():
                was_ready = waiter.ready
                waiter.check(timestamp)
                if not was_ready and waiter.ready and waiter.callback is not None:
                    callbacks.append(waiter.callback)
        for callback in callbacks:
            callback()

    def next_deadline(self):
        with self._lock:
            values = [deadline for waiter in self._waits.values() if (deadline := waiter.next_deadline()) is not None]
            return min(values) if values else None

    def snapshot(self):
        with self._lock:
            return frozenset(self._occurred)

    def absent_for(self, identity, seconds):
        with self._lock:
            if identity not in self._fingerprints:
                raise ValueError('事件未发生判断引用了未知触发器')
            latest = self._latest.get(identity)
            if latest is None and self._absence_completed.get(identity, 0) >= seconds:
                return True
            since = latest['timestamp'] if latest is not None else self._started_at
            return self.clock.monotonic() - since >= seconds

    def data_snapshot(self):
        with self._lock:
            return frozenset(self._occurred), dict(self._latest)

    def apply_snapshot(self, context):
        occurred, latest = self.data_snapshot()
        for identity, entry in latest.items():
            context['triggers'][identity] = {'type': entry['event']['type'], 'payload': entry['payload'],
                'config': context.get('_trigger_configs', {}).get(identity, entry['event'].get('params', {}))}
        return occurred

    def close(self):
        with self._lock:
            self._closed = True
            callbacks = [waiter.callback for waiter in self._waits.values() if waiter.callback is not None]
            self._waits.clear()
        for callback in callbacks:
            callback()
