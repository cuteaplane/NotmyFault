import ctypes
import math
import os

from notmyfault.triggers.base import PollingTrigger


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


if os.name == "nt":
    # dwTime 是 32 位 tick，差值按 DWORD 回绕
    ctypes.windll.kernel32.GetTickCount.argtypes = []
    ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint
    ctypes.windll.user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
    ctypes.windll.user32.GetLastInputInfo.restype = ctypes.c_bool


def _tick_delta_seconds(tick: int, last_input: int) -> float:
    return ((tick - last_input) & 0xFFFFFFFF) / 1000.0


def _get_idle_seconds() -> float:
    """返回自最后输入事件起的系统空闲秒数"""
    if os.name != "nt":
        from notmyfault.platform.linux_support import get_idle_seconds
        return get_idle_seconds()
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    return _tick_delta_seconds(tick, lii.dwTime)


class IdleDetectTrigger(PollingTrigger):
    interval = 2.0
    native = os.name == "nt"

    def validate(self):
        try:
            self.threshold = float(self.config.get("idle_seconds", 300))
        except (ValueError, TypeError):
            raise ValueError(
                f"无效的空闲阈值配置: {self.config.get('idle_seconds')!r}"
            ) from None
        if not math.isfinite(self.threshold) or self.threshold < 0:
            raise ValueError("空闲阈值必须是非负有限秒数")

    def setup(self):
        self._was_idle = False
        self.log(f"开始监视空闲状态，阈值: {self.threshold} 秒")

    def poll(self):
        idle_secs = _get_idle_seconds()
        idle = idle_secs >= self.threshold
        if idle and not self._was_idle:
            self.log(f"用户进入空闲状态 ({int(idle_secs)}s >= {self.threshold}s)")
            self.emit({"state": "idle", "idle_seconds": self.threshold})
        elif not idle and self._was_idle:
            self.log(f"用户恢复活动 (阈值 {self.threshold}s)")
            self.emit({"state": "active", "idle_seconds": self.threshold})
        self._was_idle = idle


def run(meta, config, emit_event, shutdown_event):
    IdleDetectTrigger(meta, config, emit_event, shutdown_event).run()
