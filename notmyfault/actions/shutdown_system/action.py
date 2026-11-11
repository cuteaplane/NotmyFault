"""系统关机、重启、注销、休眠和睡眠动作
force 默认关闭，params.confirm 必须为 true，执行失败时抛异常
"""
from notmyfault.plugin_api import action_values, platform

import time


_ALLOWED_ACTIONS = ("shutdown", "restart", "logoff", "hibernate", "sleep")

def _execute(params, cancellation=None):
    action = params.get("action", "shutdown")
    force = params.get("force", False)
    confirm = params.get("confirm", False)
    delay = params.get("delay_seconds", 0)

    try:
        delay = max(0.0, float(delay))
    except (TypeError, ValueError):
        raise ValueError(
            f"delay_seconds 必须是数字，实际: {delay!r}"
        ) from None

    if action not in _ALLOWED_ACTIONS:
        raise ValueError(
            f"不支持的操作: {action}（可选: {', '.join(_ALLOWED_ACTIONS)}）"
        )
    if confirm is not True:
        raise PermissionError(
            "shutdown_system 需要显式设置 confirm=true 才会执行"
        )
    if not isinstance(force, bool):
        raise ValueError(f"force 必须为布尔值，实际: {force!r}")

    if delay > 0:
        print(f"[Action:shutdown_system] 等待 {delay}s 后执行: {action}")
        if cancellation is not None:
            if cancellation.wait(delay):
                cancellation.raise_if_cancelled()
        else:
            time.sleep(delay)

    if cancellation is not None:
        cancellation.raise_if_cancelled()

    print(f"[Action:shutdown_system] 执行: {action} (force={force})")

    platform.power.system(action, force=force)


def run_with_context(action_info, params, context):
    params = action_values(params)
    runtime = context.get("runtime", {})
    cancellation = runtime.get("cancellation")
    if cancellation is None:
        raise RuntimeError("shutdown_system 缺少 runtime-v1 取消对象")
    return _execute(params, cancellation)
