"""操作宏在编辑和执行入口共用的结构与数量校验。"""

import copy

from notmyfault.plugin_api import native_keyboard_api, native_uia_api


validate_key_event = native_keyboard_api().validate_key_event
_uia = native_uia_api()
DesktopElementError = _uia.DesktopElementError
validate_window_signature = _uia.validate_window_signature

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


def validate_steps(steps) -> list:
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
