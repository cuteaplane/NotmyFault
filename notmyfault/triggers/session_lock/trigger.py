"""锁屏状态触发器：检测 Windows 锁屏和解锁
Windows 用 psutil 读 LockApp/logonui 进程；Linux 用 loginctl 查 LockedHint
"""

import os
import subprocess

from notmyfault.triggers.base import PollingTrigger



def _is_locked() -> bool | None:
    """返回锁屏状态，查询失败返回 None"""
    if os.name == "nt":
        return _is_locked_windows()
    return _is_locked_linux()


def _is_locked_windows() -> bool | None:
    import psutil
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info["name"] or "").casefold() in {"logonui.exe", "lockapp.exe"}:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _is_locked_linux() -> bool | None:
    import shutil
    if not shutil.which("loginctl"):
        return None
    session_id = os.environ.get("XDG_SESSION_ID", "")
    if not session_id:
        return None
    try:
        result = subprocess.run(
            ["loginctl", "show-session", session_id, "-p", "LockedHint"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip().lower()
    if text == "lockedhint=yes":
        return True
    if text == "lockedhint=no":
        return False
    return None


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
        self._last_state: str | None = None
        self._pending: str | None = None

    def poll(self) -> None:
        locked = _is_locked()
        if locked is None:
            raise RuntimeError("无法查询会话锁屏状态，请检查登录会话和 loginctl")
        state = "locked" if locked else "unlocked"
        if self._last_state is None:
            # 第一轮只记录当前状态，不触发
            self._last_state = state
            return
        if state == self._last_state:
            self._pending = None
            return

        if self._pending == state:
            # 连续第二次采样仍为新状态：确认变化并上报
            previous = self._last_state
            if self.target_state in ("any", state):
                self.log(f"锁屏状态变化: {previous} -> {state}")
                self.emit({"state": state, "previous_state": previous})
            self._last_state = state
            self._pending = None
        else:
            # 首次采到新状态时记录到 _pending，下一轮再次确认
            self._pending = state


def run(meta, config, emit_event, shutdown_event):
    SessionLockTrigger(meta, config, emit_event, shutdown_event).run()
