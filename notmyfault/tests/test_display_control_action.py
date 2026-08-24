"""display_control 动作插件的亮度控制与错误传播测试。"""

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = PKG_ROOT / "actions" / "display_control" / "action.py"
    spec = importlib.util.spec_from_file_location("display_control_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_does_not_gate_power_actions_on_brightness():
    path = PKG_ROOT / "actions" / "display_control" / "action.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    assert "display.brightness" not in meta.get("requires_capabilities", [])


def _failing_setter(message):
    def setter(level):
        raise RuntimeError(message)
    return setter


WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="仅 Windows 走 WMI/DDC")


@WINDOWS_ONLY
def test_set_brightness_succeeds_when_wmi_is_supported(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "_set_wmi_brightness", lambda level: 1)
    monkeypatch.setattr(
        module, "_set_ddc_brightness", _failing_setter("不支持 DDC/CI")
    )

    result = module.run({}, {"action": "set_brightness", "brightness": 40})

    assert result == {
        "brightness": 40,
        "methods": ["WMI 1 台"],
        "warnings": ["DDC/CI: 不支持 DDC/CI"],
    }


@WINDOWS_ONLY
def test_set_brightness_fails_when_no_backend_can_verify_change(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "_set_wmi_brightness", _failing_setter("无 WMI 显示器"))
    monkeypatch.setattr(module, "_set_ddc_brightness", _failing_setter("无 DDC/CI 显示器"))

    with pytest.raises(RuntimeError, match="不支持可验证的亮度控制"):
        module.run({}, {"action": "set_brightness", "brightness": 40})


def test_linux_brightness_missing_backend_raises_runtime_error(monkeypatch):
    module = load_module()

    class MissingDisplayBackend:
        def __init__(self, runner):
            pass

        def set_brightness(self, brightness):
            from notmyfault.platform.backends import BackendMissingError

            raise BackendMissingError("依赖缺失：亮度控制需要 brightnessctl")

    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(module, "DisplayBackend", MissingDisplayBackend)
    with pytest.raises(RuntimeError, match="依赖缺失"):
        module.run({}, {"action": "set_brightness", "brightness": 40})


def test_linux_brightness_sets_detected_tool(monkeypatch):
    module = load_module()
    calls = []

    class FakeDisplayBackend:
        def __init__(self, runner):
            calls.append(runner)

        def set_brightness(self, brightness):
            calls.append(brightness)

    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(module, "DisplayBackend", FakeDisplayBackend)
    assert module.run({}, {"action": "set_brightness", "brightness": 40}) == {
        "action": "set_brightness",
    }
    assert calls == [module.default_runner, 40]


def test_windows_brightness_action_returns_verified_result(monkeypatch):
    module = load_module()
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="40,42", stderr="")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: completed)

    assert module._set_wmi_brightness(40) == 2


def test_windows_brightness_action_propagates_failure(monkeypatch):
    module = load_module()
    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="未发现支持 WMI 亮度控制的显示器"
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: completed)

    with pytest.raises(RuntimeError, match="未发现支持 WMI 亮度控制的显示器"):
        module._set_wmi_brightness(40)


def test_unknown_display_action_is_not_reported_as_success():
    module = load_module()

    with pytest.raises(ValueError, match="不支持的显示器操作"):
        module.run({}, {"action": "flip"})
