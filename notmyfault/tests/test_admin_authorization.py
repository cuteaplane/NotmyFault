"""管理员授权通知和代理命令校验。"""

import inspect
import importlib
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

from notmyfault.core.admin_session import AdminSessionManager
from notmyfault.security import admin_broker, admin_prompt


def test_admin_notification_default_is_two_minutes():
    default = inspect.signature(admin_prompt.confirm_admin_request).parameters["timeout"].default
    assert default == 120.0


def test_failure_actions_are_included_in_startup_authorization():
    manager = AdminSessionManager(
        sudo=SimpleNamespace(),
        engine_token="token",
        authorization_mode="engine_start",
        rules_fn=lambda: [{
            "event": {"type": "manual", "params": {}},
            "actions": [{
                "type": "ordinary_action",
                "params": {},
                "failure_actions": [{"type": "admin_action", "params": {}}],
            }],
        }],
        triggers_meta_fn=lambda: {},
        actions_meta_fn=lambda: {"admin_action": {"permissions": ["admin"]}},
        alert_cb=lambda *args, **kwargs: None,
    )

    assert manager.required_admin_plugins() == ["admin_action"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Toast 测试")
def test_admin_notification_approves_only_allow_button(monkeypatch):
    shown = []
    removed = []

    class FakeToaster:
        def show_toast(self, toast):
            shown.append(toast)
            toast.on_activated(SimpleNamespace(arguments="approve"))

        def remove_toast(self, toast):
            removed.append(toast)

    notification_module = importlib.import_module("Win_toaster.show_notification")

    monkeypatch.setattr(notification_module, "toaster", FakeToaster())
    assert admin_prompt.confirm_admin_request("plug", timeout=0.01) is True
    assert removed == shown


@pytest.mark.skipif(os.name != "nt", reason="Windows Toast 测试")
def test_pending_admin_notification_can_be_cancelled(monkeypatch):
    shown = threading.Event()
    removed = []

    class FakeToaster:
        def show_toast(self, toast):
            shown.set()

        def remove_toast(self, toast):
            removed.append(toast)

    notification_module = importlib.import_module("Win_toaster.show_notification")
    monkeypatch.setattr(notification_module, "toaster", FakeToaster())
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            admin_prompt.confirm_admin_request("plug", timeout=10)
        )
    )
    thread.start()
    assert shown.wait(timeout=2)

    admin_prompt.cancel_pending_admin_requests()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result == [False]
    assert len(removed) == 1


def test_broker_rejects_invalid_command():
    response = admin_broker._execute({"command": "cmd.exe"})
    assert response == {"ok": False, "error": "管理员命令格式无效"}


def test_broker_executes_argument_list_without_shell(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")

    monkeypatch.setattr(admin_broker.subprocess, "run", fake_run)
    response = admin_broker._execute(
        {"command": ["tool.exe", "argument with spaces"], "wait": True, "timeout": 5}
    )

    assert response["ok"] is True
    assert response["stdout"] == "done"
    assert captured["command"] == ["tool.exe", "argument with spaces"]
    assert "shell" not in captured["kwargs"]


def test_broker_timeout_output_bytes_are_decoded(monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command, kwargs["timeout"], output=b"partial", stderr=b"timed out"
        )

    monkeypatch.setattr(admin_broker.subprocess, "run", fake_run)
    response = admin_broker._execute(
        {"command": ["tool.exe"], "wait": True, "timeout": 1}
    )

    assert response["error"] == "timeout"
    assert response["stdout"] == "partial"
    assert response["stderr"] == "timed out"
