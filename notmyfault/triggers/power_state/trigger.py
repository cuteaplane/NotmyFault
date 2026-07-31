"""电源状态监测：交流/电池/低电量轮询 + Windows 睡眠恢复事件监听。

- ac / battery / low_battery：轮询系统电源状态，状态变化时触发。
- resume：Windows 下通过隐藏窗口接收 WM_POWERBROADCAST 消息，
  系统从睡眠恢复时触发一次；非 Windows 平台不支持该选项。
"""

import ctypes
import os
import psutil

WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMSUSPEND = 0x0004

# WNDPROC 回调必须保持引用存活，否则 ctypes 会回收回调导致崩溃。
_WND_PROC_HOLD: list = []


def _is_on_battery():
    if os.name != "nt":
        battery = psutil.sensors_battery()
        if battery is None:
            return False, 100
        return not battery.power_plugged, round(battery.percent)
    try:
        SYSTEM_POWER_STATUS = ctypes.c_uint8 * 12
        sps = SYSTEM_POWER_STATUS()
        ctypes.windll.kernel32.GetSystemPowerStatus(sps)
        ac_line = sps[0]
        battery_life = sps[2]
        return ac_line == 0, battery_life
    except Exception:
        return False, 100


def _create_power_event_window():
    """创建隐藏窗口接收电源广播；失败返回 None（不阻塞轮询路径）。"""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_size_t,
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
            ]

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_size_t),
                ("time", ctypes.c_uint),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long),
            ]

        state = {"resume": False}

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST:
                if wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    state["resume"] = True
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        _WND_PROC_HOLD.append(wnd_proc)

        class_name = "NotmyFaultPowerState"
        wc = WNDCLASSW()
        wc.lpfnWndProc = WNDPROC(wnd_proc)
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            return None

        hwnd = user32.CreateWindowExW(
            0, class_name, class_name, 0, 0, 0, 0, 0,
            None, None, wc.hInstance, None,
        )
        if not hwnd:
            return None
        return {
            "hwnd": hwnd,
            "state": state,
            "user32": user32,
            "msg_cls": MSG,
            "class_name": class_name,
            "wnd_proc": wnd_proc,
        }
    except Exception:
        return None


def _pump_power_messages(window) -> bool:
    """处理消息队列中的电源广播；返回本轮是否发生了 resume。"""
    if window is None:
        return False
    try:
        user32 = window["user32"]
        msg = window["msg_cls"]()
        while user32.PeekMessageW(ctypes.byref(msg), window["hwnd"], 0, 0, 1):
            if msg.message == 0x0012:  # WM_QUIT
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        resumed = window["state"]["resume"]
        window["state"]["resume"] = False
        return resumed
    except Exception:
        return False


def _destroy_power_event_window(window) -> None:
    """销毁隐藏窗口并注销窗口类，避免线程退出后泄漏。"""
    if window is None:
        return
    try:
        hwnd = window.get("hwnd")
        class_name = window.get("class_name")
        wnd_proc = window.get("wnd_proc")
        if wnd_proc in _WND_PROC_HOLD:
            _WND_PROC_HOLD.remove(wnd_proc)
        if hwnd:
            ctypes.windll.user32.DestroyWindow(hwnd)
        if class_name:
            ctypes.windll.user32.UnregisterClassW(
                class_name, ctypes.windll.kernel32.GetModuleHandleW(None)
            )
    except Exception:
        pass

def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "power_state")
    target_state = config.get("state", "ac")
    if target_state not in ("ac", "battery", "low_battery", "resume"):
        raise ValueError(
            f"无效的电源状态: {target_state!r}"
            "（可选: ac/battery/low_battery/resume）"
        )
    print(f"[Trigger:{trigger_id}] 开始监控电源状态，目标: {target_state}")

    # resume 只在 Windows 上通过电源广播消息实现。
    power_window = _create_power_event_window() if os.name == "nt" else None
    if target_state == "resume" and power_window is None:
        print(
            f"[Trigger:{trigger_id}] 当前平台不支持睡眠恢复事件监听，"
            "resume 规则不会触发"
        )

    try:
        on_battery, _ = _is_on_battery()
    except Exception as e:
        print(f"[Trigger:{trigger_id}] 初始电源状态读取失败: {e}")
        on_battery = False
    last_state = "battery" if on_battery else "ac"
    low_battery_active = False

    try:
        while not shutdown_event.is_set():
            try:
                if _pump_power_messages(power_window):
                    if target_state == "resume":
                        print(f"[Trigger:{trigger_id}] 系统从睡眠中恢复")
                        _, resume_pct = _is_on_battery()
                        emit_event({"state": "resume", "battery_percent": resume_pct})

                on_battery, battery_pct = _is_on_battery()
                current = "battery" if on_battery else "ac"

                if current != last_state:
                    if current == target_state:
                        print(f"[Trigger:{trigger_id}] 电源状态变化: {current}")
                        emit_event({"state": current, "battery_percent": battery_pct})
                    last_state = current

                # 低电量仅在首次进入时触发一次，恢复后重置，避免每轮重复发事件
                is_low = on_battery and battery_pct <= 20
                if is_low and not low_battery_active and target_state == "low_battery":
                    print(f"[Trigger:{trigger_id}] 低电量: {battery_pct}%")
                    emit_event({"state": "low_battery", "battery_percent": battery_pct})
                    low_battery_active = True
                elif not is_low:
                    low_battery_active = False
            except Exception as e:
                print(f"[Trigger:{trigger_id}] 检查电源出错: {e}")

            shutdown_event.wait(2)
    finally:
        _destroy_power_event_window(power_window)
