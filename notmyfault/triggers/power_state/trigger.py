import time
import ctypes
import threading

WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMSUSPEND = 0x0004

kernel32 = ctypes.windll.kernel32


def _is_on_battery():
    try:
        SYSTEM_POWER_STATUS = ctypes.c_uint8 * 12
        sps = SYSTEM_POWER_STATUS()
        ctypes.windll.kernel32.GetSystemPowerStatus(sps)
        ac_line = sps[0]
        battery_life = sps[2]
        return ac_line == 0, battery_life
    except Exception:
        return False, 100


def run(meta, config_list, emit_event, shutdown_event):
    trigger_id = meta.get("id", "power_state")

    target_states = set()
    for cfg in config_list:
        s = cfg.get("state", "ac")
        target_states.add(s)

    if not target_states:
        print(f"[Trigger:{trigger_id}] 没有配置目标状态，退出")
        return

    print(f"[Trigger:{trigger_id}] 开始监控电源状态，目标: {target_states}")

    on_battery, _ = _is_on_battery()
    last_state = "battery" if on_battery else "ac"
    low_battery_active = False

    while not shutdown_event.is_set():
        try:
            on_battery, battery_pct = _is_on_battery()
            current = "battery" if on_battery else "ac"

            if current != last_state:
                if current in target_states:
                    print(f"[Trigger:{trigger_id}] 电源状态变化: {current}")
                    emit_event(trigger_id, {"state": current, "battery_percent": battery_pct})
                last_state = current

            # 低电量仅在首次进入时触发一次，恢复后重置，避免每轮重复发事件
            is_low = on_battery and battery_pct <= 20
            if is_low and not low_battery_active and "low_battery" in target_states:
                print(f"[Trigger:{trigger_id}] 低电量: {battery_pct}%")
                emit_event(trigger_id, {"state": "low_battery", "battery_percent": battery_pct})
                low_battery_active = True
            elif not is_low:
                low_battery_active = False

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 检查电源出错: {e}")

        shutdown_event.wait(10)
