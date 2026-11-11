from __future__ import annotations

import notmyfault.platform

CLIPBOARD_READ = "clipboard.read"
CLIPBOARD_WRITE = "clipboard.write"
INPUT_SEND = "input.send"
INPUT_GLOBAL_HOTKEY = "input.global_hotkey"
WINDOW_ENUMERATE = "window.enumerate"
WINDOW_CONTROL = "window.control"
WINDOW_PIN = "window.pin"
SCREEN_CAPTURE = "screen.capture"
SCREEN_OCR = "screen.ocr"
DISPLAY_BRIGHTNESS = "display.brightness"
DISPLAY_POWER = "display.power"
AUDIO_CONTROL = "audio.control"
AUDIO_DEVICE_QUERY = "audio.device_query"
MEDIA_CONTROL = "media.control"
BLUETOOTH_CONTROL = "bluetooth.control"
SESSION_LOCK = "session.lock"
IDLE_QUERY = "idle.query"
TTS = "tts"
TRAY = "tray"
POWER_PROFILE = "power.profile"
FILE_LOCK = "file.lock"
SESSION_STATE = "session.state"
POWER_STATE = "power.state"
POWER_SYSTEM = "power.system"
WIFI_QUERY = "wifi.query"
WIFI_CONTROL = "wifi.control"
DESKTOP_WALLPAPER = "desktop.wallpaper"
DESKTOP_SHORTCUT = "desktop.shortcut"

CAPABILITY_IDS = frozenset({
    CLIPBOARD_READ,
    CLIPBOARD_WRITE,
    INPUT_SEND,
    INPUT_GLOBAL_HOTKEY,
    WINDOW_ENUMERATE,
    WINDOW_CONTROL,
    WINDOW_PIN,
    SCREEN_CAPTURE,
    SCREEN_OCR,
    DISPLAY_BRIGHTNESS,
    DISPLAY_POWER,
    AUDIO_CONTROL,
    AUDIO_DEVICE_QUERY,
    MEDIA_CONTROL,
    BLUETOOTH_CONTROL,
    SESSION_LOCK,
    IDLE_QUERY,
    TTS,
    TRAY,
    POWER_PROFILE,
    FILE_LOCK,
    SESSION_STATE,
    POWER_STATE,
    POWER_SYSTEM,
    WIFI_QUERY,
    WIFI_CONTROL,
    DESKTOP_WALLPAPER,
    DESKTOP_SHORTCUT,
})


def entry(available: bool, backend: str | None, reason: str | None = None,
          degraded: bool = False) -> dict:
    return {
        "available": available,
        "backend": backend,
        "reason": reason,
        "degraded": degraded,
    }


def _probes() -> dict:
    name = notmyfault.platform.current()
    if name == "linux":
        from notmyfault.platform.linux.probes import PROBES
    elif name == "windows":
        from notmyfault.platform.windows.probes import PROBES
    else:
        return {}
    return PROBES


def probe_capability(capability: str) -> dict:
    if capability not in CAPABILITY_IDS:
        raise ValueError(f"未知能力 id: {capability!r}")
    probe = _probes().get(capability)
    if probe is None:
        return entry(False, None, "当前平台暂未提供此能力")
    return probe()


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


def platform_report() -> dict:
    """诊断页用的平台报告，capabilities 每项带 available / backend / reason / degraded"""
    name = notmyfault.platform.current()
    desktop_name = session = None
    limitations: dict = {}
    if name == "linux":
        from notmyfault.platform.linux import desktop

        desktop_name = desktop.desktop_environment()
        session = desktop.session_type()
        limitations = {
            "global_hotkey": (
                "Wayland 不允许普通应用全局监听按键" if session == "wayland" else None
            ),
            "window_titles": (
                "Wayland 不允许普通应用枚举其他应用窗口标题" if session == "wayland" else None
            ),
            "display_power": (
                None
                if desktop_name in ("gnome", "kde") or session != "wayland"
                else "非 GNOME/KDE 的 Wayland 会话没有可用的显示器开关接口"
            ),
        }
    return {
        "platform": name,
        "desktop": desktop_name,
        "session_type": session,
        "capabilities": probe_capabilities(),
        "limitations": limitations,
    }
