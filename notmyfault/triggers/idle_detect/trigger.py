import time
import ctypes
import os


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _get_idle_seconds() -> float:
    """返回系统空闲秒数（自最后输入事件起）"""
    if os.name != "nt":
        from notmyfault.linux_support import get_idle_seconds
        return get_idle_seconds()
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    return (tick - lii.dwTime) / 1000.0


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "idle_detect")
    try:
        threshold = float(config.get("idle_seconds", 300))
    except (ValueError, TypeError):
        raise ValueError(
            f"无效的空闲阈值配置: {config.get('idle_seconds')!r}"
        ) from None
    print(f"[Trigger:{trigger_id}] 开始监视空闲状态，阈值: {threshold} 秒")
    was_idle = False

    while not shutdown_event.is_set():
        try:
            idle_secs = _get_idle_seconds()
            idle = idle_secs >= threshold
            if idle and not was_idle:
                print(f"[Trigger:{trigger_id}] 用户进入空闲状态 ({int(idle_secs)}s >= {threshold}s)")
                emit_event({"state": "idle", "idle_seconds": threshold})
                was_idle = True
            elif not idle and was_idle:
                print(f"[Trigger:{trigger_id}] 用户恢复活动 (阈值 {threshold}s)")
                emit_event({"state": "active", "idle_seconds": threshold})
                was_idle = False
        except Exception as e:
            print(f"[Trigger:{trigger_id}] 扫描出错: {e}")

        shutdown_event.wait(2)
