from notmyfault.plugin_api import action_values, platform


def run_with_context(action_info, params, context):
    params = action_values(params)
    mode = params.get("mode", "type_text")
    if mode == "type_text":
        text = str(params.get("text", "") or "")
        if not text:
            raise ValueError("没有要输入的文本")
        platform.keyboard.type_text(text)
    elif mode == "hotkey":
        keys = str(params.get("keys", "") or "")
        parts = [part for part in keys.replace("+", " ").split() if part]
        platform.keyboard.send_hotkey(parts)
    else:
        raise ValueError(f"未知模式: {mode!r}（可选: type_text/hotkey）")
    print(f"[Action:send_keys] 已发送: mode={mode}")
    return {"mode": mode}
