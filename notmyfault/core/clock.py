from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    """规则时钟可以暂停，线程退出和文件刷新使用真实时间计时"""

    def monotonic(self) -> float: ...

    def time(self) -> float: ...

    def wait(self, event: threading.Event, timeout: float | None) -> bool: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def wait(self, event: threading.Event, timeout: float | None) -> bool:
        return event.wait(timeout)


class FakeClock:
    def __init__(self, *, monotonic: float = 0.0, wall_time: float = 0.0) -> None:
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._changed = threading.Condition()

    def monotonic(self) -> float:
        with self._changed:
            return self._monotonic

    def time(self) -> float:
        with self._changed:
            return self._wall_time

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("时钟不能倒退")
        with self._changed:
            self._monotonic += seconds
            self._wall_time += seconds
            self._changed.notify_all()

    def wait(self, event: threading.Event, timeout: float | None) -> bool:
        with self._changed:
            deadline = None if timeout is None else self._monotonic + max(timeout, 0)
            while not event.is_set():
                if deadline is not None and self._monotonic >= deadline:
                    return False
                # Event.set() 不会通知这个 Condition，每隔 10 ms 检查取消请求
                self._changed.wait(0.01)
            return True
