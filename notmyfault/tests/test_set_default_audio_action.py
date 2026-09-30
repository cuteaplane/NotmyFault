"""set_default_audio 动作的参数与 Linux 平台服务测试。"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from notmyfault.plugin_api import PlatformServiceError
from notmyfault.security.plugin_schema import validate_plugin_meta


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "set_default_audio"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("set_default_audio_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_requires_audio_device_query():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    valid, errors = validate_plugin_meta(meta, "action")
    assert valid, errors
    assert meta["id"] == "set_default_audio"
    assert meta["requires_capabilities"] == ["audio.device_query"]
    assert meta["platforms"] == ["windows", "linux"]
    assert "audio" in meta["permissions"]


def test_rejects_empty_device_and_bad_type():
    module = load_module()
    with pytest.raises(ValueError, match="必须填写目标设备"):
        module.run({}, {"device_type": "render", "device": " "})
    with pytest.raises(ValueError, match="无效的设备类型"):
        module.run({}, {"device_type": "output", "device": "42"})


def test_linux_sets_default_via_platform_services(monkeypatch):
    module = load_module()
    calls = []
    devices = {"render": "12", "capture": "55"}

    class FakeServices:
        def default_audio_devices(self):
            return dict(devices)

        def set_default_audio_device(self, flow, device_id):
            calls.append((flow, device_id))
            devices[flow] = device_id

        def capability(self, capability):
            return SimpleNamespace(backend="/usr/bin/wpctl")

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(
        "notmyfault.plugin_api.platform_services",
        lambda: FakeServices(),
    )

    result = module.run({}, {"device_type": "render", "device": "41"})
    assert calls == [("render", "41")]
    assert result["device_id"] == "41"
    assert result["previous_device_id"] == "12"
    assert result["changed"] is True
    assert result["device_type"] == "render"


def test_linux_skips_when_already_default(monkeypatch):
    module = load_module()

    class FakeServices:
        def default_audio_devices(self):
            return {"render": "41", "capture": "55"}

        def set_default_audio_device(self, flow, device_id):
            raise AssertionError("不应再设置")

        def capability(self, capability):
            return SimpleNamespace(backend="pactl")

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(
        "notmyfault.plugin_api.platform_services",
        lambda: FakeServices(),
    )
    result = module.run({}, {"device_type": "render", "device": "41"})
    assert result["changed"] is False
    assert result["device_id"] == "41"


def test_linux_surfaces_platform_service_error(monkeypatch):
    module = load_module()

    class FakeServices:
        def default_audio_devices(self):
            raise PlatformServiceError(
                "audio.device_query", "unavailable", "未安装 wpctl 或 pactl"
            )

        def capability(self, capability):
            return SimpleNamespace(backend=None)

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(
        "notmyfault.plugin_api.platform_services",
        lambda: FakeServices(),
    )
    with pytest.raises(RuntimeError, match="未安装 wpctl 或 pactl"):
        module.run({}, {"device_type": "render", "device": "41"})


def test_linux_rejects_when_readback_mismatches(monkeypatch):
    module = load_module()

    class FakeServices:
        def default_audio_devices(self):
            return {"render": "12"}

        def set_default_audio_device(self, flow, device_id):
            return None

        def capability(self, capability):
            return SimpleNamespace(backend="wpctl")

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(
        "notmyfault.plugin_api.platform_services",
        lambda: FakeServices(),
    )
    with pytest.raises(RuntimeError, match="回读为"):
        module.run({}, {"device_type": "render", "device": "41"})
