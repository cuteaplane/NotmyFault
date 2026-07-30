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


def run(meta, config_list, emit_event, shutdown_event):
    trigger_id = meta.get("id", "idle_detect")

    # 每个阈值独立跟踪 was_idle 状态，之前只取 min(thresholds) 导致：
    # 1. 300s 和 600s 的规则都会在 300s 时触发（600s 规则失效）
    # 2. emit 事件不携带阈值，规则无法区分
    threshold_states = {}
    for cfg in config_list:
        try:
            t = float(cfg.get("idle_seconds", 300))
            threshold_states[t] = False
        except (ValueError, TypeError):
            pass

    if not threshold_states:
        print(f"[Trigger:{trigger_id}] 无有效空闲阈值配置，退出")
        return

    print(f"[Trigger:{trigger_id}] 开始监视空闲状态，阈值: {sorted(threshold_states.keys())} 秒")

    while not shutdown_event.is_set():
        try:
            idle_secs = _get_idle_seconds()
            # 遍历快照避免迭代时修改
            for threshold, was_idle in list(threshold_states.items()):
                idle = idle_secs >= threshold
                if idle and not was_idle:
                    print(f"[Trigger:{trigger_id}] 用户进入空闲状态 ({int(idle_secs)}s >= {threshold}s)")
                    # idle_seconds 用字符串与用户配置类型一致
                    # （rules.check_event_params 严格相等比较，
                    #   schema default 是 "300" 字符串，input v-model 也是字符串）
                    idle_str = str(int(threshold)) if threshold == int(threshold) else str(threshold)
                    emit_event(trigger_id, {
                        "state": "idle",
                        "idle_seconds": idle_str,
                    })
                    threshold_states[threshold] = True
                elif not idle and was_idle:
                    print(f"[Trigger:{trigger_id}] 用户恢复活动 (阈值 {threshold}s)")
                    idle_str = str(int(threshold)) if threshold == int(threshold) else str(threshold)
                    emit_event(trigger_id, {
                        "state": "active",
                        "idle_seconds": idle_str,
                    })
                    threshold_states[threshold] = False
        except Exception as e:
            print(f"[Trigger:{trigger_id}] 扫描出错: {e}")

        shutdown_event.wait(2)
