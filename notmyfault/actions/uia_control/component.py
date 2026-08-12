"""uia_control 插件的录制组件：采集屏幕控件、校验选择器并转成动作"""

import time

from notmyfault.native.uia import (
    DesktopElementError,
    capture_element_under_cursor,
    check_selector,
)


def describe() -> dict:
    """组件支持的调用方法，Dashboard 可以按这份清单渲染入口"""
    return {
        "methods": ["capture", "check", "to_actions"],
    }


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
    """把录制会话的步骤列表转成普通动作，动作类型都是本插件族的"""
    if not isinstance(steps, list):
        raise ValueError("步骤必须是数组")
    return [_convert_step(step) for step in steps]


def invoke(session, method, payload):
    options = payload if isinstance(payload, dict) else {}
    if method == "capture":
        delay = _clamp_delay(options.get("delay_seconds", 3))
        time.sleep(delay)
        selector = capture_element_under_cursor()
        session.set_status("已采集屏幕控件")
        return {"ok": True, "data": {"selector": selector}}
    if method == "check":
        result = check_selector(options.get("selector"))
        return {"ok": True, "data": result}
    if method == "to_actions":
        actions = to_actions(options.get("steps"))
        return {"ok": True, "data": {"actions": actions}}
    return {"ok": False, "error": f"未知方法: {method}"}
