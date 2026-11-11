from __future__ import annotations

from notmyfault.platform import capabilities as ids
from notmyfault.platform.capabilities import entry as _entry
from notmyfault.platform._run import command_path
from notmyfault.platform.linux import desktop, session


def _command(*names: str) -> tuple[str | None, str | None]:
    path = command_path(*names)
    if path:
        return path, None
    return None, "未安装 " + " 或 ".join(names)


def _wayland() -> bool:
    return desktop.session_type() == "wayland"


def _clipboard_read() -> dict:
    session_name = desktop.session_type()
    names = (
        ("wl-paste",)
        if session_name == "wayland"
        else (("xclip", "xsel") if session_name == "x11" else ("wl-paste", "xclip", "xsel"))
    )
    path, reason = _command(*names)
    return _entry(bool(path), path, reason)


def _clipboard_write() -> dict:
    session_name = desktop.session_type()
    names = (
        ("wl-copy",)
        if session_name == "wayland"
        else (("xclip", "xsel") if session_name == "x11" else ("wl-copy", "xclip", "xsel"))
    )
    path, reason = _command(*names)
    return _entry(bool(path), path, reason)


def _input_send() -> dict:
    path, reason = _command(*(("ydotool",) if _wayland() else ("xdotool", "ydotool")))
    return _entry(bool(path), path, reason)


def _input_global_hotkey() -> dict:
    if _wayland():
        return _entry(False, None, "Wayland 不允许普通应用全局监听按键")
    try:
        import Xlib  # noqa: F401
        return _entry(True, "Xlib-XGrabKey")
    except ImportError:
        return _entry(False, None, "未安装 python-xlib")


def _window_enumerate() -> dict:
    if _wayland():
        return _entry(False, None, "Wayland 不允许普通应用枚举其他应用窗口")
    path, reason = _command("wmctrl", "xdotool")
    return _entry(bool(path), path, reason)


def _window_control() -> dict:
    if _wayland():
        return _entry(False, None, "Wayland 不允许普通应用控制其他应用窗口")
    wmctrl, reason = _command("wmctrl")
    if not wmctrl:
        return _entry(False, None, reason)
    xdotool, reason = _command("xdotool")
    if not xdotool:
        return _entry(False, None, "隐藏窗口需要 xdotool，其他窗口控制操作需要 wmctrl")
    return _entry(True, "wmctrl+xdotool", "窗口透明度没有统一接口", degraded=True)


def _window_pin() -> dict:
    if _wayland():
        return _entry(False, None, "Wayland 下没有统一的窗口置顶接口")
    path, reason = _command("wmctrl")
    if path:
        xprop, reason = _command("xprop")
        if not xprop:
            return _entry(False, None, reason)
    return _entry(bool(path), path, reason)


def _screen_capture() -> dict:
    if _wayland():
        # portal 截图走的是 python 包 dbus_next，不是 gdbus 命令
        try:
            import importlib.util

            if importlib.util.find_spec("dbus_next"):
                return _entry(True, "xdg-desktop-portal+dbus_next", "Portal 服务和截图授权在执行时检查", degraded=True)
        except (ImportError, ValueError):
            pass
        return _entry(False, None, "未安装 dbus-next，无法调用截图 portal")
    path, reason = _command("gnome-screenshot", "spectacle", "import")
    return _entry(bool(path), path, reason)


def _screen_ocr() -> dict:
    path, reason = _command("tesseract")
    if not path:
        return _entry(False, None, reason)
    capture = _screen_capture()
    if not capture["available"]:
        return _entry(False, None, capture["reason"])
    return _entry(
        True,
        "tesseract",
        capture["reason"] if capture["degraded"] else None,
        degraded=bool(capture["degraded"]),
    )


def _display_brightness() -> dict:
    path, reason = _command("brightnessctl")
    return _entry(bool(path), path, reason)


