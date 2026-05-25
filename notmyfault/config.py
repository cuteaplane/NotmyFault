import json
import os
from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "processes": [
        {
            "process_name": "WeChat",
            "software_name": "微信",
            "volume_action": "max",
            "notification": {
                "title": "微信正在运行",
                "message": "音量已设置为100%"
            }
        },
        {
            "process_name": "POWERPNT",
            "software_name": "PowerPoint",
            "volume_action": "max",
            "notification": {
                "title": "PowerPoint正在运行",
                "message": "音量已设置为100%"
            }
        },
        {
            "process_name": "wmplayer",
            "software_name": "Windows Media Player",
            "volume_action": "max",
            "notification": {
                "title": "Windows Media Player运行中",
                "message": "音量已设置为100%"
            }
        }
    ]
}

CONFIG_FILE = os.path.join(os.getenv("APPDATA", ""), "NotmyFault", "config.json")


def get_config() -> Dict[str, Any]:
    config_dir = os.path.dirname(CONFIG_FILE)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)

    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as config_file:
            json.dump(DEFAULT_CONFIG, config_file, ensure_ascii=False, indent=4)
        print("[DEBUG] Default config created.")
        return DEFAULT_CONFIG.copy()

    with open(CONFIG_FILE, "r", encoding="utf-8") as config_file:
        config = json.load(config_file)
    print("[DEBUG] Config loaded:", config)
    return config
