"""bluetooth_toggle 动作的参数、系统调用和状态确认测试。"""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from notmyfault.security.plugin_schema import validate_plugin_meta


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "bluetooth_toggle"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("bluetooth_toggle_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed(*, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_manifest_is_a_non_admin_optional_action():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))

    valid, errors = validate_plugin_meta(meta, "action")

    assert valid, errors
    assert meta["permissions"] == ["external_binary", "native_api"]
    assert meta["platforms"] == ["windows", "linux"]
    assert meta["requires_capabilities"] == ["bluetooth.control"]
    assert "security" not in meta


def test_unknown_action_is_rejected_before_platform_dispatch(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "_run_windows", lambda action: pytest.fail(action))

    with pytest.raises(ValueError, match="不支持的蓝牙操作"):
        module.run({}, {"action": "reset-adapter"})


def test_windows_query_uses_packaged_helper_and_returns_payload(monkeypatch):
    module = load_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return completed(
            stdout=(
                '{"ok":true,"action":"query","state":"on",'
                '"changed":false,"method":"winrt","radios":[]}'
            )
        )

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run({}, {"action": "query"})

    assert result == {
        "action": "query",
        "state": "on",
        "changed": False,
        "method": "winrt",
        "radios": [],
    }
    command, kwargs = calls[0]
    assert command[0].replace("/", "\\").endswith(
        r"System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    assert command[-2:] == ["-Action", "query"]
    assert Path(command[command.index("-File") + 1]).name == "radio.ps1"
    assert kwargs["timeout"] == 30


def test_windows_structured_failure_has_user_facing_message(monkeypatch):
    module = load_module()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: completed(
            returncode=4,
            stdout='{"ok":false,"code":"access_denied","detail":"DeniedBySystem"}',
        ),
    )

    with pytest.raises(RuntimeError, match="Windows 拒绝蓝牙控制权限：DeniedBySystem"):
        module._run_windows("on")


def test_windows_rejects_success_exit_without_json(monkeypatch):
    module = load_module()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: completed(stdout="unexpected output"),
    )

    with pytest.raises(RuntimeError, match="蓝牙辅助程序执行失败"):
        module._run_windows("query")


def test_linux_query_reads_powered_state(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/bluetoothctl")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: completed(stdout="Controller AA:BB\n\tPowered: yes\n"),
    )

    assert module.run({}, {"action": "query"}) == {
        "action": "query",
        "state": "on",
        "changed": False,
        "method": "bluetoothctl",
    }


def test_linux_toggle_changes_and_confirms_state(monkeypatch):
    module = load_module()
    commands = []
    shows = iter(("\tPowered: yes\n", "\tPowered: no\n"))

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[-1] == "show":
            return completed(stdout=next(shows))
        return completed(stdout="Changing power off succeeded\n")

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/bluetoothctl")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    assert module.run({}, {"action": "toggle"}) == {
        "action": "toggle",
        "state": "off",
        "changed": True,
        "method": "bluetoothctl",
    }
    assert commands == [
        ["/usr/bin/bluetoothctl", "show"],
        ["/usr/bin/bluetoothctl", "power", "off"],
        ["/usr/bin/bluetoothctl", "show"],
    ]


def test_linux_missing_controller_is_not_reported_as_off(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/bluetoothctl")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: completed(stdout="No default controller available\n"),
    )

    with pytest.raises(RuntimeError, match="没有返回默认蓝牙控制器"):
        module._run_linux("query")


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 自带 Windows PowerShell")
def test_windows_helper_returns_structured_query_result():
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PLUGIN_ROOT / "radio.ps1"),
            "-Action",
            "query",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is (result.returncode == 0)
    if result.returncode == 0:
        assert payload["method"] == "winrt"
        assert payload["state"] in {"on", "off", "disabled", "mixed"}
    else:
        assert payload["code"] in {"no_radio", "winrt_unavailable"}
