from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict

from notmyfault.core.run_history import RunHistory
from notmyfault.core.event_stream import Event


_EVENT_TYPE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


@dataclass(frozen=True, slots=True)
class EventSubscription:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue


class EventBroker:
    def __init__(self, history: RunHistory) -> None:
        self._history = history
        self._subscriptions: list[EventSubscription] = []
        self._lock = threading.Lock()
        self._closed = False

    def publish(self, event_type: Event | str, data: Dict[str, Any] | None = None) -> None:
        if isinstance(event_type, Event):
            event = event_type.to_dict()
            history_packet = event
            terminal = {"run.succeeded", "run.recovered", "run.failed", "run.cancelled", "run.timed_out"}
            names = {"engine.failed": "error", "run.triggered": "rule_triggered", "run.queued": "run_queued", "run.dropped": "run_dropped",
                     "run.step.succeeded": "action_executed", "run.step.skipped": "action_skipped",
                     "run.step.failed": "error", "run.step.cancelled": "action_cancelled", "run.step.timed_out": "action_timed_out",
                     "run.assertions.completed": "test_assertions_completed", "run.chain_limited": "run_chain_limited",
                     "trigger.crashed": "trigger_crashed", "trigger.stopped": "trigger_stopped",
                     "trigger.payload_invalid": "trigger_payload_invalid", "plugin.load_failed": "plugin_load_failed",
                     "plugin.worker.crashed": "plugin_worker_crashed", "plugin.update.rolled_back": "plugin_update_rolled_back",
                     "plugin.update.rollback_failed": "plugin_update_rollback_failed"}
            event_type = "workflow_completed" if event["type"] in terminal else names.get(event["type"], event["type"])
            if event["type"] == "run.failed" and event["data"].get("failure_kind") == "binding":
                event_type = "workflow_failed"
            data = {**event["data"], **{key: event[key] for key in ("run_id", "rule_id", "step_id", "error") if event[key] is not None}}
            packet = {"type": event_type, "data": data, "ts": event["ts"]}
        else:
            packet = {"type": event_type, "data": data or {}, "ts": time.time()}
            history_packet = packet
        if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(event_type):
            event_type = "message"
        packet["type"] = event_type
        try:
            self._history.record_async(history_packet)
        except (OSError, TypeError, ValueError):
            pass
        with self._lock:
            subscriptions = list(self._subscriptions)
        for subscription in subscriptions:
            try:
                subscription.loop.call_soon_threadsafe(
                    self._deliver,
                    subscription,
                    packet,
                )
            except RuntimeError:
                self.unsubscribe(subscription)

    def subscribe(self) -> EventSubscription:
        subscription = EventSubscription(
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(maxsize=200),
        )
        with self._lock:
            closed = self._closed
            if not closed:
                self._subscriptions.append(subscription)
        if closed:
            # 关闭后才连上来的客户端也要立刻收到流结束标记，否则服务端等它到超时
            subscription.queue.put_nowait(None)
        return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        with self._lock:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)

    def close(self) -> None:
        """结束所有事件流：uvicorn 要等连接清空，这些流不结束就关不掉服务"""
        with self._lock:
            self._closed = True
            subscriptions = list(self._subscriptions)
            self._subscriptions.clear()
        for subscription in subscriptions:
            try:
                subscription.loop.call_soon_threadsafe(
                    self._deliver,
                    subscription,
                    None,
                )
            except RuntimeError:
                pass

    def _deliver(
        self,
        subscription: EventSubscription,
        packet: Dict[str, Any],
    ) -> None:
        try:
            subscription.queue.put_nowait(packet)
        except asyncio.QueueFull:
            while not subscription.queue.empty():
                subscription.queue.get_nowait()
            subscription.queue.put_nowait(None)
            self.unsubscribe(subscription)
