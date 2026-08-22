"""Linux 桌面能力探测与命令后端"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from notmyfault.platform.backends import (
    BackendMissingError,
    ClipboardBackend,
    default_runner,
)


def require_command(capability: str, *names: str) -> str:
    for name in names:
        if path := shutil.which(name):
            return path
    raise BackendMissingError(
        f"依赖缺失：{capability} 需要 " + " 或 ".join(names)
    )


def command_path(*names: str) -> str | None:
    for name in names:
        if path := shutil.which(name):
            return path
    return None


def desktop_environment() -> str:
    value = ":".join(
        filter(
            None,
            (
                os.environ.get("XDG_CURRENT_DESKTOP", ""),
                os.environ.get("XDG_SESSION_DESKTOP", ""),
                os.environ.get("DESKTOP_SESSION", ""),
            ),
        )
    ).lower()
    if "gnome" in value or "ubuntu" in value:
        return "gnome"
    if "kde" in value or "plasma" in value:
        return "kde"
    return "unknown"


def session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() or (
        "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11"
    )


def run_command(command: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def get_clipboard_text() -> str | None:
    return ClipboardBackend(default_runner).read_text()


def set_clipboard_text(text: str) -> None:
    ClipboardBackend(default_runner).write_text(text)


def get_idle_seconds() -> float:
    if desktop_environment() == "gnome" and (gdbus := command_path("gdbus")):
        result = run_command(
            [
                gdbus,
                "call",
                "--session",
                "--dest",
                "org.gnome.Mutter.IdleMonitor",
                "--object-path",
                "/org/gnome/Mutter/IdleMonitor/Core",
                "--method",
                "org.gnome.Mutter.IdleMonitor.GetIdletime",
            ],
            timeout=3,
        )
        match = re.search(r"uint64\s+(\d+)", result.stdout)
        if result.returncode == 0 and match:
            return int(match.group(1)) / 1000
    if executable := command_path("xprintidle"):
        result = run_command([executable], timeout=3)
        if result.returncode == 0:
            return int(result.stdout.strip()) / 1000
    raise BackendMissingError(
        "依赖缺失：空闲时间检测需要 gdbus（GNOME）或 xprintidle（X11）"
    )


def default_output_path(prefix: str, extension: str) -> Path:
    from datetime import datetime

    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():
        desktop = Path.home() / "Pictures"
    desktop.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return desktop / f"{prefix}_{timestamp}.{extension}"


def capability_report() -> dict[str, object]:
    """诊断页用的平台报告，capabilities 每项带 available / backend / reason / degraded"""
    from notmyfault.platform.capabilities import probe_capabilities

    return {
        "platform": "linux",
        "desktop": desktop_environment(),
        "session_type": session_type(),
        "capabilities": probe_capabilities(),
        "limitations": {
            "global_hotkey": (
                "Wayland 不允许普通应用全局监听按键"
                if session_type() == "wayland"
                else None
            ),
            "window_titles": (
                "Wayland 不允许普通应用枚举其他应用窗口标题"
                if session_type() == "wayland"
                else None
            ),
        },
    }
