from notmyfault.plugin_api import action_values, platform, PlatformServiceError


def run_with_context(action_info, params, context):
    params = action_values(params)
    if params.get("working_directory"):
        raise PlatformServiceError(
            "desktop.shortcut", "invalid",
            "当前快捷方式接口没有 working_directory 参数，不能设置非空工作目录",
        )
    return platform.desktop.create_shortcut(
        str(params.get("target_path", "") or ""),
        str(params.get("location", "desktop") or "desktop"),
        str(params.get("name", "") or "").strip(),
        arguments=str(params.get("arguments", "") or ""),
        icon=str(params.get("icon_path", "") or ""),
    )
