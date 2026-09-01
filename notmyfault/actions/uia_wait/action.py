"""等待录制的 Windows UI Automation 控件出现"""

from notmyfault.plugin_api import native_uia_api

wait_for_selector = native_uia_api().wait_for_selector


def run_with_context(action_info, params, context):
    target = params.get("target")
    if not isinstance(target, dict):
        raise ValueError("请先选择要等待的屏幕控件")
    runtime = context.get("runtime", {})
    cancellation = runtime.get("cancellation") if isinstance(runtime, dict) else None
    return wait_for_selector(
        target,
        params.get("wait_seconds", 30),
        cancellation,
    )


def run(action_info, params):
    return run_with_context(action_info, params, {})
