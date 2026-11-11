import math

from notmyfault.plugin_api import action_values, platform


def _brightness_level(params) -> int:
    action = params.get("action", "off")
    if action != "set_brightness":
        raise ValueError(f"不支持的亮度操作: {action}")
    raw_level = params.get("brightness", 50)
    if isinstance(raw_level, bool):
        raise ValueError("亮度必须是 0–100 的整数")
    try:
        numeric_level = float(raw_level)
    except (TypeError, ValueError) as error:
        raise ValueError("亮度必须是 0–100 的整数") from error
    if (
        not math.isfinite(numeric_level)
        or not numeric_level.is_integer()
        or not 0 <= numeric_level <= 100
    ):
        raise ValueError("亮度必须是 0–100 的整数")
    return int(numeric_level)


def validate_params(_action_info, params):
    if params.get("action", "off") == "set_brightness":
        try:
            _brightness_level(params)
        except ValueError as error:
            return [str(error)]
    return []


def run_with_context(action_info, params, context):
    params = action_values(params)
    action = params.get("action", "off")
    print(f"[Action:display_control] 执行: {action}")
    if action == "set_brightness":
        result = platform.display.set_brightness(_brightness_level(params))
    else:
        result = platform.display.set_power(action)
    print(f"[Action:display_control] 操作完成: {action}")
    return {"action": action, **result}
