from notmyfault.plugin_api import platform


def run_with_context(action_info, params, context):
    platform.clipboard.clear()
    print("[Action:clipboard_clear] 剪贴板已清空")
    return {"cleared": True}
