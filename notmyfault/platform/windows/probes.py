from __future__ import annotations

import os
import shutil

from notmyfault.platform import capabilities as ids
from notmyfault.platform._run import powershell_path
from notmyfault.platform.capabilities import entry as _entry


def _backend(backend: str, *, modules=(), apis=None, reason=None) -> dict:
    import ctypes
    import importlib.util

    for name in modules:
        try:
            present = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            present = False
        if not present:
            return _entry(False, None, f"未安装 {name}")
    for library, names in (apis or {}).items():
        try:
            dll = ctypes.WinDLL(library, use_last_error=True)
            for name in names:
                getattr(dll, name)
        except (OSError, AttributeError) as error:
            return _entry(False, None, f"{library} 接口不可用: {error}")
    return _entry(True, backend, reason)


def _display_brightness() -> dict:
    ddc = _backend("DDC-CI", apis={"user32": ("EnumDisplayMonitors",), "Dxva2": ("GetPhysicalMonitorsFromHMONITOR", "GetMonitorBrightness", "SetMonitorBrightness")}, reason="显示器亮度支持在执行时检查")
    if os.path.isfile(powershell_path()) or shutil.which("powershell"):
        return _entry(True, "WMI/DDC-CI" if ddc["available"] else "WMI", "显示器亮度支持在执行时检查", degraded=not ddc["available"])
    return ddc


def _audio_device_query() -> dict:
    return _backend("MMDeviceEnumerator", modules=("pycaw", "comtypes"), reason="音频设备在执行时检查")


def _bluetooth_control() -> dict:
    if not os.path.isfile(powershell_path()):
        return _entry(False, None, "未安装 Windows PowerShell")
    return _backend("winrt", apis={"combase": ("RoGetActivationFactory",)}, reason="蓝牙设备和无线电权限在执行时检查")


def _power_profile() -> dict:
    if shutil.which("powercfg"):
        return _entry(True, "powercfg")
    return _entry(False, None, "未找到 powercfg")


def _clipboard_read() -> dict:
    return _backend("user32-clipboard", apis={"user32": ("OpenClipboard", "CloseClipboard", "GetClipboardData", "IsClipboardFormatAvailable"), "kernel32": ("GlobalLock", "GlobalUnlock", "GlobalSize")})


def _clipboard_write() -> dict:
    return _backend("user32-clipboard", apis={"user32": ("OpenClipboard", "CloseClipboard", "EmptyClipboard", "SetClipboardData"), "kernel32": ("GlobalAlloc", "GlobalLock", "GlobalUnlock", "GlobalFree")})


def _input_send() -> dict:
    return _backend("SendInput", apis={"user32": ("SendInput",)})


def _input_global_hotkey() -> dict:
    return _backend("RegisterHotKey", apis={"user32": ("RegisterHotKey", "UnregisterHotKey", "PeekMessageW", "GetAsyncKeyState")})


def _window_enumerate() -> dict:
    return _backend("EnumWindows", apis={"user32": ("EnumWindows", "GetWindowTextW", "GetWindowTextLengthW", "IsWindowVisible")})


def _window_control() -> dict:
    return _backend("user32-window", apis={"user32": (
        "IsWindow", "ShowWindowAsync", "SetForegroundWindow", "SetWindowPos",
        "GetWindowLongW", "SetWindowLongW", "GetLayeredWindowAttributes",
        "SetLayeredWindowAttributes", "PostMessageW",
    )})


def _window_pin() -> dict:
    return _backend("SetWindowPos", apis={"user32": ("EnumWindows", "GetWindowTextW", "SetWindowPos")})


def _screen_capture() -> dict:
    return _backend("BitBlt", apis={"user32": ("GetDC", "ReleaseDC"), "gdi32": ("BitBlt", "CreateCompatibleDC", "CreateCompatibleBitmap", "SelectObject", "GetDIBits", "DeleteObject", "DeleteDC")})


def _screen_ocr() -> dict:
    if not shutil.which("tesseract"):
        return _entry(False, None, "未安装 tesseract")
    capture = _screen_capture()
    if not capture["available"]:
        return _entry(False, None, capture["reason"])
    return _entry(True, "tesseract")


def _display_power() -> dict:
    return _backend("SendNotifyMessage", apis={"user32": ("SendNotifyMessageW",)})


def _audio_control() -> dict:
    return _backend("pycaw", modules=("pycaw", "comtypes"), reason="音频设备在执行时检查")


def _media_control() -> dict:
    return _backend("WM_APPCOMMAND", apis={"user32": ("GetForegroundWindow", "SendMessageTimeoutW")})


def _session_lock() -> dict:
    return _backend("LockWorkStation", apis={"user32": ("LockWorkStation",)})


