from __future__ import annotations

import os
import re

from notmyfault.platform._run import CommandRunner, command_path, default_runner, failed_detail
from notmyfault.platform.errors import BackendFailedError, BackendMissingError
from notmyfault.platform.linux import desktop


def _parse_gdbus_uint(stdout: str) -> int | None:
    match = re.search(r"uint(?:32|64)\s+(\d+)", stdout)
    if match:
        return int(match.group(1))
    match = re.search(r"\(\s*(\d+)\s*,", stdout)
    return int(match.group(1)) if match else None


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


def idle_probe() -> tuple[str | None, str | None]:
    """返回空闲接口及不可用原因，供能力探测使用"""
    desktop_name = desktop.desktop_environment()
    gdbus = command_path("gdbus")
    if desktop_name == "gnome" and gdbus:
        return "gnome-Mutter.IdleMonitor", None
    if desktop_name == "kde" and gdbus:
        return "kde-ScreenSaver.GetSessionIdleTime", None
    if gdbus:
        return "freedesktop-ScreenSaver.GetSessionIdleTime", None
    if command_path("xprintidle"):
        return "xprintidle", None
    return None, "依赖缺失：空闲时间检测需要 gdbus（GNOME/KDE）或 xprintidle（X11）"


class SessionBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def _loginctl(self) -> str:
        path = self._runner.which("loginctl")
        if not path:
            raise BackendMissingError("依赖缺失：会话操作需要 loginctl")
        return path

    def _query(self, args: list[str]):
        result = self._runner.run(args, timeout=5)
        if result.returncode != 0:
            raise BackendFailedError(failed_detail(result))
        return result

    def lock(self) -> None:
        self._query([self._loginctl(), "lock-session"])

    def is_locked(self) -> bool:
        session = self.session_id()
        if not session:
            raise BackendFailedError("无法获取当前登录会话 ID")
        result = self._query([
            self._loginctl(), "show-session", session, "-p", "LockedHint",
        ])
        state = result.stdout.strip().lower()
        if state == "lockedhint=yes":
            return True
        if state == "lockedhint=no":
            return False
        raise BackendFailedError("loginctl 未返回有效的 LockedHint")

    def session_id(self) -> str | None:
        session = (os.environ.get("XDG_SESSION_ID") or "").strip()
        if session:
            return session
        loginctl = self._loginctl()
        user = _current_username()
        if user:
            result = self._query([loginctl, "show-user", user, "-p", "Display"])
            match = re.search(r"^Display=(\S+)\s*$", result.stdout, re.M)
            if match:
                return match.group(1)
        result = self._query([loginctl, "list-sessions", "--no-legend"])
        candidates = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and (not user or parts[2] == user):
                candidates.append(parts[0])
        active = []
        for candidate in candidates:
            info = self._query([
                loginctl, "show-session", candidate, "-p", "Active", "-p", "Class",
            ])
            properties = dict(
                line.split("=", 1) for line in info.stdout.splitlines() if "=" in line
            )
            if properties.get("Active") == "yes":
                if properties.get("Class") == "user":
                    return candidate
                active.append(candidate)
        return active[0] if active else candidates[0] if candidates else None

    def _idle_via_screensaver(self, gdbus: str, dest: str, object_path: str) -> float | None:
        result = self._runner.run([
            gdbus, "call", "--session", "--dest", dest,
            "--object-path", object_path,
            "--method", "org.freedesktop.ScreenSaver.GetSessionIdleTime",
        ], timeout=3)
        value = _parse_gdbus_uint(result.stdout)
        if result.returncode == 0 and value is not None:
            return value / 1000
        return None

    def idle_seconds(self) -> float:
        desktop_name = desktop.desktop_environment()
        gdbus = self._runner.which("gdbus")
        if desktop_name == "gnome" and gdbus:
            result = self._runner.run([
                gdbus, "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime",
            ], timeout=3)
            value = _parse_gdbus_uint(result.stdout)
            if result.returncode == 0 and value is not None:
                return value / 1000
        if desktop_name == "kde" and gdbus:
            for dest, object_path in (
                ("org.kde.screensaver", "/ScreenSaver"),
                ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"),
            ):
                idle = self._idle_via_screensaver(gdbus, dest, object_path)
                if idle is not None:
                    return idle
        if gdbus:
            idle = self._idle_via_screensaver(
                gdbus, "org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
            )
            if idle is not None:
                return idle
        executable = self._runner.which("xprintidle")
        if executable:
            result = self._runner.run([executable], timeout=3)
            if result.returncode == 0:
                try:
                    milliseconds = int(result.stdout.strip())
                except ValueError as error:
                    raise BackendFailedError("xprintidle 返回了无效的空闲时间") from error
                if milliseconds >= 0:
                    return milliseconds / 1000
        if not gdbus and not executable:
            raise BackendMissingError("依赖缺失：空闲时间检测需要 gdbus 或 xprintidle")
        raise BackendFailedError(f"无法读取当前桌面（{desktop_name}）的空闲时间")
