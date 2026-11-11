from __future__ import annotations

import threading

from notmyfault.core.clock import SystemClock
from notmyfault.core.conditions import check_event_params, config_fingerprint
from notmyfault.core.condition_runtime import ConditionRuntime
from notmyfault.core.workflow_graph import get_all_rule_events
from notmyfault.values import hold_value


class EventWait:
    def __init__(self, observation, identity, timeout):
        self.observation = observation
        self.identity = identity
        self.rule = observation.rule
        self.runtime = ConditionRuntime(clock=observation.clock)
        now = observation.clock.monotonic()
        from notmyfault.core.condition_program import build_matcher
        self.program = build_matcher(self.rule, roots=(identity,))
        if not self.program.leaves:
            raise ValueError('等待事件组必须连接触发器')
        absence_since = observation._absence_starts(self.program, now)
        self.runtime.register_rule(identity, self.rule, now, program=self.program,
            absence_since=absence_since)
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
        self.rule = compiled.source
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
        self._absence_evidence = set()
        self._composite_absence_runtime = None
        self._composite_absence_key = '__composite_absence__'
        self._composite_absences = ()
        self._prepare_composite_absences()

    def seed(self, events):
        with self._lock:
            for entry in sorted(events, key=lambda item: item.get('timestamp', self._started_at)):
                absence_node_id = entry.get('absence_node_id')
                if absence_node_id is not None:
                    self._absence_evidence.add(absence_node_id)
                identity = entry.get('binding_id')
                if identity in self._fingerprints:
                    if entry.get('absence'):
                        self._absence_completed[identity] = float(entry['payload']['wait_seconds'])
                        continue
                    self._occurred.add(identity)
                    self._latest[identity] = hold_value(entry)
                    event = entry.get('event', {})
                    self._feed_composite_event(event.get('type'), entry.get('payload', {}),
                        entry.get('timestamp', self._started_at), {'config': event.get('params', {})})

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
            self._record_composite_absences(event_type, payload, now, instance)
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

    def wait_for(self, identity, timeout=None):
        with self._lock:
            existing = self._waits.get(identity)
            if existing is not None:
                return existing
            waiter = EventWait(self, identity, timeout)
            self._waits[identity] = waiter
            program = waiter.program
            now = self.clock.monotonic()
            retention_by_id = {program.graph.nodes[node_id]['trigger']['binding_id']: program.retention.get(program.keys[node_id]) for leaves in program.leaves.values() for node_id in leaves}
            bindings = {program.graph.nodes[node_id]['trigger']['binding_id']
                for leaves in program.leaves.values() for node_id in leaves}
            for entry in sorted(self._latest.values(), key=lambda event: event['timestamp']):
                if entry['binding_id'] not in bindings:
                    continue
                retention = retention_by_id.get(entry['binding_id'])
                if retention is not None and (now > entry['timestamp'] + retention.seconds or now == entry['timestamp'] + retention.seconds and not retention.inclusive):
                    continue
                event = entry['event']
                waiter.runtime.match(identity, waiter.rule, event['type'], entry['payload'],
                    now=entry['timestamp'], instance={'config': event.get('params', {})})
            now = self.clock.monotonic()
            waiter.runtime.take_due(now, consume=False)
            waiter.matches = waiter.runtime.current_match(identity, now, consume=True)
            if waiter.matches:
                waiter.ready = True
            waiter.check(now)
        self.wake()
        return waiter

    def process_timers(self, now=None):
        callbacks = []
        with self._lock:
            timestamp = self.clock.monotonic() if now is None else now
            self._process_composite_absence_timers(timestamp)
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
            if self._composite_absence_runtime is not None:
                deadline = self._composite_absence_runtime.next_deadline()
                if deadline is not None:
                    values.append(deadline)
            return min(values) if values else None

    def capture_wait_matches(self, matches):
        with self._lock:
            self._absence_evidence.update(entry['absence_node_id'] for entry in matches
                if entry.get('absence_node_id') is not None)

    def absence_matched(self, node_id):
        with self._lock:
            if node_id in self._absence_evidence:
                return True
            return self._composite_absence_runtime is not None and self._composite_absence_runtime.current_node_match(
                self._composite_absence_key, node_id, self.clock.monotonic())

    def _prepare_composite_absences(self):
        from notmyfault.core.canvas_workflow import CanvasProgram
        from notmyfault.core.condition_program import build_matcher

        graph = CanvasProgram({'workflow': self.rule['workflow']})
        candidates = []
        for identity, node in graph.nodes.items():
            if node.get('op', node.get('group_type')) != 'not' or 'duration_seconds' not in node:
                continue
            children = graph.dependencies(identity)
            if len(children) == 1 and len(graph.event_dependencies(children[0])) > 1:
                candidates.append(identity)
        if not candidates:
            return
        program = build_matcher(self.rule, roots=tuple(candidates))
        self._composite_absences = program.absences
        self._composite_absence_runtime = ConditionRuntime(clock=self.clock)
        now = self.clock.monotonic()
        self._composite_absence_runtime.register_rule(self._composite_absence_key, self.rule, now,
            program=program, absence_since=self._absence_starts(program, now))

    def _absence_starts(self, program, now):
        starts = {}
        for absence in program.absences:
            trigger_ids = set()
            for root in absence.reset_roots:
                trigger_ids.update(program.graph.event_dependencies(root))
            bindings = [program.graph.nodes[node_id]['trigger']['binding_id'] for node_id in trigger_ids]
            latest_times = [self._latest[identity]['timestamp'] for identity in bindings
                if identity in self._latest]
            if latest_times:
                starts[absence.key] = max(latest_times)
            elif len(bindings) == 1 and self._absence_completed.get(bindings[0], 0) >= absence.seconds:
                starts[absence.key] = now - absence.seconds
            else:
                starts[absence.key] = self._started_at
        return starts

    def _record_composite_absences(self, event_type, payload, now, instance):
        runtime = self._composite_absence_runtime
        if runtime is None:
            return
        due = runtime.take_due(now, consume=False)
        self.capture_wait_matches([entry for _key, events in due for entry in events])
        self._feed_composite_event(event_type, payload, now, instance)

    def _feed_composite_event(self, event_type, payload, now, instance):
        runtime = self._composite_absence_runtime
        if runtime is None or event_type is None:
            return
        previous = {absence.key: runtime.absence_token(self._composite_absence_key, absence.key)
            for absence in self._composite_absences}
        runtime.match(self._composite_absence_key, self.rule, event_type, payload,
            now=now, instance=instance)
        for absence in self._composite_absences:
            token = runtime.absence_token(self._composite_absence_key, absence.key)
            if token != previous[absence.key]:
                self._absence_evidence.discard(absence.node_id)

    def _process_composite_absence_timers(self, timestamp):
        if self._composite_absence_runtime is None:
            return
        due = self._composite_absence_runtime.take_due(timestamp, consume=False)
        self.capture_wait_matches([entry for _key, events in due for entry in events])

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
