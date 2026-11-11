import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from notmyfault.core.workflow import ActionCancellation, ActionCancelled
from notmyfault.platform.errors import BackendFailedError, BackendPermissionDeniedError
from notmyfault.tests.plugin_support import load_plugin


def test_unknown_action_is_rejected_before_platform_dispatch():
    module = load_plugin("actions", "bluetooth_toggle")
    with pytest.raises(ValueError, match="不支持的蓝牙操作"):
        module.run_with_context({}, {"action": "reset-adapter"}, {})


def test_toggle_reverses_the_current_state_and_preserves_query(monkeypatch):
    module = load_plugin("actions", "bluetooth_toggle")
    state = ["on"]

    def set_state(enabled):
        target = "on" if enabled else "off"
        changed = state[0] != target
        state[0] = target
        return {"state": target, "changed": changed, "method": "device"}

    monkeypatch.setattr(module.platform.bluetooth, "state", lambda: state[0])
    monkeypatch.setattr(module.platform.bluetooth, "set", set_state)
    monkeypatch.setattr(module.platform, "capability", lambda _: SimpleNamespace(backend="device"))
    assert module.run_with_context({}, {"action": "toggle"}, {}) == {
        "action": "toggle", "state": "off", "changed": True, "method": "device",
    }
    assert module.run_with_context({}, {"action": "toggle"}, {})["state"] == "on"
    assert module.run_with_context({}, {"action": "on"}, {})["changed"] is False
    assert module.run_with_context({}, {"action": "query"}, {}) == {
        "action": "query", "state": "on", "changed": False, "method": "device",
    }


@pytest.mark.parametrize("cancel_before", [True, False])
def test_cancellation_does_not_change_bluetooth_after_query(monkeypatch, cancel_before):
    module = load_plugin("actions", "bluetooth_toggle")
    event = threading.Event()
    changes = []

    def query():
        event.set()
        return "on"

    if cancel_before:
        event.set()
    monkeypatch.setattr(module.platform.bluetooth, "state", query)
    monkeypatch.setattr(module.platform.bluetooth, "set", changes.append)
    with pytest.raises(ActionCancelled):
        module.run_with_context({}, {"action": "toggle"}, {
            "runtime": {"cancellation": ActionCancellation(event)},
        })
    assert changes == []


@pytest.mark.parametrize("payload,kind", [
    ('{"ok":false,"code":"access_denied","detail":"DeniedBySystem"}', "permission_denied"),
    ("unexpected output", "failed"),
])
def test_windows_bluetooth_failure_preserves_error_kind(payload, kind):
    from notmyfault.platform.windows.bluetooth import BluetoothBackend

    runner = SimpleNamespace(run=lambda *args, **kwargs: SimpleNamespace(returncode=4, stdout=payload, stderr=""))
    with pytest.raises((BackendPermissionDeniedError, BackendFailedError)) as caught:
        BluetoothBackend(runner).set(True)
    assert caught.value.kind == kind


def test_linux_bluetooth_change_uses_one_total_deadline(monkeypatch):
    from notmyfault.platform.linux import bluetooth

    now = [0.0]
    timeouts = []

    def run(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        now[0] += kwargs["timeout"]
        return SimpleNamespace(returncode=0, stdout="Powered: yes\n", stderr="")

    monkeypatch.setattr(bluetooth, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    ))
    runner = SimpleNamespace(which=lambda *names: "/fake/bluetoothctl", run=run)
    with pytest.raises(BackendFailedError, match="总时限"):
        bluetooth.BluetoothBackend(runner).set(False)
    assert now[0] == pytest.approx(30)
    assert all(0 < timeout <= 8 for timeout in timeouts)


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 自带 Windows PowerShell")
@pytest.mark.system_native
def test_windows_helper_returns_structured_query_result():
    helper = Path(__file__).resolve().parents[1] / "platform/windows/radio.ps1"
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-File", str(helper), "-Action", "query"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is (result.returncode == 0)
    if result.returncode == 0:
        assert payload["method"] == "winrt"
        assert payload["state"] in {"on", "off", "disabled", "mixed"}
    else:
        assert payload["code"] in {"no_radio", "winrt_unavailable"}
