"""锁屏状态变化触发器"""

from notmyfault.plugin_api import platform

from notmyfault.triggers.base import PollingTrigger


class SessionLockTrigger(PollingTrigger):
    """锁屏状态触发器，使用 event-v2 轮询"""

    interval: float = 5.0
    native: bool = False

    def validate(self) -> None:
        state = self.config.get("state", "locked")
        if state not in ("locked", "unlocked", "any"):
            raise ValueError(
                f"无效的目标状态: {state!r}（可选: locked/unlocked/any）"
            )
        self.target_state = state

    def poll(self) -> None:
        locked = platform.session.is_locked()
        state = "locked" if locked else "unlocked"
        self.emit({"state": state, "previous_state": state})


def run(meta, config, emit_event, shutdown_event):
    SessionLockTrigger(meta, config, emit_event, shutdown_event).run()
