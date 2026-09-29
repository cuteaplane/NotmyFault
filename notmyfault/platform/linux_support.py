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


def _parse_gdbus_uint(stdout: str) -> int | None:
    match = re.search(r"uint(?:32|64)\s+(\d+)", stdout)
    if match:
        return int(match.group(1))
    match = re.search(r"\(\s*(\d+)\s*,", stdout)
    if match:
        return int(match.group(1))
    return None


def _current_username() -> str:
    for key in ("USER", "LOGNAME"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return ""


def current_session_id() -> str | None:
    """返回当前图形登录会话 id，环境变量缺失时用 loginctl 查找"""
    session_id = (os.environ.get("XDG_SESSION_ID") or "").strip()
    if session_id:
        return session_id
    loginctl = command_path("loginctl")
    if not loginctl:
        return None
    user = _current_username()
    if user:
        result = run_command(
            [loginctl, "show-user", user, "-p", "Display"],
            timeout=5,
        )
        match = re.search(r"^Display=(\S+)\s*$", result.stdout, re.M)
        if result.returncode == 0 and match and match.group(1):
            return match.group(1)
    result = run_command([loginctl, "list-sessions", "--no-legend"], timeout=5)
    if result.returncode != 0:
        return None
    candidates: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        session, _uid, session_user = parts[0], parts[1], parts[2]
        if user and session_user != user:
            continue
        candidates.append(session)
    for session in candidates:
        info = run_command(
            [loginctl, "show-session", session, "-p", "Active", "-p", "Class"],
            timeout=5,
        )
        if info.returncode != 0:
            continue
        text = info.stdout
        if "Active=yes" in text and "Class=user" in text:
            return session
    for session in candidates:
        info = run_command(
            [loginctl, "show-session", session, "-p", "Active"],
            timeout=5,
        )
        if info.returncode == 0 and "Active=yes" in info.stdout:
            return session
    return candidates[0] if candidates else None


def idle_probe() -> tuple[str | None, str | None]:
    """返回 (backend, reason)，供能力探测复用"""
    desktop = desktop_environment()
    gdbus = command_path("gdbus")
    if desktop == "gnome" and gdbus:
        return "gnome-Mutter.IdleMonitor", None
    if desktop == "kde" and gdbus:
        return "kde-ScreenSaver.GetSessionIdleTime", None
    if gdbus:
        return "freedesktop-ScreenSaver.GetSessionIdleTime", None
    if command_path("xprintidle"):
        return "xprintidle", None
    return None, "依赖缺失：空闲时间检测需要 gdbus（GNOME/KDE）或 xprintidle（X11）"


def _idle_via_screensaver(gdbus: str, dest: str, object_path: str) -> float | None:
    result = run_command(
        [
            gdbus,
            "call",
            "--session",
            "--dest",
            dest,
            "--object-path",
            object_path,
            "--method",
            "org.freedesktop.ScreenSaver.GetSessionIdleTime",
        ],
        timeout=3,
    )
    value = _parse_gdbus_uint(result.stdout)
    if result.returncode == 0 and value is not None:
        return value / 1000
    return None


def get_idle_seconds() -> float:
    desktop = desktop_environment()
    gdbus = command_path("gdbus")
    if desktop == "gnome" and gdbus:
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
        value = _parse_gdbus_uint(result.stdout)
        if result.returncode == 0 and value is not None:
            return value / 1000
    if desktop == "kde" and gdbus:
        for dest, object_path in (
            ("org.kde.screensaver", "/ScreenSaver"),
            ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"),
        ):
            idle = _idle_via_screensaver(gdbus, dest, object_path)
            if idle is not None:
                return idle
    if gdbus:
        idle = _idle_via_screensaver(
            gdbus,
            "org.freedesktop.ScreenSaver",
            "/org/freedesktop/ScreenSaver",
        )
        if idle is not None:
            return idle
    if executable := command_path("xprintidle"):
        result = run_command([executable], timeout=3)
        if result.returncode == 0:
            return int(result.stdout.strip()) / 1000
    _backend, reason = idle_probe()
    raise BackendMissingError(
        reason
        or (
            f"无法读取空闲时间：当前桌面（{desktop}）的空闲接口不可用，"
            "需要 gdbus（GNOME/KDE）或 xprintidle（X11）"
        )
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
            "display_power": (
                None
                if desktop_environment() in ("gnome", "kde")
                or session_type() != "wayland"
                else "非 GNOME/KDE 的 Wayland 会话没有可用的显示器开关接口"
            ),
        },
    }
