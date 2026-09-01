"""通过 Windows UI Automation 重新定位并操作录制控件"""

from notmyfault.plugin_api import native_uia_api

perform_selector = native_uia_api().perform_selector


def run_with_context(action_info, params, context):
    target = params.get("target")
    if not isinstance(target, dict):
        raise ValueError("请先选择要操作的屏幕控件")
    operation = str(params.get("operation", "invoke") or "invoke")
    text = params.get("text", "")
    if not isinstance(text, str):
        raise ValueError("写入屏幕控件的内容必须是文本")
    runtime = context.get("runtime", {})
    cancellation = runtime.get("cancellation") if isinstance(runtime, dict) else None
    return perform_selector(target, operation, cancellation, text)


def run(action_info, params):
    return run_with_context(action_info, params, {})
