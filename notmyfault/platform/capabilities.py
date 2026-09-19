from __future__ import annotations

import os
import sys

CLIPBOARD_READ = "clipboard.read"
CLIPBOARD_WRITE = "clipboard.write"
INPUT_SEND = "input.send"
INPUT_GLOBAL_HOTKEY = "input.global_hotkey"
WINDOW_ENUMERATE = "window.enumerate"
WINDOW_PIN = "window.pin"
SCREEN_CAPTURE = "screen.capture"
DISPLAY_BRIGHTNESS = "display.brightness"
AUDIO_CONTROL = "audio.control"
AUDIO_DEVICE_QUERY = "audio.device_query"
BLUETOOTH_CONTROL = "bluetooth.control"
SESSION_LOCK = "session.lock"
TTS = "tts"
TRAY = "tray"

CAPABILITY_IDS = frozenset({
    CLIPBOARD_READ,
    CLIPBOARD_WRITE,
    INPUT_SEND,
    INPUT_GLOBAL_HOTKEY,
    WINDOW_ENUMERATE,
    WINDOW_PIN,
    SCREEN_CAPTURE,
    DISPLAY_BRIGHTNESS,
    AUDIO_CONTROL,
    AUDIO_DEVICE_QUERY,
    BLUETOOTH_CONTROL,
    SESSION_LOCK,
    TTS,
    TRAY,
})


def _entry(available: bool, backend: str | None, reason: str | None = None,
           degraded: bool = False) -> dict:
    return {
        "available": available,
        "backend": backend,
        "reason": reason,
        "degraded": degraded,
    }


def _linux_command(*names: str) -> tuple[str | None, str | None]:
    from notmyfault.platform.linux_support import command_path

    path = command_path(*names)
    if path:
        return path, None
    return None, "未安装 " + " 或 ".join(names)


def _windows_backend(backend: str, *, modules=(), apis=None, reason=None) -> dict:
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


def _probe_windows(capability: str) -> dict:
    import shutil

    if capability == DISPLAY_BRIGHTNESS:
        ddc = _windows_backend("DDC-CI", apis={"user32": ("EnumDisplayMonitors",), "Dxva2": ("GetPhysicalMonitorsFromHMONITOR", "GetMonitorBrightness", "SetMonitorBrightness")}, reason="显示器亮度支持在执行时检查")
        if shutil.which("powershell"):
            return _entry(True, "WMI/DDC-CI" if ddc["available"] else "WMI", "显示器亮度支持在执行时检查", degraded=not ddc["available"])
        return ddc
    if capability == AUDIO_DEVICE_QUERY:
        return _windows_backend("MMDeviceEnumerator", modules=("pycaw", "comtypes"), reason="音频设备在执行时检查")
    if capability == BLUETOOTH_CONTROL:
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if not os.path.isfile(path):
            return _entry(False, None, "未安装 Windows PowerShell")
        return _windows_backend("WinRT", apis={"combase": ("RoGetActivationFactory",)}, reason="蓝牙设备和无线电权限在执行时检查")
    probes = {
        CLIPBOARD_READ: lambda: _windows_backend("user32-clipboard", apis={"user32": ("OpenClipboard", "CloseClipboard", "GetClipboardData", "IsClipboardFormatAvailable"), "kernel32": ("GlobalLock", "GlobalUnlock", "GlobalSize")}),
        CLIPBOARD_WRITE: lambda: _windows_backend("user32-clipboard", apis={"user32": ("OpenClipboard", "CloseClipboard", "EmptyClipboard", "SetClipboardData"), "kernel32": ("GlobalAlloc", "GlobalLock", "GlobalUnlock", "GlobalFree")}),
        INPUT_SEND: lambda: _windows_backend("SendInput", apis={"user32": ("SendInput",)}),
        INPUT_GLOBAL_HOTKEY: lambda: _windows_backend("RegisterHotKey", apis={"user32": ("RegisterHotKey", "UnregisterHotKey", "PeekMessageW", "GetAsyncKeyState")}),
        WINDOW_ENUMERATE: lambda: _windows_backend("EnumWindows", apis={"user32": ("EnumWindows", "GetWindowTextW", "GetWindowTextLengthW", "IsWindowVisible")}),
        WINDOW_PIN: lambda: _windows_backend("SetWindowPos", apis={"user32": ("EnumWindows", "GetWindowTextW", "SetWindowPos")}),
        SCREEN_CAPTURE: lambda: _windows_backend("BitBlt", apis={"user32": ("GetDC", "ReleaseDC"), "gdi32": ("BitBlt", "CreateCompatibleDC", "CreateCompatibleBitmap", "SelectObject", "GetDIBits", "DeleteObject", "DeleteDC")}),
        AUDIO_CONTROL: lambda: _windows_backend("pycaw", modules=("pycaw", "comtypes"), reason="音频设备在执行时检查"),
        SESSION_LOCK: lambda: _windows_backend("LockWorkStation", apis={"user32": ("LockWorkStation",)}),
        TTS: lambda: _windows_backend("SAPI", modules=("pythoncom", "win32com.client"), reason="SAPI 语音服务在执行时检查"),
        TRAY: lambda: _windows_backend("explorer-tray", modules=("win32api", "win32con", "win32gui"), apis={"shell32": ("Shell_NotifyIconW",)}),
    }
    return probes[capability]()


