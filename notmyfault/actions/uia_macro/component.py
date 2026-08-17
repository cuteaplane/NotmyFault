"""uia_macro 插件的操作宏录制和编辑命令。"""

import copy

from notmyfault.native.input_recorder import InputRecorder, build_macro_steps
from notmyfault.native.keyboard import validate_key_event
from notmyfault.native.mouse import current_virtual_screen
from notmyfault.native.uia import (
    DesktopElementError,
    capture_element_at,
    capture_foreground_window,
    is_focused_password_control,
    validate_window_signature,
)


_CONTROL_OPERATIONS = {
    "invoke",
    "focus",
    "set_text",
    "wait_present",
    "focus_window",
    "read_text",
}
_COORDINATE_OPERATIONS = {
    "left_click",
    "double_click",
    "right_click",
    "middle_click",
    "left_down",
    "left_up",
    "right_down",
    "right_up",
    "middle_down",
    "middle_up",
    "scroll",
    "move",
}


def _current_steps(value) -> list:
    if not isinstance(value, dict):
        return []
    if value.get("$type") == "io.github.notmyfault.uia_macro/mouse_macro@1":
        value = value.get("data")
    if not isinstance(value, dict):
        return []
    steps = value.get("steps")
    return copy.deepcopy(steps) if isinstance(steps, list) else []


def _delay(value, default: float = 0.0) -> float:
    try:
        delay = float(value)
    except (TypeError, ValueError):
        delay = default
    return round(min(max(delay, 0.0), 600.0), 3)


def _coordinate_point(point, index: int) -> dict:
    if not isinstance(point, dict):
        raise ValueError(f"第 {index + 1} 步缺少屏幕坐标")
    x = point.get("x")
    y = point.get("y")
    if isinstance(x, bool) or not isinstance(x, int):
        raise ValueError(f"第 {index + 1} 步横坐标必须是整数")
    if isinstance(y, bool) or not isinstance(y, int):
        raise ValueError(f"第 {index + 1} 步纵坐标必须是整数")
    return copy.deepcopy(point)


def _keyboard_events(events, index: int) -> list[dict]:
    if not isinstance(events, list) or not events:
        raise ValueError(f"第 {index + 1} 步没有键盘事件")
    if len(events) > 1000:
        raise ValueError(f"第 {index + 1} 步包含过多键盘事件")
    checked = []
    for event in events:
        try:
            validate_key_event(event)
        except ValueError as exc:
            raise ValueError(f"第 {index + 1} 步{exc}") from exc
        normalized = copy.deepcopy(event)
        normalized["delay_seconds"] = _delay(event.get("delay_seconds"))
        checked.append(normalized)
    return checked