def _idle_query() -> dict:
    return _backend("GetLastInputInfo", apis={"user32": ("GetLastInputInfo",), "kernel32": ("GetTickCount",)})


def _tts() -> dict:
    return _backend("SAPI", modules=("pythoncom", "win32com.client"), reason="SAPI 语音服务在执行时检查")


def _tray() -> dict:
    return _backend("explorer-tray", modules=("win32api", "win32con", "win32gui"), apis={"shell32": ("Shell_NotifyIconW",)})


def _file_lock() -> dict:
    return _backend("CreateFileW", apis={"kernel32": ("CreateFileW", "CloseHandle")})


def _session_state() -> dict:
    return _backend("WTS", apis={
        "wtsapi32": ("WTSQuerySessionInformationW", "WTSFreeMemory"),
        "kernel32": ("ProcessIdToSessionId",),
    })


def _power_state() -> dict:
    return _backend("GetSystemPowerStatus/WM_POWERBROADCAST", apis={
        "kernel32": ("GetSystemPowerStatus", "GetModuleHandleW", "GetCurrentThreadId"),
        "user32": ("RegisterClassW", "CreateWindowExW", "DefWindowProcW",
                   "PeekMessageW", "TranslateMessage", "DispatchMessageW",
                   "DestroyWindow", "UnregisterClassW"),
    })


def _power_system() -> dict:
    return _backend("ExitWindowsEx/SetSuspendState", apis={
        "user32": ("ExitWindowsEx",),
        "powrprof": ("SetSuspendState",),
        "advapi32": ("OpenProcessToken", "LookupPrivilegeValueW", "AdjustTokenPrivileges"),
        "kernel32": ("GetCurrentProcess", "CloseHandle"),
    }, reason="关机权限在执行时检查")


def _wifi_query() -> dict:
    return _backend("WLAN", apis={"wlanapi": (
        "WlanOpenHandle", "WlanCloseHandle", "WlanEnumInterfaces",
        "WlanQueryInterface", "WlanFreeMemory",
    )})


def _wifi_control() -> dict:
    if not shutil.which("netsh"):
        return _entry(False, None, "未找到 netsh")
    return _backend("netsh/WLAN", apis={
        "wlanapi": ("WlanOpenHandle", "WlanCloseHandle", "WlanEnumInterfaces",
                    "WlanGetProfileList", "WlanGetProfile", "WlanFreeMemory"),
        "iphlpapi": ("ConvertInterfaceGuidToLuid", "ConvertInterfaceLuidToAlias"),
    }, modules=("win32security", "win32file"), reason="无线设备、配置及权限在执行时检查")


def _desktop_wallpaper() -> dict:
    return _backend("SystemParametersInfoW", apis={"user32": ("SystemParametersInfoW",)})


def _desktop_shortcut() -> dict:
    if not os.path.isfile(powershell_path()) and not shutil.which("powershell"):
        return _entry(False, None, "未安装 Windows PowerShell")
    result = _backend("IShellLinkW", apis={"ole32": ("CoCreateInstance",)})
    if result["available"]:
        result.update(degraded=True, reason="自定义目录缺少接口参数，仅支持 desktop/start_menu/startup")
    return result


PROBES = {
    ids.CLIPBOARD_READ: _clipboard_read,
    ids.CLIPBOARD_WRITE: _clipboard_write,
    ids.INPUT_SEND: _input_send,
    ids.INPUT_GLOBAL_HOTKEY: _input_global_hotkey,
    ids.WINDOW_ENUMERATE: _window_enumerate,
    ids.WINDOW_CONTROL: _window_control,
    ids.WINDOW_PIN: _window_pin,
    ids.SCREEN_CAPTURE: _screen_capture,
    ids.SCREEN_OCR: _screen_ocr,
    ids.DISPLAY_BRIGHTNESS: _display_brightness,
    ids.DISPLAY_POWER: _display_power,
    ids.AUDIO_CONTROL: _audio_control,
    ids.AUDIO_DEVICE_QUERY: _audio_device_query,
    ids.MEDIA_CONTROL: _media_control,
    ids.BLUETOOTH_CONTROL: _bluetooth_control,
    ids.SESSION_LOCK: _session_lock,
    ids.IDLE_QUERY: _idle_query,
    ids.TTS: _tts,
    ids.TRAY: _tray,
    ids.POWER_PROFILE: _power_profile,
    ids.FILE_LOCK: _file_lock,
    ids.SESSION_STATE: _session_state,
    ids.POWER_STATE: _power_state,
    ids.POWER_SYSTEM: _power_system,
    ids.WIFI_QUERY: _wifi_query,
    ids.WIFI_CONTROL: _wifi_control,
    ids.DESKTOP_WALLPAPER: _desktop_wallpaper,
    ids.DESKTOP_SHORTCUT: _desktop_shortcut,
}
