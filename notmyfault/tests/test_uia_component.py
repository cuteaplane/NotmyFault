"""uia_control 录制组件：采集、校验和步骤转动作"""

import pytest

import notmyfault.actions.uia_control.component as component
from notmyfault.components.session import ComponentSession


SELECTOR = {
    "version": 1,
    "window": {"app": "记事本", "title": "未命名 - 记事本"},
    "target": {"control_type": 50000, "name": "保存"},
    "ancestors": [],
}


class TestToActions:
    def test_click_step_maps_to_uia_control(self):
        actions = component.to_actions([
            {"selector": SELECTOR, "operation": "invoke"},
        ])
        assert actions[0]["type"] == "uia_control"
        assert actions[0]["params"]["operation"] == "invoke"
        assert actions[0]["params"]["target"] is SELECTOR

    def test_text_step_keeps_text(self):
        actions = component.to_actions([
            {"selector": SELECTOR, "operation": "set_text", "text": "hello"},
        ])
        assert actions[0]["type"] == "uia_control"
        assert actions[0]["params"]["text"] == "hello"

    def test_focus_step_has_no_text(self):
        actions = component.to_actions([
            {"selector": SELECTOR, "operation": "focus"},
        ])
        assert actions[0]["params"]["text"] == ""

    def test_wait_window_read_steps(self):
        actions = component.to_actions([
            {"selector": SELECTOR, "operation": "wait_present", "waitSeconds": 45},
            {"selector": SELECTOR, "operation": "focus_window"},
            {"selector": SELECTOR, "operation": "read_text"},
        ])
        assert [action["type"] for action in actions] == [
            "uia_wait", "uia_focus_window", "uia_read_text",
        ]
        assert actions[0]["params"]["wait_seconds"] == 45

    def test_wait_seconds_clamped(self):
        actions = component.to_actions([
            {"selector": SELECTOR, "operation": "wait_present", "waitSeconds": 9999},
            {"selector": SELECTOR, "operation": "wait_present", "waitSeconds": "abc"},
        ])
        assert actions[0]["params"]["wait_seconds"] == 600
        assert actions[1]["params"]["wait_seconds"] == 30

    def test_invalid_step_rejected(self):
        with pytest.raises(ValueError):
            component.to_actions([{"operation": "nope"}])
        with pytest.raises(ValueError):
            component.to_actions([{"selector": "not-a-dict"}])
        with pytest.raises(ValueError):
            component.to_actions("not-a-list")


class TestInvoke:
    def test_unknown_method_fails(self):
        session = ComponentSession("uia_control", "record", {})
        result = component.invoke(session, "nope", {})
        assert result["ok"] is False
        assert "未知方法" in result["error"]

    def test_to_actions_via_invoke(self):
        result = component.invoke(
            None,
            "to_actions",
            {"steps": [{"selector": SELECTOR, "operation": "invoke"}]},
        )
        assert result["ok"] is True
        assert result["data"]["actions"][0]["type"] == "uia_control"

    def test_capture_returns_selector(self, monkeypatch):
        monkeypatch.setattr(
            component,
            "capture_element_under_cursor",
            lambda: dict(SELECTOR),
        )
        monkeypatch.setattr(component.time, "sleep", lambda seconds: None)
        session = ComponentSession("uia_control", "record", {})
        result = component.invoke(session, "capture", {"delay_seconds": 2})
        assert result["ok"] is True
        assert result["data"]["selector"]["version"] == 1
        assert session.status == "已采集屏幕控件"

    def test_capture_delay_clamped(self, monkeypatch):
        slept = []
        monkeypatch.setattr(
            component,
            "capture_element_under_cursor",
            lambda: dict(SELECTOR),
        )
        monkeypatch.setattr(
            component.time, "sleep", lambda seconds: slept.append(seconds)
        )
        session = ComponentSession("uia_control", "record", {})
        component.invoke(session, "capture", {"delay_seconds": 99})
        assert slept == [10.0]

    def test_check_returns_capabilities(self, monkeypatch):
        monkeypatch.setattr(
            component,
            "check_selector",
            lambda selector: {
                "ok": True,
                "display": {"control": "保存"},
                "bounds": {"left": 0, "top": 0, "width": 10, "height": 10},
                "capabilities": {
                    "invoke": True,
                    "focus": True,
                    "set_text": False,
                    "read_text": False,
                },
            },
        )
        result = component.invoke(None, "check", {"selector": SELECTOR})
        assert result["ok"] is True
        assert result["data"]["display"]["control"] == "保存"
