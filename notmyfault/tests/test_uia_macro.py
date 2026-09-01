"""uia_macro 插件的宏执行和扩展命令。"""

import json
from pathlib import Path

import pytest

import notmyfault.actions.uia_macro.action as macro_action
import notmyfault.actions.uia_macro.component as macro_component
from notmyfault.extensions.protocol import make_owned_value
from notmyfault.extensions.registry import ExtensionRegistry
from notmyfault.extensions.session import ExtensionContext, ExtensionSession


SELECTOR = {
    "version": 1,
    "window": {"app": "记事本", "name": "未命名 - 记事本"},
    "target": {"control_type": 50000, "name": "保存"},
}
STEPS = [
    {"selector": SELECTOR, "operation": "invoke"},
    {"selector": SELECTOR, "operation": "set_text", "text": "hello"},
]
POINT = {
    "version": 1,
    "x": 320,
    "y": 240,
    "screen": {"left": 0, "top": 0, "width": 1920, "height": 1080},
}


class TestExecuteMacro:
    def test_executes_steps_in_order(self, monkeypatch):
        calls = []

        def fake_perform(selector, operation, cancellation, text):
            calls.append(("perform", operation, text))
            return {"operation": operation}

        monkeypatch.setattr(macro_action, "perform_selector", fake_perform)
        result = macro_action.execute_macro(STEPS)
        assert result["executed"] == 2
        assert calls == [
            ("perform", "invoke", ""),
            ("perform", "set_text", "hello"),
        ]

    def test_wait_window_read_dispatched(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            macro_action,
            "wait_for_selector",
            lambda selector, timeout, cancellation: (
                calls.append(("wait", timeout)) or {"found": True}
            ),
        )
        monkeypatch.setattr(
            macro_action,
            "focus_selector_window",
            lambda selector, cancellation: calls.append(("focus",)) or {"focused": True},
        )
        monkeypatch.setattr(
            macro_action,
            "read_selector_text",
            lambda selector, cancellation: calls.append(("read",)) or {"text": "x"},
        )
        result = macro_action.execute_macro([
            {"selector": SELECTOR, "operation": "wait_present", "wait_seconds": 10},
            {"selector": SELECTOR, "operation": "focus_window"},
            {"selector": SELECTOR, "operation": "read_text"},
        ])
        assert calls == [("wait", 10), ("focus",), ("read",)]
        assert result["executed"] == 3

    def test_coordinate_and_control_steps_can_be_mixed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            macro_action,
            "perform_coordinate",
            lambda point, operation, cancellation, mouse_data=0: (
                calls.append(("coordinate", point, operation, mouse_data))
                or {"x": point["x"]}
            ),
        )
        monkeypatch.setattr(
            macro_action,
            "perform_selector",
            lambda selector, operation, cancellation, text: (
                calls.append(("control", selector, operation)) or {"operation": operation}
            ),
        )
        result = macro_action.execute_macro([
            {"kind": "coordinate", "point": POINT, "operation": "left_click"},
            {"kind": "control", "selector": SELECTOR, "operation": "invoke"},
        ])
        assert result["executed"] == 2
        assert calls == [
            ("coordinate", POINT, "left_click", 0),
            ("control", SELECTOR, "invoke"),
        ]

    def test_keyboard_events_and_delays_are_replayed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            macro_action,
            "_wait",
            lambda seconds, cancellation=None: calls.append(("wait", seconds)),
        )
        monkeypatch.setattr(
            macro_action,
            "perform_key_event",
            lambda event, cancellation=None: calls.append(("key", event["event"])),
        )
        result = macro_action.execute_macro([{
            "kind": "keyboard",
            "delay_seconds": 1.25,
            "events": [
                {"event": "down", "vk": 65, "scan_code": 30, "delay_seconds": 0},
                {"event": "up", "vk": 65, "scan_code": 30, "delay_seconds": 0.2},
            ],
        }])
        assert result["executed"] == 1
        assert calls == [
            ("wait", 1.25),
            ("wait", 0),
            ("key", "down"),
            ("wait", 0.2),
            ("key", "up"),
        ]

    def test_keyboard_focuses_recorded_window_before_typing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(macro_action, "_wait", lambda *args: None)
        monkeypatch.setattr(
            macro_action,
            "focus_window_signature",
            lambda window, cancellation=None: calls.append(("focus", window)),
        )
        monkeypatch.setattr(
            macro_action,
            "perform_key_event",
            lambda event, cancellation=None: calls.append(("key", event["vk"])),
        )
        window = {"process": "WindowsTerminal.exe", "name": "终端"}

        result = macro_action.execute_macro([{
            "kind": "keyboard",
            "window": window,
            "events": [{"event": "down", "vk": 65, "scan_code": 30}],
        }])

        assert calls[:2] == [("focus", window), ("key", 65)]
        assert result["steps"][0]["window_focused"] is True

    def test_unmatched_key_down_is_released_after_macro(self, monkeypatch):
        calls = []
        monkeypatch.setattr(macro_action, "_wait", lambda *args: None)
        monkeypatch.setattr(
            macro_action,
            "perform_key_event",
            lambda event, cancellation=None: calls.append(event["event"]),
        )
        result = macro_action.execute_macro([{
            "kind": "keyboard",
            "events": [{"event": "down", "vk": 17, "scan_code": 29}],
        }])
        assert result["executed"] == 1
        assert calls == ["down", "up"]

    def test_unmatched_mouse_down_is_released_at_last_drag_point(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            macro_action,
            "perform_coordinate",
            lambda point, operation, cancellation=None, mouse_data=0: (
                calls.append((operation, point["x"])) or {"operation": operation}
            ),
        )
        macro_action.execute_macro([
            {"kind": "coordinate", "point": POINT, "operation": "left_down"},
            {
                "kind": "coordinate",
                "point": {**POINT, "x": 500},
                "operation": "move",
            },
        ])
        assert calls == [("left_down", 320), ("move", 500), ("left_up", 500)]

    def test_invalid_steps_rejected(self):
        with pytest.raises(ValueError):
            macro_action.execute_macro([])
        with pytest.raises(ValueError):
            macro_action.execute_macro([{"operation": "nope"}])
        with pytest.raises(ValueError):
            macro_action.execute_macro([{"operation": "invoke"}])

    def test_execution_rejects_keyboard_event_limit_bypass(self):
        events = [
            {"event": "down", "vk": 65, "scan_code": 30}
            for _ in range(1001)
        ]

        with pytest.raises(ValueError, match="过多键盘事件"):
            macro_action.execute_macro([{"kind": "keyboard", "events": events}])

    def test_run_with_context_passes_cancellation(self, monkeypatch):
        captured = {}

        def fake_execute(steps, cancellation):
            captured["steps"] = steps
            captured["cancellation"] = cancellation
            return {"executed": len(steps)}

        monkeypatch.setattr(macro_action, "execute_macro", fake_execute)
        cancellation = object()
        result = macro_action.run_with_context(
            None,
            {"macro": {"steps": STEPS}},
            {"runtime": {"cancellation": cancellation}},
        )
        assert result["executed"] == 2
        assert captured["cancellation"] is cancellation

    def test_run_rejects_missing_macro(self):
        with pytest.raises(ValueError):
            macro_action.run_with_context(None, {}, {})

    def test_run_accepts_owned_macro(self, monkeypatch):
        monkeypatch.setattr(
            macro_action,
            "execute_macro",
            lambda steps, cancellation: {"executed": len(steps)},
        )
        value = make_owned_value(
            "io.github.notmyfault.uia_macro",
            "mouse_macro",
            1,
            {"version": 1, "steps": STEPS},
            "1 个操作宏 · 2 步",
        )
        assert macro_action.run_with_context(None, {"macro": value}, {}) == {
            "executed": 2
        }

    def test_run_rejects_other_plugin_data(self):
        value = make_owned_value(
            "com.example.other",
            "mouse_macro",
            1,
            {"steps": STEPS},
            "其他插件的数据",
        )
        with pytest.raises(ValueError, match="归属不匹配"):
            macro_action.run_with_context(None, {"macro": value}, {})


