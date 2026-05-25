from notmyfault.volume import set_volume


def run(action_info, params):
    action_level = params.get("action", "max")
    print(f"[Action:set_volume] 设置音量为 {action_level}")
    set_volume(action_level)
