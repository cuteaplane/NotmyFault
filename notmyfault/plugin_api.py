from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace


HOST_API_VERSION = 1


@dataclass(frozen=True)
class CapabilityStatus:
    id: str
    available: bool
    backend: str | None
    reason: str | None
    degraded: bool


class PlatformServiceError(RuntimeError):
    def __init__(self, capability: str, kind: str, reason: str) -> None:
        self.capability = capability
        self.kind = kind
        self.reason = reason
        super().__init__(f"{capability}: {reason}")


def platform_services():
    from notmyfault.platform.services import PlatformServices

    return PlatformServices()


def native_lock():
    from notmyfault.native import NATIVE_LOCK

    return NATIVE_LOCK


def platform_backend_api():
    from notmyfault.platform.backends import (
        BackendError,
        BackendMissingError,
        DisplayBackend,
        ScreenshotBackend,
        WindowBackend,
        default_runner,
    )

    return SimpleNamespace(
        BackendError=BackendError,
        BackendMissingError=BackendMissingError,
        DisplayBackend=DisplayBackend,
        ScreenshotBackend=ScreenshotBackend,
        WindowBackend=WindowBackend,
        default_runner=default_runner,
    )


def network_security_api():
    from notmyfault.security.network import resolve_public_http_url

    return SimpleNamespace(resolve_public_http_url=resolve_public_http_url)


def owned_value_api():
    from notmyfault.extensions.protocol import OwnedValueError, unpack_owned_value

    return SimpleNamespace(
        OwnedValueError=OwnedValueError,
        unpack_owned_value=unpack_owned_value,
    )


def native_keyboard_api():
    from notmyfault.native.keyboard import perform_key_event, validate_key_event

    return SimpleNamespace(
        perform_key_event=perform_key_event,
        validate_key_event=validate_key_event,
    )


def native_mouse_api():
    from notmyfault.native.mouse import (
        current_virtual_screen,
        perform_coordinate,
    )

    return SimpleNamespace(
        current_virtual_screen=current_virtual_screen,
        perform_coordinate=perform_coordinate,
    )


def native_uia_api():
    from notmyfault.native.uia import (
        DesktopElementError,
        capture_element_at,
        capture_element_under_cursor,
        capture_foreground_window,
        check_selector,
        focus_selector_window,
        focus_window_signature,
        is_focused_password_control,
        perform_selector,
        read_selector_text,
        validate_window_signature,
        wait_for_selector,
    )

    return SimpleNamespace(
        DesktopElementError=DesktopElementError,
        capture_element_at=capture_element_at,
        capture_element_under_cursor=capture_element_under_cursor,
        capture_foreground_window=capture_foreground_window,
        check_selector=check_selector,
        focus_selector_window=focus_selector_window,
        focus_window_signature=focus_window_signature,
        is_focused_password_control=is_focused_password_control,
        perform_selector=perform_selector,
        read_selector_text=read_selector_text,
        validate_window_signature=validate_window_signature,
        wait_for_selector=wait_for_selector,
    )


def native_input_recorder_api():
    from notmyfault.native.input_recorder import InputRecorder, build_macro_steps

    return SimpleNamespace(
        InputRecorder=InputRecorder,
        build_macro_steps=build_macro_steps,
    )


def engines_compatibility(meta: dict) -> tuple[bool, str]:
    engines = meta.get("engines") if isinstance(meta, dict) else None
    if not isinstance(engines, dict) or not engines:
        return True, ""
    required = engines.get("notmyfault_api")
    if required is None:
        return True, ""
    if required == HOST_API_VERSION:
        return True, ""
    return (
        False,
        f"插件要求宿主 API 版本 {required}，当前是 {HOST_API_VERSION}",
    )
