"""bluetooth_toggle 动作插件的 WinRT 查询、PnP 回退与脚本生成测试。"""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用 WinRT/PnP")


def load_module():
    path = PKG_ROOT / "bundled" / "actions" / "bluetooth_toggle" / "action.py"
    spec = importlib.util.spec_from_file_location("bluetooth_toggle_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@WINDOWS_ONLY
def test_query_uses_winrt_when_available(monkeypatch):
    module = load_module()
    payload = {
        "method": "winrt_radio",
        "radios": [{"name": "Intel Bluetooth", "state": "On"}],
    }
    monkeypatch.setattr(
        module, "_run_powershell", lambda script: (0, json.dumps(payload), "")
    )

    result = module.run({}, {"action": "query"})

    assert result == {
        "ok": True,
        "method": "winrt_radio",
        "state": "on",
        "radios": payload["radios"],
    }


@WINDOWS_ONLY
def test_falls_back_to_elevated_pnp_and_verifies_state(monkeypatch, tmp_path):
    module = load_module()
    # WinRT 查询被策略拒绝，触发提权 PnP 回退
    monkeypatch.setattr(
        module, "_run_powershell", lambda script: (1, "", "WinRT Radio API access denied")
    )

    result_path = str(tmp_path / "bluetooth-result.json")

    def fake_mkstemp(prefix="", suffix=""):
        fd = os.open(result_path, os.O_CREAT | os.O_RDWR)
        return fd, result_path

    def fake_run_as_admin(command, timeout=None):
        pnp_result = {
            "method": "pnp_adapter",
            "state": "off",
            "before": [{"instance_id": "USB\\VID_8087", "name": "适配器", "status": "OK", "problem": 0}],
            "adapters": [{"instance_id": "USB\\VID_8087", "name": "适配器", "status": "Error", "problem": 22}],
        }
        with open(result_path, "w", encoding="utf-8") as result_file:
            json.dump(pnp_result, result_file)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(module, "run_as_admin", fake_run_as_admin)

    result = module.run({}, {"action": "toggle"})

    assert result["ok"] is True
    assert result["method"] == "pnp_adapter"
    assert result["state"] == "off"
    assert result["winrt_error"] == "WinRT Radio API access denied"
    assert not os.path.exists(result_path)


# Windows 上 run() 走 WinRT/PnP，会真的碰蓝牙适配器
@pytest.mark.skipif(os.name != "posix", reason="仅 Linux 调用 bluetoothctl")
def test_linux_query_and_toggle_use_bluetoothctl(monkeypatch):
    module = load_module()
    commands = []
    responses = iter(["Powered: yes", "Powered: yes", "Powered: yes", "Powered: no"])

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[1] == "show":
            output = next(responses)
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.run({}, {"action": "query"}) == {
        "ok": True,
        "method": "bluetoothctl",
        "state": "on",
    }
    assert module.run({}, {"action": "toggle"}) == {
        "ok": True,
        "method": "bluetoothctl",
        "state": "off",
    }
    assert commands == [
        ["bluetoothctl", "show"],
        ["bluetoothctl", "show"],
        ["bluetoothctl", "show"],
        ["bluetoothctl", "power", "off"],
        ["bluetoothctl", "show"],
    ]


def test_pnp_script_filters_physical_adapters_and_writes_result_file():
    module = load_module()

    script = module._pnp_control_script("toggle", "C:/temp/result.json")
    assert "-match '^(USB|PCI|BTH)\\\\'" in script
    assert "-notmatch '^BTHENUM\\\\'" in script
    assert "Set-Content -LiteralPath 'C:/temp/result.json'" in script
    assert "'toggle'" in script

    # 路径中的单引号按 PowerShell 规则加倍转义
    quoted = module._pnp_control_script("on", "C:/it's/result.json")
    assert "'C:/it''s/result.json'" in quoted
    responses = iter(["Powered: yes", "Powered: yes", "Powered: yes", "Powered: no"])

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[1] == "show":
            output = next(responses)
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")
