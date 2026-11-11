from notmyfault.plugin_api import action_values, platform


def run_with_context(action_info, params, context):
    params = action_values(params)
    plan = params.get("plan", "balanced")
    print(f"[Action:power_plan] 切换电源计划: {plan}")
    profile = platform.power.set_profile(plan, params.get("custom_guid", ""))
    print(f"[Action:power_plan] 已切换到 {plan}（{profile}）")
    return {"plan": plan, "profile": profile}