def _display_power() -> dict:
    desktop_name = desktop.desktop_environment()
    if desktop_name == "gnome":
        path, reason = _command("gdbus")
        return _entry(bool(path), "gnome-ScreenSaver" if path else None, reason)
    if desktop_name == "kde":
        path, reason = _command("gdbus")
        if path:
            return _entry(True, "kde-ScreenSaver")
        if not _wayland():
            path, reason = _command("xset")
            return _entry(bool(path), path, reason)
        return _entry(False, None, reason)
    if _wayland():
        return _entry(
            False,
            None,
            f"当前桌面（{desktop_name}）在 Wayland 下没有可用的显示器开关接口",
        )
    path, reason = _command("xset")
    return _entry(bool(path), path, reason)


def _audio_control() -> dict:
    path, reason = _command("wpctl")
    return _entry(bool(path), path, reason)


def _audio_device_query() -> dict:
    path, reason = _command("wpctl", "pactl")
    return _entry(bool(path), path, reason)


def _media_control() -> dict:
    path, reason = _command("playerctl")
    return _entry(bool(path), path, reason)


def _bluetooth_control() -> dict:
    path, reason = _command("bluetoothctl")
    return _entry(bool(path), "bluetoothctl" if path else None, reason)


def _session_lock() -> dict:
    path, reason = _command("loginctl")
    return _entry(bool(path), path, reason)


def _idle_query() -> dict:
    backend, reason = session.idle_probe()
    return _entry(bool(backend), backend, reason)


def _tts() -> dict:
    path, reason = _command("spd-say")
    return _entry(bool(path), path, reason)


def _power_profile() -> dict:
    path, reason = _command("powerprofilesctl")
    return _entry(bool(path), path, reason)


def _tray() -> dict:
    if _wayland():
        return _entry(False, None, "Wayland 没有统一的系统托盘协议支持")
    try:
        import pystray  # noqa: F401
        return _entry(True, "pystray-xembed")
    except ImportError:
        return _entry(False, None, "未安装 pystray")
    except Exception as error:
        return _entry(False, None, f"pystray 后端不可用: {error}")


def _file_lock() -> dict:
    try:
        import psutil
    except ImportError:
        return _entry(False, None, "未安装 psutil，无法检查编辑进程打开的文档")
    return _entry(True, "psutil-open-files", "扫描可访问编辑进程的文档句柄，不检查其他进程的独占锁", degraded=True)


def _power_state() -> dict:
    import importlib.util

    if importlib.util.find_spec("psutil") is None:
        return _entry(False, None, "未安装 psutil，无法查询电池")
    resume = importlib.util.find_spec("dbus_next") is not None
    return _entry(True, "psutil/logind", None if resume else "未安装 dbus-next，睡眠恢复监听不可用", degraded=not resume)


def _power_system() -> dict:
    for name in ("systemctl", "loginctl"):
        path, reason = _command(name)
        if not path:
            return _entry(False, None, reason)
    return _entry(True, "systemd-logind", "系统授权和抑制器在执行时检查")


def _wifi() -> dict:
    path, reason = _command("nmcli")
    return _entry(bool(path), path, reason)


def _desktop_wallpaper() -> dict:
    if desktop.desktop_environment() == "gnome":
        path, reason = _command("gsettings")
    elif desktop.desktop_environment() == "kde":
        path, reason = _command("plasma-apply-wallpaperimage")
    else:
        return _entry(False, None, "当前桌面没有可用的壁纸后端")
    return _entry(bool(path), path, reason)


def _desktop_shortcut() -> dict:
    return _entry(True, "desktop-entry", "自定义目录缺少接口参数，仅支持 desktop/start_menu/startup", degraded=True)


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
    ids.SESSION_STATE: _session_lock,
    ids.POWER_STATE: _power_state,
    ids.POWER_SYSTEM: _power_system,
    ids.WIFI_QUERY: _wifi,
    ids.WIFI_CONTROL: _wifi,
    ids.DESKTOP_WALLPAPER: _desktop_wallpaper,
    ids.DESKTOP_SHORTCUT: _desktop_shortcut,
}
