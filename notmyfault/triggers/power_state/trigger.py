"""电源状态触发器：轮询交流、电池和低电量状态，并在 Windows 监听睡眠恢复消息
resume 依赖隐藏窗口接收 WM_POWERBROADCAST，非 Windows 平台没有该事件
"""

import ctypes
import os
import psutil

from notmyfault.plugin_api import native_lock
from notmyfault.triggers.base import PollingTrigger

if os.name == "nt":
    from ctypes import wintypes
    _kernel32 = ctypes.windll.kernel32
    _user32 = ctypes.windll.user32
    _kernel32.GetSystemPowerStatus.argtypes = [ctypes.c_void_p]
    _kernel32.GetSystemPowerStatus.restype = ctypes.c_bool
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                     wintypes.UINT, wintypes.UINT, wintypes.UINT]
    _user32.PeekMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _user32.UnregisterClassW.restype = wintypes.BOOL

WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMSUSPEND = 0x0004

# WNDPROC 回调必须保持引用存活，ctypes 才能继续调用它
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
    """创建隐藏窗口接收电源广播，失败时返回 None"""
    callback = None
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

        state = {"resume": False}

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST:
                if wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    state["resume"] = True
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        callback = WNDPROC(wnd_proc)
        _WND_PROC_HOLD.append(callback)

        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = ctypes.c_ushort
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, ctypes.c_void_p, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        class_name = f"NotmyFaultPowerState_{kernel32.GetCurrentThreadId()}"
        wc = WNDCLASSW()
        wc.lpfnWndProc = callback
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            _WND_PROC_HOLD.remove(callback)
            return None

        hwnd = user32.CreateWindowExW(
            0, class_name, class_name, 0, 0, 0, 0, 0,
            None, None, wc.hInstance, None,
        )
        if not hwnd:
            user32.UnregisterClassW(class_name, wc.hInstance)
            _WND_PROC_HOLD.remove(callback)
            return None
        return {
            "hwnd": hwnd,
            "state": state,
            "user32": user32,
            "msg_cls": wintypes.MSG,
            "class_name": class_name,
            "hinstance": wc.hInstance,
            "wnd_proc": callback,
        }
    except Exception:
        if callback in _WND_PROC_HOLD:
            _WND_PROC_HOLD.remove(callback)
        return None


def _pump_power_messages(window) -> bool:
    """处理消息队列中的电源广播；返回本轮是否发生了 resume"""
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
    """销毁隐藏窗口并注销窗口类，在线程退出时调用"""
    if window is None:
        return
    wnd_proc = None
    try:
        hwnd = window.get("hwnd")
        class_name = window.get("class_name")
        wnd_proc = window.get("wnd_proc")
        if hwnd:
            window["user32"].DestroyWindow(hwnd)
        if class_name:
            window["user32"].UnregisterClassW(
                class_name, window.get("hinstance")
            )
    except Exception:
        pass
    finally:
        if wnd_proc in _WND_PROC_HOLD:
            _WND_PROC_HOLD.remove(wnd_proc)

class PowerStateTrigger(PollingTrigger):
    """电源状态监测：交流/电池/低电量轮询 + Windows 睡眠恢复事件监听"""

    interval = 2.0
    native = True

    def validate(self):
        state = self.config.get("state", "ac")
        if state not in ("ac", "battery", "low_battery", "resume"):
            raise ValueError(
                f"无效的电源状态: {state!r}"
                "（可选: ac/battery/low_battery/resume）"
            )

    def setup(self):
        self.target_state = self.config.get("state", "ac")
        self.log(f"开始监控电源状态，目标: {self.target_state}")
        # resume 只在 Windows 上通过电源广播消息实现；
        # 建窗口也要改共享 user32 函数对象，和 poll 一样持 NATIVE_LOCK
        if os.name == "nt" and self.target_state == "resume":
            with native_lock():
                self.power_window = _create_power_event_window()
        else:
            self.power_window = None
        if self.target_state == "resume" and self.power_window is None:
            self.log("当前平台不支持睡眠恢复事件监听，resume 规则不会触发")
        try:
            with native_lock():
                on_battery, _ = _is_on_battery()
        except Exception as e:
            self.log(f"初始电源状态读取失败: {e}")
            on_battery = False
        self._last_state = "battery" if on_battery else "ac"
        self._low_battery_active = False

    def poll(self):
        if _pump_power_messages(self.power_window):
            if self.target_state == "resume":
                self.log("系统从睡眠中恢复")
                _, resume_pct = _is_on_battery()
                self.emit({"state": "resume", "battery_percent": resume_pct})

        on_battery, battery_pct = _is_on_battery()
        current = "battery" if on_battery else "ac"

        if current != self._last_state:
            if current == self.target_state:
                self.log(f"电源状态变化: {current}")
                self.emit({"state": current, "battery_percent": battery_pct})
            self._last_state = current

        # 低电量首次进入时发送事件，离开阈值后重置标志
        is_low = on_battery and battery_pct <= 20
        if is_low and not self._low_battery_active and self.target_state == "low_battery":
            self.log(f"低电量: {battery_pct}%")
            self.emit({"state": "low_battery", "battery_percent": battery_pct})
            self._low_battery_active = True
        elif not is_low:
            self._low_battery_active = False

    def teardown(self):
        with native_lock():
            _destroy_power_event_window(self.power_window)


def run(meta, config, emit_event, shutdown_event):
    PowerStateTrigger(meta, config, emit_event, shutdown_event).run()
