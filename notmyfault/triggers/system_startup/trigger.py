"""引擎启动触发器：启动后等待 delay_seconds 发送一次事件
发送事件后继续等待 shutdown_event
"""

from datetime import datetime

from notmyfault.triggers.base import PollingTrigger


class SystemStartupTrigger(PollingTrigger):
    """引擎启动触发器，使用 event-v2 轮询"""

    interval: float = 1.0
    native: bool = False

    def validate(self) -> None:
        try:
            delay = float(self.config.get("delay_seconds", 5))
        except (TypeError, ValueError):
            raise ValueError("delay_seconds 必须是数字") from None
        if not 0 <= delay <= 300:
            raise ValueError(f"delay_seconds 必须在 0-300 之间，实际: {delay}")
        self.delay_seconds = delay
        self._started_at = datetime.now()
        self._fired = False

    def poll(self) -> None:
        if self._fired:
            return
        if (datetime.now() - self._started_at).total_seconds() < self.delay_seconds:
            return
        started_at = self._started_at.strftime("%Y-%m-%d %H:%M:%S")
        self.log(f"引擎已启动，触发开机任务（延迟 {self.delay_seconds}s）")
        self.emit({
            "started_at": started_at,
            "delay_seconds": self.delay_seconds,
        })
        self._fired = True


def run(meta, config, emit_event, shutdown_event):
    SystemStartupTrigger(meta, config, emit_event, shutdown_event).run()
