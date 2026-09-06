"""UIA 自动化插件的屏幕控件选择命令。"""

import copy
import time

if __package__ == "notmyfault.actions.uia_automation":
    from .nmf_uia_plugin.uia import (
        DesktopElementError,
        capture_element_under_cursor,
        check_selector,
        validate_selector,
    )
else:
    from nmf_uia_plugin.uia import (
        DesktopElementError,
        capture_element_under_cursor,
        check_selector,
        validate_selector,
    )


def _clamp_delay(value) -> float:
    try:
        delay = float(value)
    except (TypeError, ValueError):
        delay = 3.0
    return min(max(delay, 1.0), 10.0)


def _selector_summary(selector: dict) -> str:
    display = selector.get("display")
    if not isinstance(display, dict):
        return "1 个屏幕控件"
    control = str(display.get("control") or "未命名控件")
    app = str(display.get("app") or display.get("window") or "未知程序")
    return f"屏幕控件 · {control} · {app}"


def _current_selector(context, payload=None) -> dict | None:
    options = payload if isinstance(payload, dict) else {}
    selector = options.get("selector")
    if not isinstance(selector, dict):
        selector = context.session.data.get("selector")
    if not isinstance(selector, dict):
        selector = context.current_value
    return copy.deepcopy(selector) if isinstance(selector, dict) else None


def edit_selector(context, payload):
    selector = _current_selector(context, payload)
    context.session.data["selector"] = selector
    context.session.set_status("屏幕控件编辑器已打开")
    return context.open_view(
        "selector_workbench",
        {"selector": selector},
    )


def capture_selector(context, payload):
    options = payload if isinstance(payload, dict) else {}
    try:
        time.sleep(_clamp_delay(options.get("delay_seconds", 3)))
        selector = capture_element_under_cursor()
    except (DesktopElementError, ValueError) as error:
        return context.error(str(error))
    context.session.data["selector"] = copy.deepcopy(selector)
    context.session.set_status("已采集屏幕控件")
    return context.result({"selector": selector})


def verify_selector(context, payload):
    selector = _current_selector(context, payload)
    if not isinstance(selector, dict):
        return context.error("请先选择屏幕控件")
    try:
        result = check_selector(selector)
    except (DesktopElementError, ValueError) as error:
        return context.error(str(error))
    context.session.data["selector"] = copy.deepcopy(selector)
    return context.result({"selector": selector, "check": result})


def commit_selector(context, payload):
    selector = _current_selector(context, payload)
    if not isinstance(selector, dict):
        return context.error("请先选择屏幕控件")
    try:
        selector = validate_selector(selector)
    except (DesktopElementError, ValueError) as error:
        return context.error(str(error))
    context.session.data["selector"] = copy.deepcopy(selector)
    context.session.set_status("屏幕控件已保存")
    return context.commit(selector, _selector_summary(selector))


def cancel_selector(context, payload):
    context.session.data["selector"] = None
    context.session.set_status("编辑已取消")
    return context.result(close=True)
