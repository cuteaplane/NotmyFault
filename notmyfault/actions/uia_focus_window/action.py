"""切换到录制控件所属的窗口"""

from notmyfault.native.uia import focus_selector_window


def run_with_context(action_info, params, context):
    target = params.get("target")
    if not isinstance(target, dict):
        raise ValueError("请先选择目标窗口里的屏幕控件")
    runtime = context.get("runtime", {})
    cancellation = runtime.get("cancellation") if isinstance(runtime, dict) else None
    return focus_selector_window(target, cancellation)


def run(action_info, params):
    return run_with_context(action_info, params, {})
