from notmyfault.plugin_api import action_values, platform

_COMMANDS = frozenset({"play_pause", "stop", "next", "previous", "volume_up", "volume_down", "mute"})


def run_with_context(action_info, params, context):
    params = action_values(params)
    command = str(params.get("command", "play_pause") or "play_pause")
    if command not in _COMMANDS:
        raise ValueError(f"未知媒体命令: {command!r}")
    print(f"[Action:media_control] 发送媒体命令: {command}")
    platform.media.send(command)
    print(f"[Action:media_control] 已发送: {command}")
    return {"command": command}
