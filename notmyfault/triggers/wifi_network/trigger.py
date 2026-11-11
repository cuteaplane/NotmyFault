"""WiFi 网络变化触发器。"""

from notmyfault.plugin_api import platform
from notmyfault.triggers.base import PollingTrigger


class WifiNetworkTrigger(PollingTrigger):
    """WiFi 网络变化触发器，使用 event-v2 轮询"""

    interval: float = 10.0

    def validate(self) -> None:
        direction = self.config.get("direction", "connected")
        if direction not in ("connected", "disconnected", "any"):
            raise ValueError(
                f"无效的触发方向: {direction!r}（可选: connected/disconnected/any）"
            )
        target = str(self.config.get("ssid", "") or "")
        if direction in ("connected", "disconnected") and not target:
            raise ValueError("方向为 connected/disconnected 时必须填写目标 SSID")
        self.direction = direction
        self.target = target

    def poll(self) -> None:
        current = platform.wifi.current_ssid() or ""
        self.emit({"ssid": current, "previous_ssid": current, "connected": bool(current), "target": self.target})


def run(meta, config, emit_event, shutdown_event):
    WifiNetworkTrigger(meta, config, emit_event, shutdown_event).run()
