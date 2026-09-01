"""uia_macro 插件的操作宏录制和编辑命令。"""

import copy

from notmyfault.plugin_api import (
    native_input_recorder_api,
    native_mouse_api,
    native_uia_api,
)
try:
    from .macro_validation import validate_steps
except ImportError:
    from macro_validation import validate_steps

_recorder = native_input_recorder_api()
InputRecorder = _recorder.InputRecorder
build_macro_steps = _recorder.build_macro_steps
current_virtual_screen = native_mouse_api().current_virtual_screen
_uia = native_uia_api()
capture_element_at = _uia.capture_element_at
capture_foreground_window = _uia.capture_foreground_window
is_focused_password_control = _uia.is_focused_password_control


def _current_steps(value) -> list:
    if not isinstance(value, dict):
        return []
    if value.get("$type") == "io.github.notmyfault.uia_macro/mouse_macro@1":
        value = value.get("data")
    if not isinstance(value, dict):
        return []
    steps = value.get("steps")
    return copy.deepcopy(steps) if isinstance(steps, list) else []


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
            base_steps = validate_steps(supplied_steps) if supplied_steps else []
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
        steps = validate_steps(options.get("steps"))
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
