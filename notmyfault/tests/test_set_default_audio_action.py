"""默认音频设备动作的参数、输出与 Windows 角色回读。"""

import importlib.util
import sys
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest

from notmyfault.platform.windows.audio import AudioBackend


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "set_default_audio"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("set_default_audio_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rejects_empty_device_and_bad_type():
    module = load_module()
    with pytest.raises(ValueError, match="必须填写目标设备"):
        module.run_with_context({}, {"device_type": "render", "device": " "}, {})
    with pytest.raises(ValueError, match="无效的设备类型"):
        module.run_with_context({}, {"device_type": "output", "device": "42"}, {})


@pytest.mark.parametrize("device_type", ["render", "capture"])
@pytest.mark.parametrize("initial_mask,failed_role", [(7, None), *product(range(7), (None, 0, 1, 2))])
def test_windows_sets_and_verifies_every_default_device_role(monkeypatch, device_type, initial_mask, failed_role):
    defaults = {role: "target" if initial_mask & (1 << role) else "old" for role in (0, 1, 2)}
    previous = defaults[0]
    calls, queries, initialized = [], [], []

    def default_endpoint(flow, role):
        assert flow == (0 if device_type == "render" else 1)
        queries.append(role)
        return SimpleNamespace(GetId=lambda: defaults[role])

    collection = SimpleNamespace(GetCount=lambda: 1, Item=lambda index: SimpleNamespace(GetId=lambda: "target"))
    enumerator = SimpleNamespace(
        GetDefaultAudioEndpoint=default_endpoint,
        EnumAudioEndpoints=lambda flow, state: collection,
    )
    utilities = SimpleNamespace(
        GetDeviceEnumerator=lambda: enumerator,
        GetAllDevices=lambda: [SimpleNamespace(id="target", FriendlyName="目标设备")],
    )

    def set_default(target, role):
        calls.append((target, role))
        defaults[role] = "unapplied" if role == failed_role else target
        return 0

    policy = SimpleNamespace(SetDefaultEndpoint=set_default)
    monkeypatch.setitem(sys.modules, "comtypes", SimpleNamespace(
        CoInitialize=lambda: initialized.append(True),
        CoUninitialize=lambda: initialized.pop(),
        COMError=RuntimeError,
        IUnknown=object,
        GUID=lambda value: value,
        COMMETHOD=lambda *args: args,
        HRESULT=object,
        CLSCTX_ALL=23,
        CoCreateInstance=lambda clsid, interface, context: policy,
    ))
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", SimpleNamespace(AudioUtilities=utilities))
    backend = AudioBackend()
    if failed_role is not None:
        with pytest.raises(RuntimeError, match="回读为"):
            backend.set_default(device_type, "target")
    else:
        result = backend.set_default(device_type, "目标设备")
        assert result == {
            "device_id": "target", "device_name": "目标设备", "previous_device_id": previous,
            "changed": initial_mask != 7, "method": "PolicyConfig",
        }
        assert set(defaults.values()) == {"target"}
    assert calls == ([] if initial_mask == 7 else [("target", role) for role in (0, 1, 2)])
    assert set(queries) == {0, 1, 2}
    assert initialized == []
