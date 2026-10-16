"""set_default_audio 动作的参数、默认设备设置与回读。"""

import importlib.util
import json
import sys
from itertools import product
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
        module.run_with_context({}, {"device_type": "render", "device": " "}, {})
    with pytest.raises(ValueError, match="无效的设备类型"):
        module.run_with_context({}, {"device_type": "output", "device": "42"}, {})


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

    result = module.run_with_context({}, {"device_type": "render", "device": "41"}, {})
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
    result = module.run_with_context({}, {"device_type": "render", "device": "41"}, {})
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
        module.run_with_context({}, {"device_type": "render", "device": "41"}, {})


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
        module.run_with_context({}, {"device_type": "render", "device": "41"}, {})


@pytest.mark.parametrize("device_type", ["render", "capture"])
@pytest.mark.parametrize("initial_mask,failed_role", [(7, None), *product(range(7), (None, 0, 1, 2))])
def test_windows_sets_and_verifies_every_default_device_role(monkeypatch, device_type, initial_mask, failed_role):
    module = load_module()
    defaults = {role: "target" if initial_mask & (1 << role) else "old" for role in (0, 1, 2)}
    previous = defaults[0]
    calls, queries, initialized = [], [], []

    def default_endpoint(flow, role):
        assert flow == (0 if device_type == "render" else 1)
        queries.append(role)
        return SimpleNamespace(GetId=lambda: defaults[role])

    enumerator = SimpleNamespace(GetDefaultAudioEndpoint=default_endpoint)
    utilities = SimpleNamespace(GetDeviceEnumerator=lambda: enumerator)
    monkeypatch.setitem(sys.modules, "comtypes", SimpleNamespace(
        CoInitialize=lambda: initialized.append(True),
        CoUninitialize=lambda: initialized.pop(), COMError=RuntimeError,
    ))
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", SimpleNamespace(AudioUtilities=utilities))
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_resolve_windows_device", lambda flow, target: (target, "目标设备"))

    def set_default(target):
        calls.append(target)
        for role in defaults:
            defaults[role] = "unapplied" if role == failed_role else target

    monkeypatch.setattr(module, "_set_windows_default", set_default)
    if failed_role is not None:
        with pytest.raises(RuntimeError, match="回读为"):
            module.run_with_context({}, {"device_type": device_type, "device": "target"}, {})
    else:
        result = module.run_with_context({}, {"device_type": device_type, "device": "target"}, {})
        assert result["changed"] is (initial_mask != 7)
        assert result["device_id"] == "target"
        assert result["previous_device_id"] == previous
        assert set(defaults.values()) == {"target"}
    assert calls == ([] if initial_mask == 7 else ["target"])
    assert set(queries) == {0, 1, 2}
    assert initialized == []
