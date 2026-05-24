import copy
import json
import os
from typing import Any, Dict, List

DEFAULT_CONFIG: Dict[str, Any] = {
    "rules": [
        {
            "name": "微信音量规则",
            "trigger": {
                "type": "process_state",
                "params": {
                    "process_name": "WeChat.exe",
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
            "name": "PPT音量规则",
            "trigger": {
                "type": "process_state",
                "params": {
                    "process_name": "POWERPNT.EXE",
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
            "name": "媒体播放器规则",
            "trigger": {
                "type": "process_state",
                "params": {
                    "process_name": "wmplayer.exe",
                    "state": "running"
                }
            },
            "actions": [
                {"type": "set_volume", "params": {"action": "half"}},
                {
                    "type": "notify",
                    "params": {
                        "title": "媒体播放器检测",
                        "message": "音量已调整为50%"
                    }
                }
            ]
        }
    ]
}

CONFIG_FILE = os.path.join(os.getenv("APPDATA", ""), "NotmyFault", "config.json")


def _normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(config, dict) and isinstance(config.get("rules"), list):
        return config

    processes = config.get("processes")
    if not isinstance(processes, list):
        return config

    rules: List[Dict[str, Any]] = []
    for process in processes:
        if not isinstance(process, dict):
            continue

        process_name = process.get("process_name", "")
        software_name = process.get("software_name", process_name)
        volume_action = process.get("volume_action", "max")
        notification = process.get("notification", {}) or {}

        rules.append(
            {
                "name": f"{software_name} 音量规则",
                "trigger": {
                    "type": "process_state",
                    "params": {
                        "process_name": process_name,
                        "state": "running"
                    }
                },
                "actions": [
                    {"type": "set_volume", "params": {"action": volume_action}},
                    {
                        "type": "notify",
                        "params": {
                            "title": notification.get("title", f"{software_name} 正在运行"),
                            "message": notification.get("message", "")
                        }
                    }
                ]
            }
        )

    return {"rules": rules} if rules else config


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

    normalized = _normalize_config(config)
    if normalized != config:
        with open(CONFIG_FILE, "w", encoding="utf-8") as config_file:
            json.dump(normalized, config_file, ensure_ascii=False, indent=4)
        print("[DEBUG] Legacy config migrated to new rule format.")
        config = normalized

    print("[DEBUG] Config loaded:", config)
    return config
