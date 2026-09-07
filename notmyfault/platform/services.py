from __future__ import annotations

import os
import sys

from notmyfault.plugin_api import CapabilityStatus, PlatformServiceError


def _platform_name() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform


def _linux_backend(capability: str):
    from notmyfault.platform.backends import (
        AudioBackend,
        ClipboardBackend,
        DisplayBackend,
        InputBackend,
        ScreenshotBackend,
        WindowBackend,
        default_runner,
    )
    from notmyfault.platform.capabilities import (
        AUDIO_CONTROL,
        AUDIO_DEVICE_QUERY,
        CLIPBOARD_READ,
        CLIPBOARD_WRITE,
        DISPLAY_BRIGHTNESS,
        INPUT_SEND,
        SCREEN_CAPTURE,
        WINDOW_PIN,
    )

    factories = {
        CLIPBOARD_READ: lambda: ClipboardBackend(default_runner),
        CLIPBOARD_WRITE: lambda: ClipboardBackend(default_runner),
        INPUT_SEND: lambda: InputBackend(default_runner),
        AUDIO_CONTROL: lambda: AudioBackend(default_runner),
        AUDIO_DEVICE_QUERY: lambda: AudioBackend(default_runner),
        WINDOW_PIN: lambda: WindowBackend(default_runner),
        DISPLAY_BRIGHTNESS: lambda: DisplayBackend(default_runner),
        SCREEN_CAPTURE: lambda: ScreenshotBackend(default_runner),
    }
    factory = factories.get(capability)
    if factory is None:
        raise PlatformServiceError(
            capability,
            "unsupported",
            "当前宿主没有开放这个平台功能",
        )
    return factory()


class PlatformServices:
    @property
    def platform(self) -> str:
        return _platform_name()

    def capability(self, capability: str) -> CapabilityStatus:
        from notmyfault.platform.capabilities import probe_capability

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

        status = self.capability(capability)
        if not status.available:
            raise PlatformServiceError(
                capability,
                "unavailable",
                status.reason or "当前系统不可用",
            )
        if self.platform != "linux":
            raise PlatformServiceError(
                capability,
                "unsupported",
                "当前宿主没有开放这个平台功能",
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
