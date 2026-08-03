"""锁屏状态触发器：检测 Windows 锁屏和解锁
PowerShell 子进程读取 LockApp 和 logonui 进程，两次连续采样一致后发送状态变化
"""

import os
import subprocess

from notmyfault.triggers.base import PollingTrigger

_POWERSHELL_QUERY = (
    "$p = Get-Process -Name logonui, LockApp -ErrorAction SilentlyContinue; "
    "if ($p) { Write-Output 'locked=True' } else { Write-Output 'locked=False' }"
)


def _is_locked() -> bool | None:
    """返回锁屏状态，查询失败返回 None，poll 本轮直接返回"""
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _POWERSHELL_QUERY,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("locked="):
            value = line[len("locked="):].strip()
            if value == "True":
                return True
            if value == "False":
                return False
    return None


class SessionLockTrigger(PollingTrigger):
    """锁屏状态触发器，使用 event-v2 轮询"""

    interval: float = 5.0
    native: bool = False  # 探测由 powershell 子进程执行

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
            # 子进程查询失败：保持现状，下次再试
            return
        state = "locked" if locked else "unlocked"
        if self._last_state is None:
            # 首轮只记基线，不发事件
            self._last_state = state
            return
        if state == self._last_state:
            self._pending = None
            return

        if self._pending == state:
            # 连续第二次采样仍为新状态：确认变化并上报
            previous = self._last_state
            self._last_state = state
            self._pending = None
            if self.target_state in ("any", state):
                self.log(f"锁屏状态变化: {previous} -> {state}")
                self.emit({"state": state, "previous_state": previous})
        else:
            # 首次采到新状态时记录到 _pending，下一轮再次确认
            self._pending = state


def run(meta, config, emit_event, shutdown_event):
    SessionLockTrigger(meta, config, emit_event, shutdown_event).run()
