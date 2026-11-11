from notmyfault.plugin_api import action_values, platform


def run_with_context(action_info, params, context):
    params = action_values(params)
    text = params.get("text", "")
    if not isinstance(text, str):
        raise ValueError("剪贴板内容必须是文本")
    if not text:
        raise ValueError("没有文本可写入")
    print(f"[Action:clipboard_set] 准备写入剪贴板 ({len(text)} 字符)")
    platform.clipboard.write(text)
    print(f"[Action:clipboard_set] 剪贴板写入成功 ({len(text)} 字符)")
