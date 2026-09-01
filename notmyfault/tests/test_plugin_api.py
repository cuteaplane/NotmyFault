from unittest.mock import patch

import pytest

from notmyfault.plugin_api import (
    CapabilityStatus,
    PlatformServiceError,
    platform_services,
)


def available(backend="test-backend"):
    return {
        "available": True,
        "backend": backend,
        "reason": None,
        "degraded": False,
    }


def test_capability_returns_public_status():
    with patch(
        "notmyfault.platform.capabilities.probe_capability",
        return_value=available("/usr/bin/wl-copy"),
    ):
        status = platform_services().capability("clipboard.write")
    assert status == CapabilityStatus(
        id="clipboard.write",
        available=True,
        backend="/usr/bin/wl-copy",
        reason=None,
        degraded=False,
    )


def test_unknown_capability_uses_public_error():
    with pytest.raises(PlatformServiceError) as caught:
        platform_services().capability("third.party.unknown")
    assert caught.value.capability == "third.party.unknown"
    assert caught.value.kind == "unsupported"


def test_service_calls_linux_implementation_without_public_backend():
    calls = []

    class Clipboard:
        def write_text(self, text):
            calls.append(text)

    with (
        patch("notmyfault.platform.services.os.name", "posix"),
        patch("notmyfault.platform.services.sys.platform", "linux"),
        patch(
            "notmyfault.platform.capabilities.probe_capability",
            return_value=available(),
        ),
        patch(
            "notmyfault.platform.services._linux_backend",
            return_value=Clipboard(),
        ),
    ):
        services = platform_services()
        services.write_clipboard("hello")

    assert calls == ["hello"]
    assert not hasattr(services, "backend")


def test_unavailable_service_uses_capability_reason():
    value = {
        "available": False,
        "backend": None,
        "reason": "未安装 wl-copy",
        "degraded": False,
    }
    with patch(
        "notmyfault.platform.capabilities.probe_capability",
        return_value=value,
    ):
        with pytest.raises(PlatformServiceError) as caught:
            platform_services().write_clipboard("hello")
    assert caught.value.capability == "clipboard.write"
    assert caught.value.kind == "unavailable"
    assert caught.value.reason == "未安装 wl-copy"


def test_internal_error_is_translated_to_public_error():
    from notmyfault.platform.backends import BackendPermissionDeniedError

    class DeniedClipboard:
        def write_text(self, text):
            raise BackendPermissionDeniedError(f"拒绝写入：{text}")

    with (
        patch("notmyfault.platform.services.os.name", "posix"),
        patch("notmyfault.platform.services.sys.platform", "linux"),
        patch(
            "notmyfault.platform.capabilities.probe_capability",
            return_value=available(),
        ),
        patch(
            "notmyfault.platform.services._linux_backend",
            return_value=DeniedClipboard(),
        ),
    ):
        with pytest.raises(PlatformServiceError) as caught:
            platform_services().write_clipboard("secret")

    assert caught.value.capability == "clipboard.write"
    assert caught.value.kind == "permission_denied"
    assert caught.value.reason == "拒绝写入：secret"
    assert isinstance(caught.value.__cause__, BackendPermissionDeniedError)
