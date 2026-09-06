"""执行 UIA 控件操作或录制的桌面操作宏。"""

import time

from notmyfault.plugin_api import owned_value_api
if __package__ == "notmyfault.actions.uia_automation":
    from .nmf_uia_plugin.keyboard import perform_key_event
    from .nmf_uia_plugin.macro_validation import validate_steps
    from .nmf_uia_plugin.mouse import perform_coordinate
    from .nmf_uia_plugin.uia import (
        focus_selector_window,
        focus_window_signature,
        perform_selector,
        read_selector_text,
        wait_for_selector,
    )
else:
    from nmf_uia_plugin.keyboard import perform_key_event
    from nmf_uia_plugin.macro_validation import validate_steps
    from nmf_uia_plugin.mouse import perform_coordinate
    from nmf_uia_plugin.uia import (
        focus_selector_window,
        focus_window_signature,
        perform_selector,
        read_selector_text,
        wait_for_selector,
    )


_owned_values = owned_value_api()
OwnedValueError = _owned_values.OwnedValueError
unpack_owned_value = _owned_values.unpack_owned_value

_PACKAGE_NAME = "io.github.notmyfault.uia_automation"
_DATA_VERSION = 1
_TARGET_MODES = {"control", "focus_window", "read_text", "wait"}


def _unpack_plugin_data(value, data_type: str):
    if not isinstance(value, dict) or "$type" not in value:
        return value
    try:
        return unpack_owned_value(
            value,
            _PACKAGE_NAME,
            data_type,
            _DATA_VERSION,
        )
    except OwnedValueError as exc:
        raise ValueError(str(exc)) from exc


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
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            _wait(step.get("delay_seconds", 0), cancellation)
            kind = step["kind"]
            if kind == "coordinate":
                operation = step["operation"]
                point = step["point"]
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
                window_signature = step.get("window")
                focused = False
                if isinstance(window_signature, dict):
                    focus_window_signature(window_signature, cancellation)
                    focused = True
                for event in step["events"]:
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
                    "events": len(step["events"]),
                    "window_focused": focused,
                })
                continue
            selector = step["selector"]
            operation = step["operation"]
            if operation == "wait_present":
                results.append(wait_for_selector(
                    selector,
                    step["wait_seconds"],
                    cancellation,
                ))
            elif operation == "focus_window":
                results.append(focus_selector_window(selector, cancellation))
            elif operation == "read_text":
                results.append(read_selector_text(selector, cancellation))
            else:
                results.append(perform_selector(
                    selector,
                    operation,
                    cancellation,
                    step["text"],
                ))
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
    mode = str(params.get("mode", "control") or "control")
    if mode not in _TARGET_MODES | {"macro"}:
        raise ValueError(f"不支持的 UIA 自动化模式: {mode}")
    runtime = context.get("runtime", {})
    cancellation = runtime.get("cancellation") if isinstance(runtime, dict) else None

    if mode == "macro":
        macro = _unpack_plugin_data(params.get("macro"), "mouse_macro")
        if not isinstance(macro, dict) or not isinstance(macro.get("steps"), list):
            raise ValueError("操作宏数据无效，请重新录制")
        return execute_macro(macro["steps"], cancellation)

    target = _unpack_plugin_data(params.get("target"), "uia_selector")
    if not isinstance(target, dict):
        raise ValueError("请先选择屏幕控件")
    if mode == "focus_window":
        return focus_selector_window(target, cancellation)
    if mode == "read_text":
        return read_selector_text(target, cancellation)
    if mode == "wait":
        return wait_for_selector(
            target,
            params.get("wait_seconds", 30),
            cancellation,
        )

    operation = str(params.get("operation", "invoke") or "invoke")
    text = params.get("text", "")
    if not isinstance(text, str):
        raise ValueError("写入屏幕控件的内容必须是文本")
    return perform_selector(target, operation, cancellation, text)


def run(action_info, params):
    return run_with_context(action_info, params, {})
