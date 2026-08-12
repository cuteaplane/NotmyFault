"""全局输入事件转换为操作宏步骤。"""

from notmyfault.native.input_recorder import _signed_word, build_macro_steps


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
        {"kind": "mouse_down", "button": "left", "x": 1, "y": 2, "timestamp": 2.0},
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
