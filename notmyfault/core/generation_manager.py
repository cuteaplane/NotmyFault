from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from notmyfault.core.clock import Clock, SystemClock
from notmyfault.core.plugin_runtime import PluginSnapshot
from notmyfault.core.rule_model import normalize_rules
from notmyfault.core.rules import normalize_rule_input
from notmyfault.core.runtime_rules import PreparedRules, prepare_rules, validate_rule_identifiers


class GenerationReferences:
    def __init__(self):
        self._lock = threading.Lock()
        self.count = 1
        self.on_release = None

    def retain(self):
        with self._lock:
            self.count += 1

    def release(self):
        with self._lock:
            self.count -= 1
            if self.count < 0:
                raise RuntimeError("规则版本引用数无效")
            callback = self.on_release if self.count == 0 else None
        if callback is not None:
            callback()


@dataclass(frozen=True, slots=True)
class Generation:
    id: int
    source: str
    created_at: float
    snapshot: PreparedRules
    plugins: PluginSnapshot
    references: GenerationReferences = field(default_factory=GenerationReferences, compare=False)

    def retain(self):
        self.references.retain()

    def release(self):
        self.references.release()


class GenerationManager:
    def __init__(self, plugins, runs, events, *, reconcile: Callable[[Generation], Any] | None = None,
                 clock: Clock | None = None, lock=None, on_change=None) -> None:
        self._plugins = plugins
        self._runs = runs
        self._events = events
        self._reconcile = reconcile
        self._on_change = on_change or (lambda: None)
        self._clock = clock or SystemClock()
        self._apply_lock = threading.RLock()
        self._lock = lock or threading.RLock()
        self._current: Generation | None = None
        self._next_id = 0

    @property
    def current(self) -> Generation | None:
        with self._lock:
            return self._current

    def ensure_initial(self, document):
        with self._apply_lock:
            current = self.current
            return current if current is not None else self.apply(document, "initial", activate=False, validate=False)

    def prepare(self, document, *, validate: bool = True) -> PreparedRules:
        validate_rule_identifiers(document)
        normalized = normalize_rule_input(document) if validate else normalize_rules(document)
        plugins = self._plugins.snapshot(normalized)
        snapshot = prepare_rules(normalized, plugins.triggers_meta, plugins.actions_meta, validate=validate,
                                 resolve_trigger=plugins.triggers_funcs.get if validate else None,
                                 plugin_digests=plugins.digests, clock=self._clock)
        snapshot.plugins = plugins
        return snapshot

    def apply(self, document, source: str, *, activate: bool = True, validate: bool = True) -> Generation:
        with self._apply_lock:
            try:
                snapshot = self.prepare(document, validate=validate)
                return self.apply_prepared(snapshot, source, activate=activate)
            except Exception as error:
                self._events.publish("rules.rejected", {"source": source, "error": {"code": "rules_apply_failed", "message": str(error)}})
                raise

    def apply_prepared(self, snapshot: PreparedRules, source: str, *, activate: bool = True) -> Generation:
        with self._apply_lock:
            current = self.current
            if source == "file" and current is not None and current.snapshot.document_digest == snapshot.document_digest and current.plugins.digests == snapshot.plugins.digests:
                return current
            self._next_id += 1
            candidate = Generation(self._next_id, source, self._clock.time(), snapshot, snapshot.plugins)
            snapshot.generation = candidate
            previous = self.current
            unchanged = {key for key, rule in snapshot.compiled.items()
                         if previous is not None and key in previous.snapshot.compiled and previous.snapshot.compiled[key].digest == rule.digest}
            transition = self._reconcile(candidate) if activate and self._reconcile is not None else None
            try:
                with self._lock:
                    if previous is not None:
                        snapshot.runtime.migrate_from(previous.snapshot.runtime, unchanged)
                    snapshot.runtime.set_waker(self._on_change)
                    if activate:
                        snapshot.runtime.activate()
                    self._runs.activate_generation(candidate)
                    self._current = candidate
            except Exception:
                if transition is not None:
                    transition.rollback()
                raise
            if transition is not None:
                transition.commit()
                candidate.references.on_release = transition.release
            if previous is not None:
                previous.release()
            self._runs.flush_events()
            self._on_change()
            self._events.publish("rules.applied", {"generation": candidate.id, "source": source, "rules": len(snapshot),
                                                  "unchanged": len(unchanged), "issues": snapshot.issues})
            return candidate
