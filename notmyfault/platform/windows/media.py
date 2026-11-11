from __future__ import annotations

import ctypes

from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError
from . import _win32
from .audio import AudioBackend

_COMMANDS = {
    "play_pause": 14, "stop": 13, "next": 11, "previous": 12,
    "volume_up": 10, "volume_down": 9,
}
_WM_APPCOMMAND = 0x0319
_SMTO_ABORTIFHUNG = 0x0002


class MediaBackend:
    def __init__(self, runner=None) -> None:
        self._runner = runner

    def send(self, command: str) -> None:
        if command == "mute":
            AudioBackend(self._runner).set_muted(True)
            return
        if command not in _COMMANDS:
            raise BackendInvalidError(f"未知媒体命令: {command!r}")
        with NATIVE_LOCK:
            user32 = _win32.user32()
            lparam = _COMMANDS[command] << 16
            result = ctypes.c_size_t()
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                sent = user32.SendMessageTimeoutW(
                    hwnd, _WM_APPCOMMAND, 0, lparam,
                    _SMTO_ABORTIFHUNG, 1000, ctypes.byref(result),
                )
                if sent:
                    return
            shell = user32.FindWindowW("Shell_TrayWnd", None)
            if shell:
                sent = user32.SendMessageTimeoutW(
                    shell, _WM_APPCOMMAND, 0, lparam,
                    _SMTO_ABORTIFHUNG, 1000, ctypes.byref(result),
                )
                if sent:
                    return
            raise BackendFailedError("没有窗口接受媒体命令")
