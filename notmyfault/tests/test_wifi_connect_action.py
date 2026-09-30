"""wifi_connect 动作的参数、系统调用和状态确认测试。"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from notmyfault.security.plugin_schema import validate_plugin_meta


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "wifi_connect"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("wifi_connect_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed(*, returncode=0, stdout=b"", stderr=b""):
    if isinstance(stdout, str):
        stdout = stdout.encode()
    if isinstance(stderr, str):
        stderr = stderr.encode()
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def completed_text(*, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_manifest_is_valid_cross_platform_action():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    valid, errors = validate_plugin_meta(meta, "action")
    assert valid, errors
    assert meta["id"] == "wifi_connect"
    assert meta["platforms"] == ["windows", "linux"]
    assert "external_binary" in meta["permissions"]
    assert "security" not in meta


def test_connect_requires_ssid():
    module = load_module()
    with pytest.raises(ValueError, match="必须填写目标 SSID"):
        module.run({}, {"action": "connect", "ssid": "  "})


def test_unknown_action_rejected():
    module = load_module()
    with pytest.raises(ValueError, match="不支持的 WiFi 操作"):
        module.run({}, {"action": "scan"})


def test_windows_connect_uses_netsh_and_waits_for_ssid(monkeypatch):
    module = load_module()
    calls = []
    states = ["OldNet", "OldNet", "HomeWiFi"]

    def fake_current():
        return states.pop(0) if states else "HomeWiFi"

    def fake_run(command, **kwargs):
        calls.append(command)
        return completed(stdout=b"Connection request was completed successfully.\n")

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_current_ssid_windows", fake_current)
    monkeypatch.setattr(module, "_decode_netsh", lambda raw: raw.decode("utf-8", errors="replace"))
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    result = module.run({}, {"action": "connect", "ssid": "HomeWiFi"})

    assert result["ssid"] == "HomeWiFi"
    assert result["previous_ssid"] == "OldNet"
    assert result["connected"] is True
    assert result["changed"] is True
    assert result["method"] == "netsh"
    assert calls[0][:3] == ["netsh", "wlan", "connect"]
    assert "name=HomeWiFi" in calls[0]
    assert "ssid=HomeWiFi" in calls[0]


def test_windows_connect_skips_when_already_on_target(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_current_ssid_windows", lambda: "HomeWiFi")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("不应再调用 netsh"),
    )
    result = module.run({}, {"action": "connect", "ssid": "HomeWiFi"})
    assert result == {
        "action": "connect",
        "ssid": "HomeWiFi",
        "previous_ssid": "HomeWiFi",
        "connected": True,
        "changed": False,
        "method": "netsh",
    }


def test_windows_disconnect(monkeypatch):
    module = load_module()
    states = ["HomeWiFi", "HomeWiFi", ""]

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(
        module,
        "_current_ssid_windows",
        lambda: states.pop(0) if states else "",
    )
    monkeypatch.setattr(module, "_decode_netsh", lambda raw: raw.decode())
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: completed(stdout=b"Disconnection completed."),
    )
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    result = module.run({}, {"action": "disconnect"})
    assert result["connected"] is False
    assert result["previous_ssid"] == "HomeWiFi"
    assert result["changed"] is True


def test_linux_connect_uses_saved_connection(monkeypatch):
    module = load_module()
    calls = []
    states = ["Cafe", "Cafe", "HomeWiFi"]

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:4] == ["-t", "-f", "NAME,TYPE"]:
            return completed_text(stdout="HomeWiFi:802-11-wireless\n")
        if command[1:3] == ["connection", "up"]:
            return completed_text(stdout="Connection successfully activated\n")
        return completed_text()

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(
        module,
        "_current_ssid_linux",
        lambda: states.pop(0) if states else "HomeWiFi",
    )
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    result = module.run({}, {"action": "connect", "ssid": "HomeWiFi"})
    assert result["method"] == "nmcli"
    assert result["ssid"] == "HomeWiFi"
    assert result["changed"] is True
    assert ["connection", "up", "id", "HomeWiFi"] == calls[1][1:]


def test_linux_connect_with_password_for_new_network(monkeypatch):
    module = load_module()
    calls = []
    states = ["", "", "NewNet"]

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:4] == ["-t", "-f", "NAME,TYPE"]:
            return completed_text(stdout="")
        if command[1:4] == ["device", "wifi", "connect"]:
            return completed_text(stdout="Device 'wlan0' successfully activated\n")
        return completed_text()

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(
        module,
        "_current_ssid_linux",
        lambda: states.pop(0) if states else "NewNet",
    )
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    result = module.run(
        {}, {"action": "connect", "ssid": "NewNet", "password": "secret"}
    )
    assert result["ssid"] == "NewNet"
    connect_cmd = calls[1]
    assert connect_cmd[1:] == [
        "device", "wifi", "connect", "NewNet", "password", "secret",
    ]


def test_linux_disconnect_uses_wifi_device(monkeypatch):
    module = load_module()
    states = ["HomeWiFi", "HomeWiFi", ""]
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:4] == ["-t", "-f", "DEVICE,TYPE,STATE"]:
            return completed_text(stdout="wlan0:wifi:connected\neth0:ethernet:connected\n")
        if command[1:3] == ["device", "disconnect"]:
            return completed_text(stdout="Device 'wlan0' successfully disconnected.\n")
        return completed_text()

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(
        module,
        "_current_ssid_linux",
        lambda: states.pop(0) if states else "",
    )
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    result = module.run({}, {"action": "disconnect"})
    assert result["changed"] is True
    assert result["connected"] is False
    assert calls[1][1:] == ["device", "disconnect", "wlan0"]
