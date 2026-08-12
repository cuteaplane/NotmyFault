"""读取录制的 Windows UI Automation 控件文本"""

from notmyfault.native.uia import read_selector_text


def run_with_context(action_info, params, context):
    target = params.get("target")
    if not isinstance(target, dict):
        raise ValueError("请先选择要读取的屏幕控件")
    runtime = context.get("runtime", {})
    cancellation = runtime.get("cancellation") if isinstance(runtime, dict) else None
    return read_selector_text(target, cancellation)


def run(action_info, params):
    return run_with_context(action_info, params, {})
