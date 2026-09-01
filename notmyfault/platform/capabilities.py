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


def _probe_windows(capability: str) -> dict:
    import importlib.util

    def _has_module(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    probes = {
        CLIPBOARD_READ: lambda: _entry(True, "user32-clipboard"),
        CLIPBOARD_WRITE: lambda: _entry(True, "user32-clipboard"),
        INPUT_SEND: lambda: _entry(True, "SendInput"),
        INPUT_GLOBAL_HOTKEY: lambda: _entry(True, "RegisterHotKey"),
        WINDOW_ENUMERATE: lambda: _entry(True, "EnumWindows"),
        WINDOW_PIN: lambda: _entry(True, "SetWindowPos"),
        SCREEN_CAPTURE: lambda: _entry(True, "BitBlt"),
        DISPLAY_BRIGHTNESS: lambda: _entry(True, "WMI/DDC-CI"),
        # pycaw 是运行期 import 的第三方包，装不上时音量动作才会 ImportError
        AUDIO_CONTROL: lambda: (
            _entry(True, "pycaw")
            if _has_module("pycaw")
            else _entry(False, None, "未安装 pycaw")
        ),
        AUDIO_DEVICE_QUERY: lambda: _entry(True, "MMDeviceEnumerator"),
        BLUETOOTH_CONTROL: lambda: _entry(True, "WinRT"),
        SESSION_LOCK: lambda: _entry(True, "LockWorkStation"),
        TTS: lambda: _entry(True, "SAPI"),
        TRAY: lambda: _entry(True, "explorer-tray"),
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
        path, reason = _linux_command("xdotool", "ydotool")
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
        return _entry(bool(path), path, reason)
    if capability == SCREEN_CAPTURE:
        if wayland:
            # portal 截图走的是 python 包 dbus_next，不是 gdbus 命令
            try:
                import importlib.util

                if importlib.util.find_spec("dbus_next"):
                    return _entry(True, "xdg-desktop-portal+dbus_next")
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
