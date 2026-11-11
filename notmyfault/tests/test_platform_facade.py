from unittest.mock import patch

import pytest

from notmyfault.platform.capabilities import CAPABILITY_IDS
from notmyfault.platform.errors import (
    BackendInvalidError,
    BackendMissingError,
    PlatformServiceError,
)
from notmyfault.platform.services import operations, platform

CALL_ARGS = {
    ("clipboard", "read"): (),
    ("clipboard", "write"): ("x",),
    ("clipboard", "clear"): (),
    ("keyboard", "type_text"): ("x",),
    ("keyboard", "send_hotkey"): (["ctrl", "c"],),
    ("audio", "set_volume"): (10,),
    ("audio", "set_muted"): (True,),
    ("audio", "devices"): (),
    ("audio", "default_devices"): (),
    ("audio", "set_default"): ("render", "1"),
    ("media", "send"): ("play_pause",),
    ("window", "list"): (),
    ("window", "titles"): (),
    ("window", "foreground"): (),
    ("window", "info"): ("0x1",),
    ("window", "monitors"): (),
    ("window", "show"): ("0x1", "restore"),
    ("window", "activate"): ("0x1",),
    ("window", "move"): ("0x1", {"x": 0, "y": 0, "width": 10, "height": 10}),
    ("window", "set_opacity"): ("0x1", 50),
    ("window", "close"): ("0x1",),
    ("window", "set_pinned"): ("pin", "active"),
    ("display", "set_brightness"): (10,),
    ("display", "set_power"): ("on",),
    ("screen", "capture"): ("a.png", "fullscreen", "png"),
    ("screen", "ocr"): ("a.png", "eng"),
    ("power", "set_profile"): ("balanced",),
    ("desktop", "file_in_use"): ("document.txt",),
    ("session", "lock"): (),
    ("session", "is_locked"): (),
    ("session", "idle_seconds"): (),
    ("session", "session_id"): (),
    ("power", "battery"): (),
    ("power", "resume_monitor"): (),
    ("power", "system"): ("sleep",),
    ("wifi", "current_ssid"): (),
    ("wifi", "connect"): ("network",),
    ("wifi", "disconnect"): (),
    ("bluetooth", "state"): (),
    ("bluetooth", "set"): (True,),
    ("speech", "speak"): ("文本",),
    ("desktop", "set_wallpaper"): ("image.png", "fill"),
    ("desktop", "create_shortcut"): ("program", "desktop", "program"),
    ("hotkey", "register"): (["ctrl", "a"],),
    ("hotkey", "capture"): (1.0,),
}


def available():
    return {"available": True, "backend": "fake", "reason": None, "degraded": False}


def fake_backend(error=None, calls=None):
    def method(name):
        def call(self, *args, **kwargs):
            if calls is not None:
                calls.append(name)
            if error is not None:
                raise error

        return call

    return type("Backend", (), {
        "__init__": lambda self, runner: None,
        "read": method("read"),
        "write": method("write"),
    })


def test_every_operation_uses_a_known_capability():
    found = operations()
    assert found
    assert {(domain, method) for domain, method, _ in found} == set(CALL_ARGS)
    for _domain, _method, capability in found:
        assert capability in CAPABILITY_IDS


def test_macos_reports_every_operation_unsupported():
    with patch("notmyfault.platform.current", return_value="macos"):
        for domain, method, capability in operations():
            assert platform.capability(capability).available is False
            with pytest.raises(PlatformServiceError) as caught:
                getattr(getattr(platform, domain), method)(*CALL_ARGS[(domain, method)])
            assert caught.value.kind == "unsupported"
            assert caught.value.capability == capability


@pytest.mark.parametrize("error,kind", [
    (BackendMissingError("缺命令"), "unavailable"),
    (BackendInvalidError("参数错"), "invalid"),
    (ValueError("参数错"), "invalid"),
    (PermissionError("拒绝"), "permission_denied"),
    (OSError("坏了"), "failed"),
    (RuntimeError("坏了"), "failed"),
])
def test_backend_errors_map_to_public_kind(error, kind):
    with (
        patch("notmyfault.platform.current", return_value="linux"),
        patch("notmyfault.platform.capabilities.probe_capability", return_value=available()),
        patch.dict("notmyfault.platform.linux.BACKENDS", {"clipboard": fake_backend(error)}),
    ):
        with pytest.raises(PlatformServiceError) as caught:
            platform.clipboard.write("x")
    assert caught.value.kind == kind
    assert caught.value.capability == "clipboard.write"
    assert caught.value.__cause__ is error


def test_unavailable_capability_does_not_call_backend():
    calls = []
    value = {"available": False, "backend": None, "reason": "未安装", "degraded": False}
    with (
        patch("notmyfault.platform.current", return_value="linux"),
        patch("notmyfault.platform.capabilities.probe_capability", return_value=value),
        patch.dict("notmyfault.platform.linux.BACKENDS", {"clipboard": fake_backend(calls=calls)}),
    ):
        with pytest.raises(PlatformServiceError) as caught:
            platform.clipboard.read()
    assert caught.value.kind == "unavailable"
    assert caught.value.reason == "未安装"
    assert calls == []


def test_error_round_trips_through_dict():
    original = PlatformServiceError("audio.control", "permission_denied", "拒绝")
    copy = PlatformServiceError.from_dict(original.to_dict())
    assert (copy.capability, copy.kind, copy.reason) == ("audio.control", "permission_denied", "拒绝")
    assert str(copy) == str(original)


def test_error_rejects_unknown_kind():
    with pytest.raises(ValueError):
        PlatformServiceError("audio.control", "backend_failed", "x")


@pytest.mark.parametrize("domain,method,args,lifecycle", [
    ("power", "resume_monitor", (), "start"),
    ("power", "resume_monitor", (), "consume_resume"),
    ("power", "resume_monitor", (), "stop"),
    ("hotkey", "register", (["ctrl", "a"],), "poll"),
    ("hotkey", "register", (["ctrl", "a"],), "close"),
])
def test_listener_lifecycle_preserves_public_error_kind(domain, method, args, lifecycle):
    class Handle:
        def fail(self):
            raise BackendMissingError("设备已断开")

        start = consume_resume = stop = poll = close = fail

    backend = type("Backend", (), {
        "__init__": lambda self, runner: None,
        method: lambda self, *args: Handle(),
    })
    with (
        patch("notmyfault.platform.current", return_value="linux"),
        patch("notmyfault.platform.capabilities.probe_capability", return_value=available()),
        patch.dict("notmyfault.platform.linux.BACKENDS", {domain: backend}),
    ):
        handle = getattr(getattr(platform, domain), method)(*args)
        with pytest.raises(PlatformServiceError) as caught:
            getattr(handle, lifecycle)()
    assert caught.value.kind == "unavailable"
    assert caught.value.capability == ("power.state" if domain == "power" else "input.global_hotkey")
