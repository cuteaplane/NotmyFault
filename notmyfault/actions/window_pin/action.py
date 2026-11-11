from notmyfault.plugin_api import action_values, platform


def run_with_context(action_info, params, context):
    params = action_values(params)
    result = platform.window.set_pinned(
        str(params.get("action", "toggle") or "toggle"),
        params.get("target", "active"),
        str(params.get("title", "") or "").strip(),
    )
    print(f"[Action:window_pin] {result['state']}")
    return result
