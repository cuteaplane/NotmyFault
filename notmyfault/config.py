import copy
import json
import os
import sys
from typing import Any, Dict, List

DEFAULT_CONFIG: Dict[str, Any] = {
    "rules": [
        {
            "name": "微信音量规则",
            "event": {
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
            "event": {
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
            "event": {
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
        },
        {
            "name": "微信退出-恢复音量",
            "event": {
                "type": "process_state",
                "params": {
                    "process_name": "WeChat.exe",
                    "state": "stopped"
                }
            },
            "actions": [
                {"type": "set_volume", "params": {"action": "half"}},
                {
                    "type": "notify",
                    "params": {
                        "title": "微信已退出",
                        "message": "音量已恢复至50%"
                    }
                }
            ]
        },
        {
            "name": "PPT退出-恢复音量",
            "event": {
                "type": "process_state",
                "params": {
                    "process_name": "POWERPNT.EXE",
                    "state": "stopped"
                }
            },
            "actions": [
                {"type": "set_volume", "params": {"action": "half"}},
                {
                    "type": "notify",
                    "params": {
                        "title": "PowerPoint已退出",
                        "message": "音量已恢复至50%"
                    }
                }
            ]
        },
        {
            "name": "媒体播放器退出-恢复音量",
            "event": {
                "type": "process_state",
                "params": {
                    "process_name": "wmplayer.exe",
                    "state": "stopped"
                }
            },
            "actions": [
                {"type": "set_volume", "params": {"action": "half"}},
                {
                    "type": "notify",
                    "params": {
                        "title": "媒体播放器已退出",
                        "message": "音量已恢复至50%"
                    }
                }
            ]
        }
    ]
}

CONFIG_FILE = os.path.join(os.getenv("APPDATA", ""), "NotmyFault", "config.json")


def _normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return config

    if isinstance(config.get("rules"), list):
        normalized_rules = []
        for rule in config.get("rules", []):
            if not isinstance(rule, dict):
                continue

            if "trigger" in rule and "event" not in rule:
                copied = dict(rule)
                copied["event"] = copied.pop("trigger")
                normalized_rules.append(copied)
            else:
                normalized_rules.append(rule)

        # 保留 rules 之外的其他顶层键
        result = dict(config)
        result["rules"] = normalized_rules
        return result

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
                "event": {
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

    # 保留 processes 之外的其他顶层键，删除 processes 并替换为 rules
    result = {k: v for k, v in config.items() if k != "processes"}
    result["rules"] = rules
    return result if rules else config


def get_config() -> Dict[str, Any]:
    config_dir = os.path.dirname(CONFIG_FILE)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)

    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as config_file:
            json.dump(DEFAULT_CONFIG, config_file, ensure_ascii=False, indent=4)
        print("[DEBUG] Default config created.")
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (json.JSONDecodeError, OSError):
        print(
            "[ERROR] 配置文件损坏，使用默认配置覆盖",
            file=sys.stderr,
        )
        with open(CONFIG_FILE, "w", encoding="utf-8") as config_file:
            json.dump(DEFAULT_CONFIG, config_file, ensure_ascii=False, indent=4)
        return copy.deepcopy(DEFAULT_CONFIG)

    normalized = _normalize_config(config)
    if normalized != config:
        with open(CONFIG_FILE, "w", encoding="utf-8") as config_file:
            json.dump(normalized, config_file, ensure_ascii=False, indent=4)
        print("[DEBUG] Legacy config migrated to new rule format.")
        config = normalized

    print("[DEBUG] Config loaded:", config)
    return config
