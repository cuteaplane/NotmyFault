"""引擎运行事件的进程内订阅

插件通过 plugin_api.engine_events_api() 订阅。引擎在 _safe_on_event 里 publish。
回调在发布线程同步执行，异常只记日志，不打断其他订阅者。
"""

from __future__ import annotations

import itertools
import sys
import threading
import traceback
from typing import Any, Callable, Dict

EventCallback = Callable[[str, Dict[str, Any]], None]

# 规则跑完相关的稳定事件，第三方插件可以依赖这些名字
RULE_TERMINAL_EVENTS = frozenset(
    {
        "workflow_completed",
        "workflow_failed",
        "run_dropped",
        "run_replaced",
    }
)

_lock = threading.RLock()
_next_token = itertools.count(1)
_subscribers: Dict[int, EventCallback] = {}


def subscribe(callback: EventCallback) -> int:
    if not callable(callback):
        raise TypeError("callback 必须可调用")
    with _lock:
        token = next(_next_token)
        _subscribers[token] = callback
        return token


def unsubscribe(token: int) -> None:
    with _lock:
        _subscribers.pop(token, None)


def publish(event_type: str, data: Dict[str, Any] | None = None) -> None:
    payload = dict(data or {})
    with _lock:
        callbacks = list(_subscribers.values())
    for callback in callbacks:
        try:
            callback(event_type, payload)
        except Exception:
            print(
                f"[EngineEvents] 订阅回调异常 ({event_type}):",
                file=sys.stderr,
            )
            traceback.print_exc(limit=3, file=sys.stderr)


def clear() -> None:
    with _lock:
        _subscribers.clear()


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)
