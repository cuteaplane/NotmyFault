from notmyfault.plugin_api import action_values, platform

_LEVELS = {"max": 100, "half": 50, "min": 0}


def run_with_context(action_info, params, context):
    params = action_values(params)
    action_level = str(params.get("action", "max")).lower().strip()
    if action_level not in (*_LEVELS, "mute"):
        raise ValueError(f"未知音量操作: {action_level}（可选: max, half, min, mute）")
    print(f"[Action:set_volume] 设置音量为 {action_level}")
    if action_level == "mute":
        platform.audio.set_muted(True)
    else:
        platform.audio.set_volume(_LEVELS[action_level])
        platform.audio.set_muted(False)
