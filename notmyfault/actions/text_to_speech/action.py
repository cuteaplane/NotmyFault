from notmyfault.plugin_api import action_values, platform


def run_with_context(action_info, params, context):
    params = action_values(params)
    platform.speech.speak(
        str(params.get("text", "")).strip(),
        rate=params.get("rate", 0),
        volume=params.get("volume", 100),
        voice=str(params.get("voice", "")).strip(),
    )
