from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace
from threading import Event
from typing import Any, Callable, Mapping, Protocol

from notmyfault.core.run_context import RunContext
from notmyfault.values import Blob, Secret, action_values


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
        PowerProfileBackend,
        ScreenshotBackend,
        SleepResumeBackend,
        WindowBackend,
        default_runner,
    )

    return SimpleNamespace(
        BackendError=BackendError,
        BackendMissingError=BackendMissingError,
        DisplayBackend=DisplayBackend,
        PowerProfileBackend=PowerProfileBackend,
        ScreenshotBackend=ScreenshotBackend,
        SleepResumeBackend=SleepResumeBackend,
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


def engine_events_api():
    from notmyfault.core import engine_events

    stream = engine_events.current_stream()
    return SimpleNamespace(
        subscribe=stream.subscribe,
        unsubscribe=stream.unsubscribe,
        RULE_TERMINAL_EVENTS=engine_events.RULE_TERMINAL_EVENTS,
    )


class TriggerState(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any, *, durable: bool = False) -> None: ...


class TriggerClock(Protocol):
    def monotonic(self) -> float: ...

    def time(self) -> float: ...

    def wait(self, event: Event, timeout: float | None) -> bool: ...


def system_clock() -> TriggerClock:
    from notmyfault.core.clock import SystemClock

    return SystemClock()


@dataclass(frozen=True, slots=True)
class TriggerContext:
    meta: Mapping[str, Any]
    config: Mapping[str, Any]
    emit: Callable[[dict[str, Any]], bool]
    stopping: Event
    state: TriggerState
    boot_id: str
    engine_started_at: float
    clock: TriggerClock


_trigger_context = ContextVar("trigger_context", default=None)


def trigger_context():
    return _trigger_context.get()


@contextmanager
def use_trigger_context(context):
    token = _trigger_context.set(context)
    try:
        yield context
    finally:
        _trigger_context.reset(token)