def _validate_steps(steps) -> list:
    if not isinstance(steps, list) or not steps:
        raise ValueError("还没有录下任何步骤")
    if len(steps) > 5000:
        raise ValueError("一个操作宏最多包含 5000 个步骤")
    checked = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"第 {index + 1} 步格式无效")
        normalized = copy.deepcopy(step)
        kind = step.get("kind") or (
            "control" if isinstance(step.get("selector"), dict) else "coordinate"
        )
        normalized["kind"] = kind
        normalized["delay_seconds"] = _delay(step.get("delay_seconds"))
        if kind == "coordinate":
            normalized["point"] = _coordinate_point(step.get("point"), index)
            operation = step.get("operation", "left_click")
            if operation not in _COORDINATE_OPERATIONS:
                raise ValueError(f"第 {index + 1} 步坐标操作不支持: {operation}")
            normalized["operation"] = operation
            if operation == "scroll":
                try:
                    normalized["mouse_data"] = int(step.get("mouse_data", 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"第 {index + 1} 步滚动量无效") from exc
        elif kind == "control":
            selector = step.get("selector")
            if not isinstance(selector, dict) or not selector:
                raise ValueError(f"第 {index + 1} 步缺少屏幕控件信息")
            operation = step.get("operation", "invoke")
            if operation not in _CONTROL_OPERATIONS:
                raise ValueError(f"第 {index + 1} 步控件操作不支持: {operation}")
            normalized["operation"] = operation
            normalized["text"] = str(step.get("text", ""))[:10000]
            normalized["wait_seconds"] = max(
                0.1, _delay(step.get("wait_seconds"), 30.0)
            )
        elif kind == "keyboard":
            normalized["events"] = _keyboard_events(step.get("events"), index)
            if step.get("window") is not None:
                try:
                    validate_window_signature(step["window"])
                except DesktopElementError as exc:
                    raise ValueError(f"第 {index + 1} 步{exc}") from exc
                normalized["window"] = copy.deepcopy(step["window"])
        else:
            raise ValueError(f"第 {index + 1} 步类型不支持: {kind}")
        checked.append(normalized)
    return checked


def _recording_result(context, *, stop: bool = False) -> dict:
    recorder = context.session.data.get("recorder")
    if not isinstance(recorder, InputRecorder):
        steps = copy.deepcopy(context.session.data.get("steps", []))
        return {
            "recording": False,
            "steps": steps,
            "step_count": len(steps),
            "event_count": 0,
            "elapsed_seconds": 0,
        }
    if stop:
        recorder.stop()
    snapshot = recorder.snapshot()
    if snapshot["recording"]:
        return {
            "recording": True,
            "event_count": snapshot["event_count"],
            "elapsed_seconds": snapshot["elapsed_seconds"],
            "stop_hotkey": "Ctrl+Shift+F10",
        }
    recorder.stop()
    if not context.session.data.get("recording_finalized"):
        recorded = build_macro_steps(
            snapshot["events"],
            recorder.started_at,
            context.session.data.get("recording_screen")
            or current_virtual_screen(),
        )
        steps = copy.deepcopy(context.session.data.get("recording_base", []))
        steps.extend(recorded)
        context.session.data["steps"] = steps
        context.session.data["recording_finalized"] = True
    steps = copy.deepcopy(context.session.data.get("steps", []))
    context.session.set_status(f"录制完成，共 {len(steps)} 步")
    return {
        "recording": False,
        "steps": steps,
        "step_count": len(steps),
        "event_count": snapshot["event_count"],
        "elapsed_seconds": snapshot["elapsed_seconds"],
        "error": snapshot["error"],
        "window_action": "restore",
    }


def open_macro(context, payload):
    steps = _current_steps(context.current_value)
    context.session.data["steps"] = steps
    context.session.set_status("操作宏编辑器已打开")
    return context.open_view(
        "macro_workbench",
        {"steps": steps, "recording": False, "stop_hotkey": "Ctrl+Shift+F10"},
    )


def start_recording(context, payload):
    current = context.session.data.get("recorder")
    if isinstance(current, InputRecorder) and current.recording:
        return context.error("操作宏正在录制")
    if isinstance(current, InputRecorder):
        _recording_result(context)
    options = payload if isinstance(payload, dict) else {}
    append = options.get("append") is True
    minimize_window = options.get("minimize_window") is True
    base_steps = (
        copy.deepcopy(context.session.data.get("steps", [])) if append else []
    )
    supplied_steps = options.get("steps")
    if append and isinstance(supplied_steps, list):
        try:
            base_steps = _validate_steps(supplied_steps) if supplied_steps else []
        except ValueError as exc:
            return context.error(str(exc))
    try:
        screen = current_virtual_screen()
    except Exception as exc:
        return context.error(str(exc))
    recorder = InputRecorder(
        mouse_resolver=capture_element_at,
        keyboard_window_resolver=capture_foreground_window,
        keyboard_password_resolver=is_focused_password_control,
    )
    try:
        recorder.start()
    except Exception as exc:
        return context.error(str(exc))
    context.session.data["recording_base"] = base_steps
    context.session.data["recording_screen"] = screen
    context.session.data["recorder"] = recorder
    context.session.data["recording_finalized"] = False
    context.register_cleanup(recorder.stop)
    context.session.set_status("正在录制操作，按 Ctrl+Shift+F10 停止")
    return context.result({
        "recording": True,
        "event_count": 0,
        "elapsed_seconds": 0,
        "stop_hotkey": "Ctrl+Shift+F10",
        **({"window_action": "minimize"} if minimize_window else {}),
    })


def recording_status(context, payload):
    return context.result(_recording_result(context))


def stop_recording(context, payload):
    return context.result(_recording_result(context, stop=True))


def commit_macro(context, payload):
    recorder = context.session.data.get("recorder")
    if isinstance(recorder, InputRecorder) and recorder.recording:
        return context.error("请先停止录制，再保存操作宏")
    options = payload if isinstance(payload, dict) else {}
    try:
        steps = _validate_steps(options.get("steps"))
    except ValueError as exc:
        return context.error(str(exc))
    context.session.data["steps"] = copy.deepcopy(steps)
    context.session.set_status("操作宏已保存")
    return context.commit(
        {"version": 1, "steps": steps},
        f"1 个操作宏 · {len(steps)} 步",
        response={"step_count": len(steps)},
    )


def cancel_macro(context, payload):
    recorder = context.session.data.get("recorder")
    if isinstance(recorder, InputRecorder):
        recorder.stop()
    context.session.data["steps"] = []
    context.session.set_status("编辑已取消")
    return context.result(close=True)
