from __future__ import annotations

import os
import sys

from notmyfault.plugin_api import CapabilityStatus, PlatformServiceError

_SERVICE_BACKENDS = {
    "linux": {
        "clipboard.read": "ClipboardBackend",
        "clipboard.write": "ClipboardBackend",
        "input.send": "InputBackend",
        "audio.control": "AudioBackend",
        "audio.device_query": "AudioBackend",
        "window.pin": "WindowBackend",
        "display.brightness": "DisplayBackend",
        "screen.capture": "ScreenshotBackend",
    },
}


def _platform_name() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform


def _linux_backend(capability: str):
    from notmyfault.platform import backends

    name = _SERVICE_BACKENDS["linux"].get(capability)
    if name is None:
        raise PlatformServiceError(capability, "unsupported", "当前宿主没有开放这个平台功能")
    return getattr(backends, name)(backends.default_runner)


class PlatformServices:
    @property
    def platform(self) -> str:
        return _platform_name()

    def capability(self, capability: str) -> CapabilityStatus:
        from notmyfault.platform.capabilities import CAPABILITY_IDS, probe_capability

        if capability not in CAPABILITY_IDS:
            raise PlatformServiceError(capability, "unsupported", f"未知能力 id: {capability!r}")
        if capability not in _SERVICE_BACKENDS.get(self.platform, {}):
            return CapabilityStatus(
                id=capability, available=False, backend=None,
                reason="当前宿主没有开放这个平台功能", degraded=False,
            )

        try:
            value = probe_capability(capability)
        except ValueError as error:
            raise PlatformServiceError(
                capability,
                "unsupported",
                str(error),
            ) from error
        return CapabilityStatus(
            id=capability,
            available=bool(value["available"]),
            backend=value.get("backend"),
            reason=value.get("reason"),
            degraded=bool(value.get("degraded")),
        )

    def _call(self, capability: str, method: str, *args):
        from notmyfault.platform.backends import BackendError

        if capability not in _SERVICE_BACKENDS.get(self.platform, {}):
            raise PlatformServiceError(capability, "unsupported", "当前宿主没有开放这个平台功能")
        status = self.capability(capability)
        if not status.available:
            raise PlatformServiceError(
                capability,
                "unavailable",
                status.reason or "当前系统不可用",
            )
        target = _linux_backend(capability)
        try:
            return getattr(target, method)(*args)
        except BackendError as error:
            raise PlatformServiceError(
                capability,
                error.kind,
                str(error),
            ) from error
        except (OSError, RuntimeError) as error:
            raise PlatformServiceError(
                capability,
                "permission_denied" if isinstance(error, PermissionError) else "backend_failed",
                str(error),
            ) from error

    def read_clipboard(self) -> str | None:
        return self._call("clipboard.read", "read_text")

    def write_clipboard(self, text: str) -> None:
        self._call("clipboard.write", "write_text", text)

    def type_text(self, text: str) -> None:
        self._call("input.send", "type_text", text)

    def send_hotkey(self, parts: list[str]) -> None:
        self._call("input.send", "send_hotkey", parts)

    def set_volume(self, percent: int) -> None:
        self._call("audio.control", "set_volume", percent)

    def set_mute(self, muted: bool) -> None:
        self._call("audio.control", "set_mute", muted)

    def default_audio_devices(self) -> dict[str, str]:
        return self._call("audio.device_query", "default_devices")

    def set_window_pinned(
        self,
        action: str,
        target: str,
        title: str = "",
    ) -> dict[str, str]:
        return self._call("window.pin", "set_pinned", action, target, title)

    def set_brightness(self, percent: int) -> None:
        self._call("display.brightness", "set_brightness", percent)

    def capture_screen(self, output_path: str, mode: str, fmt: str) -> str:
        return self._call("screen.capture", "capture", output_path, mode, fmt)
