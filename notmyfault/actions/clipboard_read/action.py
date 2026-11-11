from notmyfault.plugin_api import action_values, platform


def run_with_context(action_info, params, context):
    params = action_values(params)
    max_chars = int(params.get("max_chars", 1000000))
    if not 1 <= max_chars <= 1000000:
        raise ValueError("最多读取字符数必须在 1 到 1000000 之间")
    text = platform.clipboard.read(max_chars=max_chars + 1)
    value = text or ""
    return {
        "text": value[:max_chars],
        "length": len(value[:max_chars]),
        "has_text": text is not None,
        "truncated": len(value) > max_chars,
    }