def _probe_linux(capability: str) -> dict:
    from notmyfault.platform.linux_support import session_type

    session = session_type()
    wayland = session == "wayland"
    if capability == CLIPBOARD_READ:
        names = (
            ("wl-paste",)
            if wayland
            else (("xclip", "xsel") if session == "x11" else ("wl-paste", "xclip", "xsel"))
        )
        path, reason = _linux_command(*names)
        return _entry(bool(path), path, reason)
    if capability == CLIPBOARD_WRITE:
        names = (
            ("wl-copy",)
            if wayland
            else (("xclip", "xsel") if session == "x11" else ("wl-copy", "xclip", "xsel"))
        )
        path, reason = _linux_command(*names)
        return _entry(bool(path), path, reason)
    if capability == INPUT_SEND:
        path, reason = _linux_command(*(("ydotool",) if wayland else ("xdotool", "ydotool")))
        return _entry(bool(path), path, reason)
    if capability == INPUT_GLOBAL_HOTKEY:
        if wayland:
            return _entry(False, None, "Wayland 不允许普通应用全局监听按键")
        try:
            import Xlib  # noqa: F401
            return _entry(True, "Xlib-XGrabKey")
        except ImportError:
            return _entry(False, None, "未安装 python-xlib")
    if capability == WINDOW_ENUMERATE:
        if wayland:
            return _entry(False, None, "Wayland 不允许普通应用枚举其他应用窗口")
        path, reason = _linux_command("xdotool")
        return _entry(bool(path), path, reason)
    if capability == WINDOW_PIN:
        if wayland:
            return _entry(False, None, "Wayland 下没有统一的窗口置顶接口")
        path, reason = _linux_command("wmctrl")
        if path:
            xprop, reason = _linux_command("xprop")
            if not xprop:
                return _entry(False, None, reason)
        return _entry(bool(path), path, reason)
    if capability == SCREEN_CAPTURE:
        if wayland:
            # portal 截图走的是 python 包 dbus_next，不是 gdbus 命令
            try:
                import importlib.util

                if importlib.util.find_spec("dbus_next"):
                    return _entry(True, "xdg-desktop-portal+dbus_next", "Portal 服务和截图授权在执行时检查", degraded=True)
            except (ImportError, ValueError):
                pass
            return _entry(False, None, "未安装 dbus-next，无法调用截图 portal")
        path, reason = _linux_command("gnome-screenshot", "spectacle", "import")
        return _entry(bool(path), path, reason)
    if capability == DISPLAY_BRIGHTNESS:
        path, reason = _linux_command("brightnessctl")
        return _entry(bool(path), path, reason)
    if capability == AUDIO_CONTROL:
        path, reason = _linux_command("wpctl")
        return _entry(bool(path), path, reason)
    if capability == AUDIO_DEVICE_QUERY:
        path, reason = _linux_command("wpctl", "pactl")
        return _entry(bool(path), path, reason)
    if capability == BLUETOOTH_CONTROL:
        path, reason = _linux_command("bluetoothctl")
        return _entry(bool(path), path, reason)
    if capability == SESSION_LOCK:
        path, reason = _linux_command("loginctl")
        return _entry(bool(path), path, reason)
    if capability == TTS:
        path, reason = _linux_command("spd-say")
        return _entry(bool(path), path, reason)
    if capability == TRAY:
        if wayland:
            return _entry(False, None, "Wayland 没有统一的系统托盘协议支持")
        try:
            import pystray  # noqa: F401
            return _entry(True, "pystray-xembed")
        except ImportError:
            return _entry(False, None, "未安装 pystray")
        except Exception as error:
            return _entry(False, None, f"pystray 后端不可用: {error}")
    raise ValueError(f"未知能力 id: {capability!r}")


def _probe_unsupported_platform(_capability: str) -> dict:
    return _entry(False, None, "当前平台暂未提供此能力")


def probe_capability(capability: str) -> dict:
    if capability not in CAPABILITY_IDS:
        raise ValueError(f"未知能力 id: {capability!r}")
    if os.name == "nt":
        probe = _probe_windows
    elif sys.platform.startswith("linux"):
        probe = _probe_linux
    else:
        probe = _probe_unsupported_platform
    return probe(capability)


def probe_capabilities() -> dict[str, dict]:
    """返回全部能力的当前状态，platform 与 capability 分开判断"""
    return {
        capability: probe_capability(capability)
        for capability in sorted(CAPABILITY_IDS)
    }


def missing_capabilities(
    required: list[str],
    report: dict[str, dict] | None = None,
) -> list[dict]:
    if report is None:
        report = probe_capabilities()
    problems = []
    for capability in required:
        entry = report.get(capability)
        if entry is None:
            problems.append({
                "capability": capability,
                "reason": f"未知能力 id: {capability!r}",
            })
        elif not entry["available"]:
            problems.append({
                "capability": capability,
                "reason": entry["reason"] or "当前系统不可用",
            })
    return problems


def is_capability_compatible(
    meta: dict,
    report: dict[str, dict] | None = None,
) -> tuple[bool, list[dict]]:
    """清单没有 requires_capabilities 时视为兼容"""
    required = meta.get("requires_capabilities")
    if not required:
        return True, []
    problems = missing_capabilities(list(required), report)
    return not problems, problems
