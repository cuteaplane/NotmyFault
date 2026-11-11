from notmyfault.plugin_api import action_values, platform


def run_with_context(action_info, params, context):
    params = action_values(params)
    mode = params.get("mode", "fullscreen")
    fmt = str(params.get("format", "png")).lower()
    output_path = params.get("output_path", "")
    print(f"[Action:screenshot] 截取{mode} -> {output_path}")
    result = platform.screen.capture(output_path, mode, fmt)
    print(f"[Action:screenshot] 截图已保存: {result}")
    return {"file": result}
