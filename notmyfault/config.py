import copy
import json
import os
from typing import Any, Dict, List

DEFAULT_CONFIG: Dict[str, Any] = {
    "rules": [
        {
            "trigger": {
                "type": "process_state",
                "params": {
                    "process_name": "WeChat",
                    "state": "running"
                }
            },
            "actions": [
                {"type": "set_volume", "params": {"action": "max"}},
                {
                    "type": "notify",
                    "params": {
                        "title": "微信正在运行",
                        "message": "音量已设置为100%"
                    }
                }
            ]
        },
        {
            "trigger": {
                "type": "process_state",
                "params": {
                    "process_name": "POWERPNT",
                    "state": "running"
                }
            },
            "actions": [
                {"type": "set_volume", "params": {"action": "max"}},
                {
                    "type": "notify",
                    "params": {
                        "title": "PowerPoint正在运行",
                        "message": "音量已设置为100%"
                    }
                }
            ]
        },
        {
            "trigger": {
                "type": "process_state",
                "params": {
                    "process_name": "wmplayer",
                    "state": "running"
                }
            },
            "actions": [
                {"type": "set_volume", "params": {"action": "max"}},
                {
                    "type": "notify",
                    "params": {
                        "title": "Windows Media Player运行中",
                        "message": "音量已设置为100%"
                    }
                }
            ]
        }
    ]
}

CONFIG_FILE = os.path.join(os.getenv("APPDATA", ""), "NotmyFault", "config.json")


def _migrate_legacy_config(config: Dict[str, Any]) -> Dict[str, Any]:
    processes = config.get("processes")
    if not isinstance(processes, list):
        return config

    rules: List[Dict[str, Any]] = []
    for process in processes:
        notification = process.get("notification", {})
        rules.append(
            {
                "trigger": {
                    "type": "process_state",
                    "params": {
                        "process_name": process.get("process_name", ""),
                        "state": "running"
                    }
                },
                "actions": [
                    {"type": "set_volume", "params": {"action": process.get("volume_action", "max")}},
                    {
                        "type": "notify",
                        "params": {
                            "title": notification.get("title", process.get("process_name", "")),
                            "message": notification.get("message", "")
                        }
                    }
                ]
            }
        )

    return {"rules": rules}


def get_config() -> Dict[str, Any]:
    config_dir = os.path.dirname(CONFIG_FILE)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)

    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as config_file:
            json.dump(DEFAULT_CONFIG, config_file, ensure_ascii=False, indent=4)
        print("[DEBUG] Default config created.")
        return copy.deepcopy(DEFAULT_CONFIG)

    with open(CONFIG_FILE, "r", encoding="utf-8") as config_file:
        config = json.load(config_file)

    normalized = _migrate_legacy_config(config)
    if normalized != config:
        with open(CONFIG_FILE, "w", encoding="utf-8") as config_file:
            json.dump(normalized, config_file, ensure_ascii=False, indent=4)
        print("[DEBUG] Legacy config migrated to new rule format.")
        config = normalized

    print("[DEBUG] Config loaded:", config)
    return config
