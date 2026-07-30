"""Linux 桌面能力探测与命令后端。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


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
    if executable := command_path("wl-paste"):
        result = run_command([executable, "--no-newline"], timeout=3)
    elif executable := command_path("xclip"):
        result = run_command([executable, "-selection", "clipboard", "-o"], timeout=3)
    elif executable := command_path("xsel"):
        result = run_command([executable, "--clipboard", "--output"], timeout=3)
    else:
        raise RuntimeError("缺少剪贴板后端：Wayland 请安装 wl-clipboard，X11 请安装 xclip")
    return result.stdout if result.returncode == 0 else None


def set_clipboard_text(text: str) -> None:
    if executable := command_path("wl-copy"):
        command = [executable]
    elif executable := command_path("xclip"):
        command = [executable, "-selection", "clipboard"]
    elif executable := command_path("xsel"):
        command = [executable, "--clipboard", "--input"]
    else:
        raise RuntimeError("缺少剪贴板后端：Wayland 请安装 wl-clipboard，X11 请安装 xclip")
    subprocess.run(
        command,
        input=text,
        text=True,
        timeout=3,
        check=True,
    )


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
    raise RuntimeError("当前桌面没有可用的空闲时间后端")


def default_output_path(prefix: str, extension: str) -> Path:
    from datetime import datetime

    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():
        desktop = Path.home() / "Pictures"
    desktop.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return desktop / f"{prefix}_{timestamp}.{extension}"


def capability_report() -> dict[str, object]:
    """返回当前 Linux 桌面可用后端，供诊断页和 API 展示。"""
    commands = {
        "notification": command_path("notify-send"),
        "clipboard_read": command_path("wl-paste", "xclip", "xsel"),
        "clipboard_write": command_path("wl-copy", "xclip", "xsel"),
        "screenshot": (
            "xdg-desktop-portal"
            if session_type() == "wayland" and command_path("gdbus")
            else command_path("gnome-screenshot", "grim", "spectacle", "import")
        ),
        "brightness": command_path("brightnessctl"),
        "audio": command_path("wpctl", "pactl", "amixer"),
        "bluetooth": command_path("bluetoothctl"),
        "text_to_speech": command_path("spd-say", "espeak"),
        "screen_lock": command_path("loginctl"),
        "powershell": command_path("pwsh", "powershell"),
    }
    capabilities = {
        name: {"available": bool(path), "backend": path}
        for name, path in commands.items()
    }
    capabilities["tray"] = {
        "available": session_type() == "x11",
        "backend": "pystray-xembed" if session_type() == "x11" else None,
    }
    return {
        "platform": "linux",
        "desktop": desktop_environment(),
        "session_type": session_type(),
        "capabilities": capabilities,
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
