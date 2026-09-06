"""全局输入事件转换为操作宏步骤。"""

import threading
from contextlib import nullcontext

import pytest

from notmyfault.actions.uia_automation.nmf_uia_plugin import input_recorder
from notmyfault.actions.uia_automation.nmf_uia_plugin.input_recorder import (
    KBDLLHOOKSTRUCT,
    InputRecorder,
    WM_KEYDOWN,
    WM_KEYUP,
    _signed_word,
    build_macro_steps,
)


SCREEN = {"left": 0, "top": 0, "width": 1920, "height": 1080}
WINDOW = {"process": "WindowsTerminal.exe", "name": "终端"}
SELECTOR = {
    "version": 1,
    "window": {"app": "记事本", "name": "未命名 - 记事本"},
    "target": {"control_type": 50000, "name": "保存"},
}


def test_signed_word_reads_mouse_wheel_delta():
    assert _signed_word(120 << 16) == 120
    assert _signed_word((0x10000 - 120) << 16) == -120


@pytest.mark.parametrize("resolved_before_dispatch", [True, False])
def test_click_uses_only_selector_available_before_dispatch(monkeypatch, resolved_before_dispatch):
    resolver_started = threading.Event()
    resolver_gate = threading.Event()
    hook_returned = threading.Event()
    state = {"selector": SELECTOR}

    def mouse_resolver(x, y):
        resolver_started.set()
        resolver_gate.wait(1)
        return state["selector"]

    recorder = InputRecorder(mouse_resolver=mouse_resolver)
    monkeypatch.setattr(recorder, "_message_timestamp", lambda value: float(value))
    monkeypatch.setattr(input_recorder, "per_monitor_dpi_context", nullcontext)
    resolver = threading.Thread(target=recorder._resolve_loop, daemon=True)
    resolver.start()

    def click():
        data = input_recorder.MSLLHOOKSTRUCT()
        data.pt.x, data.pt.y, data.time = 100, 200, 1
        recorder._mouse_event(input_recorder.WM_LBUTTONDOWN, data)
        data.time = 2
        recorder._mouse_event(input_recorder.WM_LBUTTONUP, data)
        state["selector"] = {**SELECTOR, "target": {"name": "点击后出现的控件"}}
        hook_returned.set()

    hook = threading.Thread(target=click, daemon=True)
    hook.start()
    try:
        assert resolver_started.wait(1)
        if resolved_before_dispatch:
            resolver_gate.set()
        assert hook_returned.wait(1)
        resolver_gate.set()
        recorder._resolve_queue.join()
        steps = build_macro_steps(recorder.snapshot()["events"], 0, SCREEN)
        assert len(steps) == 1
        if resolved_before_dispatch:
            assert steps[0]["operation"] == "invoke"
            assert steps[0]["selector"] == SELECTOR
        else:
            assert steps[0]["operation"] == "left_click"
            assert steps[0]["point"]["x"] == 100
            assert steps[0]["point"]["y"] == 200
    finally:
        resolver_gate.set()
        hook.join(timeout=1)
        recorder._resolve_queue.put(None)
        resolver.join(timeout=1)


@pytest.mark.parametrize("password_after_hook", [True, False])
def test_keyboard_events_with_late_password_result_are_not_recorded(
    password_after_hook,
):
    resolver_started = threading.Event()
    resolver_gate = threading.Event()

    def password_resolver():
        resolver_started.set()
        resolver_gate.wait(timeout=2)
        return password_after_hook

    recorder = InputRecorder(
        keyboard_password_resolver=password_resolver,
    )
    resolver_thread = threading.Thread(
        target=recorder._resolve_loop,
        daemon=True,
    )
    resolver_thread.start()
    data = KBDLLHOOKSTRUCT(65, 30, 0, 1, 0)
    hook_returned = threading.Event()

    def send_key():
        assert recorder._keyboard_event(WM_KEYDOWN, data) is False
        assert recorder._keyboard_event(WM_KEYUP, data) is False
        hook_returned.set()

    hook_thread = threading.Thread(target=send_key, daemon=True)
    hook_thread.start()
    try:
        assert hook_returned.wait(timeout=0.5)
        assert resolver_started.wait(timeout=0.5)
        resolver_gate.set()
        recorder._resolve_queue.join()
        assert recorder.snapshot()["events"] == []
    finally:
        resolver_gate.set()
        hook_thread.join(timeout=1)
        recorder._resolve_queue.put(None)
        resolver_thread.join(timeout=1)


