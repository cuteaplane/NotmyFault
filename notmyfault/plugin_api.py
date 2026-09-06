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


def data_types_api():
    from notmyfault.core.data_types import (
        DataTypeError, copy_value, field_type, infer_type, normalize_type,
        normalize_value, type_at_path, types_compatible,
    )
    from notmyfault.core.type_registry import TypeRegistry
    from notmyfault.core.value_codec import decode_value, encode_value
    from notmyfault.core.value_conversion import convert_value

    return SimpleNamespace(
        DataTypeError=DataTypeError,
        TypeRegistry=TypeRegistry,
        normalize_type=normalize_type,
        normalize_value=normalize_value,
        convert_value=convert_value,
        types_compatible=types_compatible,
        type_at_path=type_at_path,
        field_type=field_type,
        infer_type=infer_type,
        copy_value=copy_value,
        encode_value=encode_value,
        decode_value=decode_value,
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
