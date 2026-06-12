import time
import ctypes


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _get_idle_seconds() -> float:
    """返回系统空闲秒数（自最后输入事件起）"""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    return (tick - lii.dwTime) / 1000.0


def run(meta, config_list, emit_event):
    trigger_id = meta.get("id", "idle_detect")

    # 取第一条规则的空闲阈值（多条规则用同一触发器时取最小值）
    thresholds = []
    for cfg in config_list:
        try:
            t = float(cfg.get("idle_seconds", 300))
            thresholds.append(t)
        except (ValueError, TypeError):
            pass

    if not thresholds:
        print(f"[Trigger:{trigger_id}] 无有效空闲阈值配置，退出")
        return

    threshold = min(thresholds)
    print(f"[Trigger:{trigger_id}] 开始监视空闲状态，阈值: {threshold} 秒")

    was_idle = False

    while True:
        try:
            idle = _get_idle_seconds() >= threshold
            if idle and not was_idle:
                print(f"[Trigger:{trigger_id}] 用户进入空闲状态 ({int(_get_idle_seconds())}s)")
                emit_event(trigger_id, {"state": "idle"})
                was_idle = True
            elif not idle and was_idle:
                print(f"[Trigger:{trigger_id}] 用户恢复活动")
                emit_event(trigger_id, {"state": "active"})
                was_idle = False
        except Exception as e:
            print(f"[Trigger:{trigger_id}] 扫描出错: {e}")

        time.sleep(2)
