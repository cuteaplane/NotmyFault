"""连接或断开 WiFi。"""

from notmyfault.plugin_api import action_values, platform


def run_with_context(action_info, params, context):
    params = action_values(params)
    action = str(params.get("action", "connect") or "connect").strip().lower()
    if action not in ("connect", "disconnect"):
        raise ValueError(f"不支持的 WiFi 操作: {action}")
    if action == "disconnect":
        return platform.wifi.disconnect()
    ssid = str(params.get("ssid", "") or "")
    if not ssid:
        raise ValueError("连接 WiFi 时必须填写目标 SSID")
    password = str(params.get("password", "") or "") or None
    return platform.wifi.connect(ssid, password=password)
