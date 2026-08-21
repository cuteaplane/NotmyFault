"""display_control 动作插件的亮度控制与错误传播测试。"""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = PKG_ROOT / "actions" / "display_control" / "action.py"
    spec = importlib.util.spec_from_file_location("display_control_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


# Windows 上 run() 走 WMI/DDC，会真的改屏幕亮度
@pytest.mark.skipif(os.name != "posix", reason="仅 Linux 调用 brightnessctl")
def test_linux_brightness_missing_backend_raises_runtime_error(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="缺少亮度控制后端"):
        module.run({}, {"action": "set_brightness", "brightness": 40})


# Windows 上 run() 走 WMI/DDC，会真的改屏幕亮度
@pytest.mark.skipif(os.name != "posix", reason="仅 Linux 调用 brightnessctl")
def test_linux_brightness_sets_detected_tool(monkeypatch):
    module = load_module()
    commands = []
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/brightnessctl")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda cmd, **kwargs: commands.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert module.run({}, {"action": "set_brightness", "brightness": 40}) == {
        "action": "set_brightness",
    }
    assert commands == [["/usr/bin/brightnessctl", "set", "40%"]]


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
