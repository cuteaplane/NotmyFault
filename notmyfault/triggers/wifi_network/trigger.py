"""WiFi 网络变化触发器：连上或断开指定 SSID 时发送事件
通过 netsh wlan show interfaces 子进程读取当前 SSID，UTF-8 解码失败时尝试 GBK
"""

import os
import re
import subprocess

from notmyfault.triggers.base import PollingTrigger

_SSID_LINE_RE = re.compile(r"^\s*SSID\s*:\s*(.*)$", re.IGNORECASE)


def _decode_netsh(raw: bytes) -> str:
    """netsh 输出编码取决于系统控制台代码页，UTF-8 解码失败时尝试 GBK"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _current_ssid() -> str:
    """返回当前 WiFi SSID，未连接或查询失败时返回空字符串"""
    if os.name != "nt":
        return ""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    text = _decode_netsh(result.stdout)
    for line in text.splitlines():
        match = _SSID_LINE_RE.match(line)
        if match:
            # 取 SSID 行冒号后面的内容，再去掉两端引号
            return match.group(1).strip().strip('"')
    return ""


class WifiNetworkTrigger(PollingTrigger):
    """WiFi 网络变化触发器，使用 event-v2 轮询"""

    interval: float = 10.0
    native: bool = False  # 查询由 netsh 子进程执行

    def validate(self) -> None:
        direction = self.config.get("direction", "connected")
        if direction not in ("connected", "disconnected", "any"):
            raise ValueError(
                f"无效的触发方向: {direction!r}（可选: connected/disconnected/any）"
            )
        target = str(self.config.get("ssid", "") or "").strip()
        if direction in ("connected", "disconnected") and not target:
            raise ValueError("方向为 connected/disconnected 时必须填写目标 SSID")
        self.direction = direction
        self.target = target
        self._last_ssid: str | None = None

    def poll(self) -> None:
        current = _current_ssid()
        if self._last_ssid is None:
            # 首轮只记基线，不发事件
            self._last_ssid = current
            return
        if current == self._last_ssid:
            return
        previous = self._last_ssid
        self._last_ssid = current

        if self.direction == "connected":
            hit = bool(current) and current == self.target
        elif self.direction == "disconnected":
            hit = (not current) and previous == self.target
        else:
            hit = True  # any：任意变化都算命中
        if not hit:
            return

        self.log(f"WiFi 变化: {previous!r} -> {current!r}")
        self.emit({
            "ssid": current,
            "previous_ssid": previous,
            "connected": bool(current),
            "target": self.target,
        })


def run(meta, config, emit_event, shutdown_event):
    WifiNetworkTrigger(meta, config, emit_event, shutdown_event).run()
