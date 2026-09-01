"""uia_control 插件的屏幕控件选择和录制命令。"""

import time

from notmyfault.plugin_api import native_uia_api

_uia = native_uia_api()
DesktopElementError = _uia.DesktopElementError
capture_element_under_cursor = _uia.capture_element_under_cursor
check_selector = _uia.check_selector


def _clamp_delay(value) -> float:
    try:
        delay = float(value)
    except (TypeError, ValueError):
        delay = 3.0
    return min(max(delay, 1.0), 10.0)


def _clamp_wait(value) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = 30
    return min(max(seconds, 1), 600)


def _convert_step(step) -> dict:
    if not isinstance(step, dict):
        raise ValueError("步骤必须是对象")
    selector = step.get("selector")
    if not isinstance(selector, dict):
        raise ValueError("步骤缺少屏幕控件信息，请重新选择控件")
    operation = step.get("operation", "invoke")
    if operation == "wait_present":
        return {
            "type": "uia_wait",
            "params": {
                "target": selector,
                "wait_seconds": _clamp_wait(step.get("waitSeconds", 30)),
            },
        }
    if operation == "focus_window":
        return {"type": "uia_focus_window", "params": {"target": selector}}
    if operation == "read_text":
        return {"type": "uia_read_text", "params": {"target": selector}}
    if operation not in ("invoke", "focus", "set_text"):
        raise ValueError(f"不支持的步骤操作: {operation}")
    return {
        "type": "uia_control",
        "params": {
            "target": selector,
            "operation": operation,
            "text": str(step.get("text", "")) if operation == "set_text" else "",
        },
    }


def to_actions(steps) -> list:
    if not isinstance(steps, list):
        raise ValueError("步骤必须是数组")
    return [_convert_step(step) for step in steps]


def edit_selector(context, payload):
    options = payload if isinstance(payload, dict) else {}
    operation = options.get("operation", "capture")
    try:
        if operation == "capture":
            time.sleep(_clamp_delay(options.get("delay_seconds", 3)))
            selector = capture_element_under_cursor()
            context.session.set_status("已采集屏幕控件")
            return context.commit_value(selector)
        if operation == "check":
            selector = options.get("selector", context.current_value)
            return context.result(check_selector(selector), close=True)
        if operation == "to_actions":
            return context.result(
                {"actions": to_actions(options.get("steps"))},
                close=True,
            )
    except (DesktopElementError, ValueError) as error:
        return context.error(str(error), close=True)
    return context.error(f"未知操作: {operation}", close=True)
