from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict

from notmyfault.core.run_history import RunHistory


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

    def publish(self, event_type: str, data: Dict[str, Any]) -> None:
        if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(event_type):
            event_type = "message"
        packet = {"type": event_type, "data": data, "ts": time.time()}
        try:
            self._history.record(packet)
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
            self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        with self._lock:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)

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
