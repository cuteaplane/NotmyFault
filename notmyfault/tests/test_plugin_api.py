from unittest.mock import patch

import pytest

from notmyfault.plugin_api import (
    CapabilityStatus,
    PlatformServiceError,
    platform,
)


def available(backend="test-backend"):
    return {
        "available": True,
        "backend": backend,
        "reason": None,
        "degraded": False,
    }


def test_capability_returns_public_status():
    with patch("notmyfault.platform.current", return_value="linux"), patch(
        "notmyfault.platform.capabilities.probe_capability",
        return_value=available("/usr/bin/wl-copy"),
    ):
        status = platform.capability("clipboard.write")
    assert status == CapabilityStatus(
        id="clipboard.write",
        available=True,
        backend="/usr/bin/wl-copy",
        reason=None,
        degraded=False,
    )


def test_unknown_capability_uses_public_error():
    with pytest.raises(PlatformServiceError) as caught:
        platform.capability("third.party.unknown")
    assert caught.value.capability == "third.party.unknown"
    assert caught.value.kind == "invalid"


def test_unavailable_service_uses_capability_reason():
    value = {
        "available": False,
        "backend": None,
        "reason": "未安装 wl-copy",
        "degraded": False,
    }
    with patch("notmyfault.platform.current", return_value="linux"), patch(
        "notmyfault.platform.capabilities.probe_capability",
        return_value=value,
    ):
        with pytest.raises(PlatformServiceError) as caught:
            platform.clipboard.write("hello")
    assert caught.value.capability == "clipboard.write"
    assert caught.value.kind == "unavailable"
    assert caught.value.reason == "未安装 wl-copy"


def test_internal_error_is_translated_to_public_error():
    from notmyfault.platform.errors import BackendPermissionDeniedError

    class DeniedClipboard:
        def __init__(self, runner):
            pass

        def write(self, text):
            raise BackendPermissionDeniedError(f"拒绝写入：{text}")

    with (
        patch("notmyfault.platform.current", return_value="linux"),
        patch(
            "notmyfault.platform.capabilities.probe_capability",
            return_value=available(),
        ),
        patch.dict("notmyfault.platform.linux.BACKENDS", {"clipboard": DeniedClipboard}),
    ):
        with pytest.raises(PlatformServiceError) as caught:
            platform.clipboard.write("secret")

    assert caught.value.capability == "clipboard.write"
    assert caught.value.kind == "permission_denied"
    assert caught.value.reason == "拒绝写入：secret"
    assert isinstance(caught.value.__cause__, BackendPermissionDeniedError)


def test_screen_ocr_reports_missing_tesseract():
    value = {
        "available": False,
        "backend": None,
        "reason": "未安装 tesseract",
        "degraded": False,
    }
    with patch("notmyfault.platform.current", return_value="linux"), patch(
        "notmyfault.platform.capabilities.probe_capability",
        return_value=value,
    ):
        with pytest.raises(PlatformServiceError) as caught:
            platform.screen.ocr("shot.png", "eng")
    assert caught.value.capability == "screen.ocr"
    assert caught.value.kind == "unavailable"
    assert caught.value.reason == "未安装 tesseract"
