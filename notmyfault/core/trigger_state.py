from __future__ import annotations

import json
from notmyfault.core.compiled_rules import freeze
from notmyfault.core.value_codec import encode_value


class StateDetector:
    def __init__(self, meta, config, state):
        self.meta, self.config, self.state = meta, config, state
        self.baselines = state.get("baselines", {})
        self.pending = {}

    def parameter(self, name):
        if name in self.config:
            return self.config[name]
        return next((item.get("default") for item in self.meta.get("params", []) if item.get("name") == name), None)

    def _event(self, payload, previous):
        output = self.meta.get("previous_state_output")
        if output and previous is not None:
            payload = freeze({**payload, output: previous})
        filters = self.meta.get("state_filter", {})
        selected = filters.get("cases", {}).get(self.parameter(filters.get("param")), filters.get("default", {}))
        for key, expected in selected.items():
            if isinstance(expected, dict):
                expected = self.parameter(expected["config"])
            if payload.get(key) != expected:
                return []
        return [payload]

    def _commit(self, partition, value, payload):
        previous = self.baselines.get(partition, self.meta.get("state_initial_value"))
        updated = {**self.baselines, partition: value}
        self.state.set("baselines", updated)
        self.baselines = updated
        return self._event(payload, previous)

    def offer(self, payload, now):
        key = self.meta["state_key"]
        partition_key = self.meta.get("state_partition_key")
        partition = json.dumps(encode_value(payload.get(partition_key)), sort_keys=True) if partition_key else ""
        value = payload[key]
        if value in self.meta.get("state_untracked", []):
            return self._event(payload, None)
        initial = partition not in self.baselines
        previous = self.baselines.get(partition, self.meta.get("state_initial_value"))
        hysteresis = self.meta.get("state_hysteresis")
        if hysteresis and all(self.parameter(name) == expected for name, expected in hysteresis.get("when", {}).items()):
            actual = payload[hysteresis["value"]]
            threshold = hysteresis.get("threshold", self.parameter(hysteresis.get("threshold_param")))
            direction = hysteresis.get("direction", self.parameter(hysteresis.get("direction_param")))
            active = hysteresis.get("active_value", True)
            margin = hysteresis["margin"] if previous == active else 0
            hit = actual <= threshold + margin if direction == "below" else actual >= threshold - margin
            hit &= all(payload.get(name) == expected for name, expected in hysteresis.get("guard", {}).items())
            value = active if hit else (payload[key] if "active_value" in hysteresis else False)
            payload = freeze({**payload, key: value})
        if initial and "state_initial_value" not in self.meta:
            self._commit(partition, value, payload)
            return self._event(payload, None) if self.meta.get("state_mode") == "level" else []
        if value == previous:
            self.pending.pop(partition, None)
            return self._event(payload, previous) if self.meta.get("state_mode") == "level" else []
        delay = self.meta.get("debounce_seconds", 0)
        if delay:
            pending = self.pending.get(partition)
            deadline = pending[0] if pending is not None and pending[1] == value else now + delay
            self.pending[partition] = (deadline, value, payload)
            return []
        return self._commit(partition, value, payload)

    def due(self, now):
        results = []
        for partition, (deadline, value, payload) in list(self.pending.items()):
            if deadline <= now:
                results.extend(self._commit(partition, value, payload))
                self.pending.pop(partition)
        return results

    @property
    def deadline(self):
        return min((item[0] for item in self.pending.values()), default=None)
