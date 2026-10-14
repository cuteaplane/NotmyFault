from notmyfault.plugin_api import action_values
from .volume import set_volume


def run_with_context(action_info, params, context):
    params = action_values(params)
    action_level = params.get("action", "max")
    print(f"[Action:set_volume] 设置音量为 {action_level}")
    set_volume(action_level)
