from __future__ import annotations

import ctypes
import os

from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform._run import CommandRunner, default_runner
from notmyfault.platform.errors import BackendFailedError
from notmyfault.platform.windows import _win32


class SessionBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def lock(self) -> None:
        with NATIVE_LOCK:
            if not _win32.user32().LockWorkStation():
                raise BackendFailedError("LockWorkStation 调用失败（可能被系统拒绝）")

    def session_id(self) -> str | None:
        session = _win32.SESSION_DWORD()
        with NATIVE_LOCK:
            if not _win32.kernel32().ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
                raise BackendFailedError("无法查询当前进程的 Windows 会话 ID")
        return str(session.value)

    def is_locked(self) -> bool:
        with NATIVE_LOCK:
            api = _win32.wtsapi32()
            buffer = ctypes.c_void_p()
            length = _win32.SESSION_DWORD()
            try:
                if not api.WTSQuerySessionInformationW(
                    None, int(self.session_id()), _win32.WTSSessionInfoEx,
                    ctypes.byref(buffer), ctypes.byref(length),
                ):
                    raise BackendFailedError("无法查询 Windows 会话锁屏状态")
                if not buffer.value or length.value < ctypes.sizeof(_win32.WTSINFOEXW):
                    raise BackendFailedError("Windows 会话信息长度无效")
                info = ctypes.cast(buffer, ctypes.POINTER(_win32.WTSINFOEXW)).contents
                if info.Level != 1:
                    raise BackendFailedError("Windows 会话信息 Level 无效")
                flags = info.Data.WTSInfoExLevel1.SessionFlags
                if flags not in (_win32.WTS_SESSIONSTATE_LOCK, _win32.WTS_SESSIONSTATE_UNLOCK):
                    raise BackendFailedError("Windows 会话锁屏状态未知")
                return flags == _win32.WTS_SESSIONSTATE_LOCK
            finally:
                if buffer.value:
                    api.WTSFreeMemory(buffer)

    def idle_seconds(self) -> float:
        info = _win32.LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        with NATIVE_LOCK:
            if not _win32.user32().GetLastInputInfo(ctypes.byref(info)):
                raise BackendFailedError("读取用户空闲时间失败")
            tick = _win32.kernel32().GetTickCount()
        return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0
