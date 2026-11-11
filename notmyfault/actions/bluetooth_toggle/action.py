"""通过系统蓝牙无线电接口查询或切换蓝牙状态。"""

from notmyfault.plugin_api import action_values, platform


def run_with_context(_action_info, params, context):
    params = action_values(params)
    action = str(params.get("action", "toggle")).strip().lower()
    if action not in ("toggle", "on", "off", "query"):
        raise ValueError(f"不支持的蓝牙操作: {action}")
    cancellation = context.get("runtime", {}).get("cancellation")
    if cancellation:
        cancellation.raise_if_cancelled()
    if action == "query":
        result = {
            "state": platform.bluetooth.state(), "changed": False,
            "method": platform.capability("bluetooth.control").backend,
        }
    else:
        if action == "toggle":
            enabled = platform.bluetooth.state() != "on"
            if cancellation:
                cancellation.raise_if_cancelled()
        else:
            enabled = action == "on"
        result = platform.bluetooth.set(enabled)
    if cancellation:
        cancellation.raise_if_cancelled()
    return {**result, "action": action}
