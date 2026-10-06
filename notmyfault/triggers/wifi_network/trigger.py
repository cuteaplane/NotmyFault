"""WiFi 网络变化触发器：连上或断开指定 SSID 时发送事件
Windows 用 WLAN API；Linux 用 nmcli -t -f ACTIVE,SSID dev wifi
"""

import os
import re
import subprocess

from notmyfault.triggers.base import PollingTrigger

def _current_ssid() -> str | None:
    """返回当前 WiFi SSID，未连接返回空字符串，查询失败返回 None"""
    if os.name == "nt":
        return _current_ssid_windows()
    return _current_ssid_linux()


def _current_ssid_windows() -> str | None:
    from notmyfault.platform.windows_wifi import current_ssid

    return current_ssid()


def _current_ssid_linux() -> str | None:
    import shutil
    if not shutil.which("nmcli"):
        return None
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True,
            text=True,
            errors="replace",
            env={**os.environ, "LC_ALL": "C"},
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("yes:"):
            return re.sub(r"\\([\\:])", r"\1", line[4:])
    return ""


class WifiNetworkTrigger(PollingTrigger):
    """WiFi 网络变化触发器，使用 event-v2 轮询"""

    interval: float = 10.0
    native: bool = os.name == "nt"

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
        current = _current_ssid()
        if current is None:
            raise RuntimeError("Wi-Fi 查询失败，请检查无线服务及 WLAN API/nmcli")
        self.emit({"ssid": current, "previous_ssid": current, "connected": bool(current), "target": self.target})


def run(meta, config, emit_event, shutdown_event):
    WifiNetworkTrigger(meta, config, emit_event, shutdown_event).run()
