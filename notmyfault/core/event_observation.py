from __future__ import annotations

import threading

from notmyfault.core.conditions import check_event_params, config_fingerprint


def has_event_predicate(node):
    if not isinstance(node, dict):
        return False
    children = node.get("children")
    return node.get("op") == "event_occurred" or (
        isinstance(children, list) and any(has_event_predicate(child) for child in children)
    )


class RunEventObservation:
    def __init__(self, compiled, occurred=()):
        self._matcher = compiled.matcher_spec
        identities = {leaf.get("binding_id") for leaves in self._matcher.leaves.values() for leaf in leaves}
        self._occurred = set(occurred) & identities
        self._lock = threading.Lock()
        self._closed = False

    def record(self, event_type, payload, instance=None):
        fingerprint = (instance.get("fingerprint") or config_fingerprint(instance.get("config"))) if instance is not None else None
        with self._lock:
            if self._closed:
                return
            for leaf in self._matcher.leaves.get(event_type, ()):
                matches = self._matcher.fingerprints[id(leaf)] == fingerprint if instance is not None else check_event_params(leaf, payload)
                if matches:
                    self._occurred.add(leaf["binding_id"])

    def snapshot(self):
        with self._lock:
            return frozenset(self._occurred)

    def close(self):
        with self._lock:
            self._closed = True
