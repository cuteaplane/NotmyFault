from __future__ import annotations

import queue
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable

from notmyfault.core.clock import Clock, SystemClock


class EventType(StrEnum):
    ENGINE_FAILED = "engine.failed"
    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    RUN_SUCCEEDED = "run.succeeded"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_TIMED_OUT = "run.timed_out"
    RUN_DROPPED = "run.dropped"
    RUN_TRIGGERED = "run.triggered"
    RUN_CHAIN_LIMITED = "run.chain_limited"
    STEP_STARTED = "run.step.started"
    STEP_SUCCEEDED = "run.step.succeeded"
    STEP_FAILED = "run.step.failed"
    STEP_SKIPPED = "run.step.skipped"
    STEP_CANCELLED = "run.step.cancelled"
    STEP_TIMED_OUT = "run.step.timed_out"
    ASSERTIONS_COMPLETED = "run.assertions.completed"
    TRIGGER_DISPATCH_FAILED = "trigger.dispatch_failed"
    TRIGGER_STARTED = "trigger.started"
    TRIGGER_STOPPED = "trigger.stopped"
    TRIGGER_CRASHED = "trigger.crashed"
    TRIGGER_BACKOFF = "trigger.backoff"
    TRIGGER_FAILED = "trigger.failed"
    TRIGGER_PAYLOAD_INVALID = "trigger.payload_invalid"
    TRIGGER_EVENT_DROPPED = "trigger.event_dropped"
    TRIGGER_ZOMBIE = "trigger.zombie"
    RULES_APPLIED = "rules.applied"
    RULES_REJECTED = "rules.rejected"
    PLUGIN_LOAD_FAILED = "plugin.load_failed"
    PLUGIN_WORKER_CRASHED = "plugin.worker.crashed"
    PLUGIN_UPDATE_ROLLED_BACK = "plugin.update.rolled_back"
    PLUGIN_UPDATE_ROLLBACK_FAILED = "plugin.update.rollback_failed"


RUN_TERMINAL_EVENTS = frozenset({EventType.RUN_SUCCEEDED, EventType.RUN_FAILED,
                               EventType.RUN_CANCELLED, EventType.RUN_TIMED_OUT, EventType.RUN_DROPPED})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Event:
    v: int
    seq: int
    ts: float
    type: EventType
    run_id: str | None
    rule_id: str | None
    step_id: str | None
    data: Mapping[str, Any]
    error: Mapping[str, str] | None

    def to_dict(self) -> dict[str, Any]:
        return {"v": self.v, "seq": self.seq, "ts": self.ts, "type": self.type.value,
                "run_id": self.run_id, "rule_id": self.rule_id, "step_id": self.step_id,
                "data": _thaw(self.data), "error": _thaw(self.error)}


class Subscription:
    def __init__(self, callback: Callable[[Event], None] | None, capacity: int, overflow: str,
                 on_overflow: Callable[[int], None] | None = None) -> None:
        if capacity < 1 or overflow not in {"disconnect", "drop_oldest", "drop_newest"}:
            raise ValueError("订阅队列大小或丢弃方式无效")
        self.queue: queue.Queue[Event | None] = queue.Queue(capacity)
        self.overflow = overflow
        self.dropped = 0
        self.closed = False
        self._lock = threading.Lock()
        self._thread = None
        self._on_overflow = on_overflow
        self._reported_dropped = 0
        if callback is not None:
            self._thread = threading.Thread(target=self._consume, args=(callback,), name="EventSubscriber", daemon=True)
            self._thread.start()

    def offer(self, event: Event) -> bool:
        with self._lock:
            if self.closed:
                return False
            try:
                self.queue.put_nowait(event)
                return True
            except queue.Full:
                self.dropped += 1
                if self.overflow == "drop_newest":
                    return True
                if self.overflow == "disconnect":
                    self._close_locked()
                    return False
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except queue.Empty:
                    pass
                self.queue.put_nowait(event)
                return True

    def _consume(self, callback: Callable[[Event], None]) -> None:
        while True:
            event = self.queue.get()
            try:
                if event is None:
                    return
                with self._lock:
                    dropped = self.dropped - self._reported_dropped
                    self._reported_dropped = self.dropped
                if dropped and self._on_overflow is not None:
                    try:
                        self._on_overflow(dropped)
                    except Exception as error:
                        from notmyfault.core.logging import engine_error

                        engine_error("event_overflow_callback_failed", error=str(error))
                callback(event)
            except Exception as error:
                from notmyfault.core.logging import engine_error

                engine_error("event_subscriber_failed", error=str(error))
            finally:
                self.queue.task_done()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"capacity": self.queue.maxsize, "queued": self.queue.qsize(),
                    "dropped": self.dropped, "closed": self.closed, "overflow": self.overflow}

    def flush(self, timeout: float = 5.0) -> bool:
        if self._thread is threading.current_thread():
            return self.queue.empty()
        deadline = time.monotonic() + timeout
        with self.queue.all_tasks_done:
            while self.queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.queue.all_tasks_done.wait(remaining)
        return True

    def _close_locked(self) -> None:
        self.closed = True
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except queue.Empty:
                break
        self.queue.put_nowait(None)

    def close(self) -> None:
        with self._lock:
            if not self.closed:
                self._close_locked()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)


class EventStream:
    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._seq = 0
        self._subscribers: set[Subscription] = set()
        self._closed = False

    def subscribe(self, callback: Callable[[Event], None] | None = None, *, capacity: int = 200,
                  overflow: str = "disconnect", on_overflow: Callable[[int], None] | None = None) -> Subscription:
        subscription = Subscription(callback, capacity, overflow, on_overflow)
        with self._lock:
            closed = self._closed
            if not closed:
                self._subscribers.add(subscription)
        if closed:
            subscription.close()
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription)
        subscription.close()

    def publish(self, kind: EventType | str, payload: Mapping[str, Any] | None = None) -> Event | None:
        kind = EventType(kind)
        data = dict(payload or {})
        ids = [data.pop(key, None) or None for key in ("run_id", "rule_id", "step_id")]
        if any(value is not None and not isinstance(value, str) for value in ids):
            raise TypeError("事件 ID 必须是字符串")
        error = data.pop("error", None)
        if error is not None:
            if not isinstance(error, Mapping) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
                raise TypeError("事件错误必须包含 code 和 message")
            error = {key: str(error[key]) for key in ("code", "message", "location") if key in error}
        data = _freeze(data)
        error = _freeze(error)
        with self._lock:
            if self._closed:
                return None
            self._seq += 1
            event = Event(1, self._seq, self._clock.time(), kind, *ids, data, error)
            for subscription in tuple(self._subscribers):
                if not subscription.offer(event):
                    self._subscribers.discard(subscription)
        return event

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._lock:
            subscribers = tuple(self._subscribers)
        return all(subscription.flush(max(0, deadline - time.monotonic())) for subscription in subscribers)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def clear(self) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for subscription in subscribers:
            subscription.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.clear()
