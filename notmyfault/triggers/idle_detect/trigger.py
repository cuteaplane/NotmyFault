import math

from notmyfault.triggers.base import PollingTrigger
from notmyfault.plugin_api import platform


class IdleDetectTrigger(PollingTrigger):
    interval = 2.0
    native = False

    def validate(self):
        try:
            self.threshold = float(self.config.get("idle_seconds", 300))
        except (ValueError, TypeError):
            raise ValueError(
                f"无效的空闲阈值配置: {self.config.get('idle_seconds')!r}"
            ) from None
        if not math.isfinite(self.threshold) or self.threshold < 0:
            raise ValueError("空闲阈值必须是非负有限秒数")

    def setup(self):
        self.log(f"开始监视空闲状态，阈值: {self.threshold} 秒")

    def poll(self):
        idle_secs = platform.session.idle_seconds()
        idle = idle_secs >= self.threshold
        self.emit({"state": "idle" if idle else "active", "idle_seconds": idle_secs})


def run(meta, config, emit_event, shutdown_event):
    IdleDetectTrigger(meta, config, emit_event, shutdown_event).run()
