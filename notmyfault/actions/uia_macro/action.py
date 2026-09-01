"""按录制顺序执行鼠标、键盘和 UIA 控件步骤。"""

import time

from notmyfault.plugin_api import (
    native_keyboard_api,
    native_mouse_api,
    native_uia_api,
    owned_value_api,
)
try:
    from .macro_validation import validate_steps
except ImportError:
    from macro_validation import validate_steps

_owned_values = owned_value_api()
OwnedValueError = _owned_values.OwnedValueError
unpack_owned_value = _owned_values.unpack_owned_value
perform_key_event = native_keyboard_api().perform_key_event
perform_coordinate = native_mouse_api().perform_coordinate
_uia = native_uia_api()
focus_selector_window = _uia.focus_selector_window
focus_window_signature = _uia.focus_window_signature
perform_selector = _uia.perform_selector
read_selector_text = _uia.read_selector_text
wait_for_selector = _uia.wait_for_selector


_PACKAGE_NAME = "io.github.notmyfault.uia_macro"
_DATA_TYPE = "mouse_macro"
_DATA_VERSION = 1


def _wait(seconds, cancellation=None):
    try:
        delay = min(max(float(seconds), 0.0), 600.0)
    except (TypeError, ValueError):
        delay = 0.0
    if not delay:
        return
    if cancellation is not None:
        if cancellation.wait(delay):
            cancellation.raise_if_cancelled()
    else:
        time.sleep(delay)


def execute_macro(steps, cancellation=None):
    steps = validate_steps(steps)
    results = []
    pressed_keys = {}
    pressed_buttons = {}
    try:
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"宏第 {index + 1} 步格式无效")
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            _wait(step.get("delay_seconds", 0), cancellation)
            kind = step.get("kind") or (
                "control"
                if isinstance(step.get("selector"), dict)
                else "coordinate"
            )
            if kind == "coordinate":
                operation = step.get("operation", "left_click")
                point = step.get("point")
                results.append(perform_coordinate(
                    point,
                    operation,
                    cancellation,
                    step.get("mouse_data", 0),
                ))
                if operation.endswith("_down"):
                    pressed_buttons[operation.removesuffix("_down")] = point
                elif operation.endswith("_up"):
                    pressed_buttons.pop(operation.removesuffix("_up"), None)
                elif operation == "move":
                    for button in pressed_buttons:
                        pressed_buttons[button] = point
                continue
            if kind == "keyboard":
                events = step.get("events")
                if not isinstance(events, list) or not events:
                    raise ValueError(f"宏第 {index + 1} 步没有键盘事件")
                window_signature = step.get("window")
                focused = False
                if isinstance(window_signature, dict):
                    focus_window_signature(window_signature, cancellation)
                    focused = True
                for event in events:
                    _wait(event.get("delay_seconds", 0), cancellation)
                    perform_key_event(event, cancellation)
                    key = (
                        event.get("vk"),
                        event.get("scan_code", 0),
                        bool(event.get("extended")),
                    )
                    if event.get("event") == "down":
                        pressed_keys[key] = event
                    else:
                        pressed_keys.pop(key, None)
                results.append({
                    "kind": "keyboard",
                    "events": len(events),
                    "window_focused": focused,
                })
                continue
            if kind != "control":
                raise ValueError(f"宏第 {index + 1} 步类型不支持: {kind}")
            selector = step.get("selector")
            if not isinstance(selector, dict):
                raise ValueError(f"宏第 {index + 1} 步缺少屏幕控件信息")
            operation = step.get("operation", "invoke")
            if operation == "wait_present":
                results.append(wait_for_selector(
                    selector,
                    step.get("wait_seconds", 30),
                    cancellation,
                ))
            elif operation == "focus_window":
                results.append(focus_selector_window(selector, cancellation))
            elif operation == "read_text":
                results.append(read_selector_text(selector, cancellation))
            elif operation in ("invoke", "focus", "set_text"):
                results.append(perform_selector(
                    selector,
                    operation,
                    cancellation,
                    step.get("text", ""),
                ))
            else:
                raise ValueError(f"宏第 {index + 1} 步操作不支持: {operation}")
    finally:
        for event in reversed(list(pressed_keys.values())):
            try:
                perform_key_event({**event, "event": "up"})
            except Exception:
                pass
        for button, point in list(pressed_buttons.items()):
            try:
                perform_coordinate(point, f"{button}_up")
            except Exception:
                pass
    return {"executed": len(results), "steps": results}


def run_with_context(action_info, params, context):
    macro = params.get("macro")
    if isinstance(macro, dict) and "$type" in macro:
        try:
            macro = unpack_owned_value(
                macro,
                _PACKAGE_NAME,
                _DATA_TYPE,
                _DATA_VERSION,
            )
        except OwnedValueError as exc:
            raise ValueError(str(exc)) from exc
    if not isinstance(macro, dict) or not isinstance(macro.get("steps"), list):
        raise ValueError("操作宏数据无效，请重新录制")
    runtime = context.get("runtime", {})
    cancellation = runtime.get("cancellation") if isinstance(runtime, dict) else None
    return execute_macro(macro["steps"], cancellation)


def run(action_info, params):
    return run_with_context(action_info, params, {})