@pytest.mark.parametrize("password_result", [False, True, "error"])
def test_keyboard_events_require_password_check_before_recording(
    monkeypatch, password_result,
):
    def password_resolver():
        if password_result == "error":
            raise RuntimeError("无法读取焦点控件")
        return password_result

    recorder = InputRecorder(
        keyboard_password_resolver=password_resolver,
        keyboard_window_resolver=lambda: WINDOW,
    )
    monkeypatch.setattr(recorder, "_message_timestamp", lambda value: float(value))
    resolver_thread = threading.Thread(
        target=recorder._resolve_loop,
        daemon=True,
    )
    resolver_thread.start()
    try:
        down = KBDLLHOOKSTRUCT(65, 30, 0, 1, 0)
        up = KBDLLHOOKSTRUCT(65, 30, 0, 2, 0)
        recorder._keyboard_event(WM_KEYDOWN, down)
        recorder._keyboard_event(WM_KEYUP, up)
        recorder._resolve_queue.join()

        events = recorder.snapshot()["events"]
        if password_result is False:
            assert [event["event"] for event in events] == ["down", "up"]
            assert events[0]["window"] == WINDOW
            assert "window" not in events[1]
        else:
            assert events == []
    finally:
        recorder._resolve_queue.put(None)
        resolver_thread.join(timeout=1)


def test_click_keyboard_and_wheel_keep_timing_and_uia_selector():
    events = [
        {
            "kind": "mouse_down",
            "button": "left",
            "x": 100,
            "y": 200,
            "selector": SELECTOR,
            "timestamp": 100.5,
        },
        {"kind": "mouse_up", "button": "left", "x": 100, "y": 200, "timestamp": 100.6},
        {"kind": "keyboard", "event": "down", "vk": 65, "scan_code": 30, "window": WINDOW, "timestamp": 101.0},
        {"kind": "keyboard", "event": "up", "vk": 65, "scan_code": 30, "timestamp": 101.2},
        {"kind": "mouse_wheel", "x": 120, "y": 220, "delta": -120, "timestamp": 102.0},
    ]
    control, keyboard, wheel = build_macro_steps(events, 100.0, SCREEN)
    assert control["kind"] == "control"
    assert control["selector"] == SELECTOR
    assert control["delay_seconds"] == 0.5
    assert keyboard["kind"] == "keyboard"
    assert keyboard["window"] == WINDOW
    assert keyboard["delay_seconds"] == 0.4
    assert [event["delay_seconds"] for event in keyboard["events"]] == [0.0, 0.2]
    assert wheel["operation"] == "scroll"
    assert wheel["mouse_data"] == -120
    assert wheel["delay_seconds"] == 0.8


def test_unrecognized_click_falls_back_to_screen_coordinate():
    events = [
        {"kind": "mouse_down", "button": "right", "x": 9, "y": 8, "timestamp": 1.0},
        {"kind": "mouse_up", "button": "right", "x": 9, "y": 8, "timestamp": 1.1},
    ]
    assert build_macro_steps(events, 0.0, SCREEN) == [{
        "kind": "coordinate",
        "point": {"version": 1, "x": 9, "y": 8, "screen": SCREEN},
        "operation": "right_click",
        "delay_seconds": 1.0,
    }]


def test_drag_is_preserved_as_down_moves_and_up():
    events = [
        {"kind": "mouse_down", "button": "left", "x": 1, "y": 2, "timestamp": 2.0, "selector": SELECTOR},
        {"kind": "mouse_move", "x": 3, "y": 4, "timestamp": 2.1},
        {"kind": "mouse_move", "x": 5, "y": 6, "timestamp": 2.2},
        {"kind": "mouse_up", "button": "left", "x": 7, "y": 8, "timestamp": 2.3},
    ]
    steps = build_macro_steps(events, 1.0, SCREEN)
    assert [step["operation"] for step in steps] == [
        "left_down", "move", "move", "left_up"
    ]
    assert [step["delay_seconds"] for step in steps] == [1.0, 0.1, 0.1, 0.1]
    assert steps[-1]["point"]["x"] == 7
