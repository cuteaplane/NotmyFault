from notmyfault.plugin_api import action_values, platform


def run_with_context(action_info, params, context):
    params = action_values(params)
    platform.desktop.set_wallpaper(params.get("image_path", ""), params.get("style", "fill"))