class TestMacroComponent:
    @staticmethod
    def make_context(current_value=None):
        root = Path(macro_component.__file__).parent
        meta = json.loads((root / "action.json").read_text(encoding="utf-8"))
        registry = ExtensionRegistry()
        registry.register_manifest("uia_macro", "action", meta, str(root))
        session = ExtensionSession(
            plugin_id="uia_macro",
            command_id="open_macro",
            plugin_meta=meta,
            source_kind="parameter_editors",
            source_id="macro_editor",
            allowed_commands={
                "open_macro",
                "start_recording",
                "recording_status",
                "stop_recording",
                "commit_macro",
                "cancel_macro",
            },
            data_type=registry.data_type("uia_macro", "mouse_macro"),
            current_value=current_value,
        )
        return ExtensionContext(session, registry)

    def test_commit_builds_macro_value(self):
        context = self.make_context()
        result = macro_component.commit_macro(context, {"steps": STEPS})
        assert result["ok"] is True
        assert result["value"]["$type"] == (
            "io.github.notmyfault.uia_macro/mouse_macro@1"
        )
        assert result["value"]["summary"] == "1 个操作宏 · 2 步"
        saved_steps = result["value"]["data"]["steps"]
        assert [step["operation"] for step in saved_steps] == ["invoke", "set_text"]
        assert all(step["kind"] == "control" for step in saved_steps)
        assert saved_steps[1]["text"] == "hello"
        assert all(step["wait_seconds"] == 30 for step in saved_steps)
        assert result["close"] is True

    def test_commit_rejects_empty(self):
        context = self.make_context()
        result = macro_component.commit_macro(context, {"steps": []})
        assert result["ok"] is False

    def test_recording_session_builds_steps_and_stops_on_cleanup(self, monkeypatch):
        class FakeRecorder:
            instances = []

            def __init__(self, mouse_resolver=None, keyboard_window_resolver=None,
                         keyboard_password_resolver=None):
                self.mouse_resolver = mouse_resolver
                self.keyboard_window_resolver = keyboard_window_resolver
                self.keyboard_password_resolver = keyboard_password_resolver
                self.recording = False
                self.started_at = 10.0
                self.events = [{"kind": "keyboard", "timestamp": 10.2}]
                self.__class__.instances.append(self)

            def start(self):
                self.recording = True

            def stop(self):
                self.recording = False

            def snapshot(self):
                return {
                    "recording": self.recording,
                    "events": self.events,
                    "event_count": len(self.events),
                    "elapsed_seconds": 0.2,
                    "error": "",
                }

        recorded = [{
            "kind": "keyboard",
            "delay_seconds": 0.2,
            "events": [{"event": "down", "vk": 65, "scan_code": 30}],
        }]
        monkeypatch.setattr(macro_component, "InputRecorder", FakeRecorder)
        monkeypatch.setattr(
            macro_component, "current_virtual_screen", lambda: POINT["screen"]
        )
        monkeypatch.setattr(
            macro_component,
            "build_macro_steps",
            lambda events, started_at, screen: recorded,
        )
        context = self.make_context({"version": 1, "steps": STEPS})
        macro_component.open_macro(context, {})
        started = macro_component.start_recording(
            context, {"append": True, "minimize_window": True}
        )
        assert started["data"]["recording"] is True
        assert FakeRecorder.instances[0].keyboard_window_resolver is macro_component.capture_foreground_window
        assert FakeRecorder.instances[0].keyboard_password_resolver is macro_component.is_focused_password_control
        assert started["data"]["window_action"] == "minimize"
        assert macro_component.recording_status(context, {})["data"]["recording"] is True
        stopped = macro_component.stop_recording(context, {})
        assert stopped["data"]["recording"] is False
        assert stopped["data"]["window_action"] == "restore"
        assert stopped["data"]["steps"] == STEPS + recorded
        context.session.close()
        assert FakeRecorder.instances[0].recording is False

        replacement_context = self.make_context({"version": 1, "steps": STEPS})
        macro_component.open_macro(replacement_context, {})
        macro_component.start_recording(replacement_context, {})
        assert replacement_context.session.data["recording_base"] == []
        replacement_context.session.close()

    def test_commit_rejects_active_recording(self, monkeypatch):
        class FakeRecorder:
            recording = True

        monkeypatch.setattr(macro_component, "InputRecorder", FakeRecorder)
        context = self.make_context()
        context.session.data["recorder"] = FakeRecorder()
        result = macro_component.commit_macro(context, {"steps": STEPS})
        assert result["ok"] is False
        assert "停止录制" in result["error"]

    def test_commit_accepts_coordinate_step(self):
        context = self.make_context()
        result = macro_component.commit_macro(context, {"steps": [{
            "kind": "coordinate",
            "point": POINT,
            "operation": "double_click",
        }]})
        assert result["ok"] is True
        saved = result["value"]["data"]["steps"][0]
        assert saved["point"] == POINT
        assert saved["operation"] == "double_click"

    def test_commit_accepts_keyboard_and_scroll_steps(self):
        context = self.make_context()
        result = macro_component.commit_macro(context, {"steps": [
            {
                "kind": "keyboard",
                "delay_seconds": 0.25,
                "window": {"process": "WindowsTerminal.exe", "name": "终端"},
                "events": [
                    {"event": "down", "vk": 65, "scan_code": 30},
                    {"event": "up", "vk": 65, "scan_code": 30},
                ],
            },
            {
                "kind": "coordinate",
                "point": POINT,
                "operation": "scroll",
                "mouse_data": -120,
            },
        ]})
        assert result["ok"] is True
        keyboard, scroll = result["value"]["data"]["steps"]
        assert keyboard["window"]["process"] == "WindowsTerminal.exe"
        assert keyboard["events"][0]["delay_seconds"] == 0.0
        assert scroll["mouse_data"] == -120

    def test_cancel_clears_steps(self):
        context = self.make_context()
        context.session.data["steps"] = [
            {"selector": SELECTOR, "operation": "invoke"}
        ]
        result = macro_component.cancel_macro(context, {})
        assert result["ok"] is True
        assert result["close"] is True
        assert context.session.data["steps"] == []

    def test_open_reuses_legacy_steps(self):
        context = self.make_context({"version": 1, "steps": STEPS})
        result = macro_component.open_macro(context, {})
        assert result["view"] == "macro_workbench"
        assert result["state"]["steps"] == STEPS

    def test_open_reuses_owned_value_without_host_unpacking(self):
        value = make_owned_value(
            "io.github.notmyfault.uia_macro",
            "mouse_macro",
            1,
            {"version": 1, "steps": STEPS},
            "1 个操作宏 · 2 步",
        )
        context = self.make_context(value)

        result = macro_component.open_macro(context, {})

        assert result["state"]["steps"] == STEPS
