import copy
import hmac
import hashlib
import json
import os
import secrets
import sys
from typing import Any, Dict, List

DEFAULT_CONFIG: Dict[str, Any] = {
    "disabled_plugins": {
        "triggers": [],
        "actions": [],
    },
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
def _backup_path() -> str:
    return CONFIG_FILE + ".bak"


def _secret_path() -> str:
    return os.path.join(os.path.dirname(CONFIG_FILE), ".config_secret")
_SIGNATURE_KEY = "_signature"
# 允许的 action 类型白名单（防恶意插件注入）
_ALLOWED_ACTION_TYPES = {"set_volume", "notify", "run_powershell", "launch_program", "kill_process", "lock_screen"}


# ---------------------------------------------------------------------------
# 配置签名与完整性校验
# ---------------------------------------------------------------------------

def _get_or_create_secret() -> bytes:
    """获取或创建配置签名密钥。

    密钥存储在 %APPDATA%/NotmyFault/.config_secret 中。
    每个安装实例有自己的唯一密钥。
    """
    config_dir = os.path.dirname(CONFIG_FILE)
    if config_dir:
        os.makedirs(config_dir, exist_ok=True)

    if os.path.exists(_secret_path()):
        try:
            with open(_secret_path(), "rb") as f:
                return f.read()
        except OSError:
            pass

    secret = secrets.token_bytes(32)
    try:
        with open(_secret_path(), "wb") as f:
            f.write(secret)
    except OSError:
        pass
    return secret


def _sign_config(config: dict) -> str:
    """计算配置的 HMAC-SHA256 签名。

    对配置按 key 排序后序列化，确保签名跨平台可复现。
    """
    secret = _get_or_create_secret()
    content = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hmac.new(secret, content.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_config(config: dict, signature: str) -> bool:
    """验证配置签名。"""
    expected = _sign_config(config)
    return hmac.compare_digest(expected, signature)


def _is_secret_installed() -> bool:
    """检查密钥文件是否存在。"""
    return os.path.exists(_secret_path())


def save_config(config: Dict[str, Any]) -> bool:
    """统一保存配置接口，自动签名并备份。

    Args:
        config: 配置字典

    Returns:
        是否保存成功
    """
    try:
        config_dir = os.path.dirname(CONFIG_FILE)
        if config_dir:
            os.makedirs(config_dir, exist_ok=True)

        # 先备份当前有效配置
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as src:
                    with open(_backup_path(), "w", encoding="utf-8") as dst:
                        dst.write(src.read())
            except OSError:
                pass  # 备份失败不是致命错误

        # 计算签名并保存
        to_save = dict(config)
        to_save[_SIGNATURE_KEY] = _sign_config(to_save)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=4)
        return True
    except OSError as e:
        print(f"[Config] 保存配置失败: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# 危险命令模式检测
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS = [
    # 高危：下载执行
    "Invoke-WebRequest", "Invoke-Expression", "IEX", "Invoke-Command",
    "Start-Process", "Start-Job", "Register-ScheduledJob",
    # 高危：绕过
    "-Bypass", "-EncodedCommand", "-Enc", "FromBase64String",
    # 高危：SMB 协议
    "\\\\\\\\(", "New-PSDrive",
    # 高危：破坏性
    "Remove-Item", "rm -rf", "Format-Volume", "Clear-Disk",
    "Stop-Computer", "Restart-Computer", "Shutdown",
    # 高危：系统配置
    "Set-MpPreference", "Add-MpPreference", "New-Service",
    "Set-ItemProperty", "New-ItemProperty",
    "reg add", "sc config", "bcdedit",
]

def _has_dangerous_command(command: str) -> str | None:
    """检测命令中是否包含危险模式。

    只对 run_powershell 和 launch_program 的参数做检查。
    """
    cmd_lower = command.lower()
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.lower() in cmd_lower:
            return pattern
    return None


def _validate_rules_safety(rules: list) -> list[str]:
    """校验规则中的动作参数是否安全。返回警告列表。"""
    warnings: list[str] = []
    for i, rule in enumerate(rules):
        rule_name = rule.get("name", f"规则 #{i+1}")
        for j, action in enumerate(rule.get("actions", [])):
            action_type = action.get("type", "")

            # action 类型白名单检查
            if action_type and action_type not in _ALLOWED_ACTION_TYPES:
                warnings.append(
                    f"规则 \"{rule_name}\" 使用了非标准的 action 类型: '{action_type}'"
                )

            # 危险命令检测
            if action_type in ("run_powershell",):
                cmd = action.get("params", {}).get("command", "")
                if cmd:
                    danger = _has_dangerous_command(cmd)
                    if danger:
                        warnings.append(
                            f"规则 \"{rule_name}\" 的 PowerShell 命令包含危险模式: '{danger}'"
                        )

            # launch_program 路径检查
            if action_type == "launch_program":
                path = action.get("params", {}).get("path", "")
                if path:
                    dangerous_locations = [
                        "\\\\", "temp\\", "%tmp%\\", "%temp%\\",
                        "powershell.exe", "cmd.exe", "wscript.exe", "cscript.exe",
                    ]
                    path_lower = path.lower()
                    for dl in dangerous_locations:
                        if dl in path_lower:
                            warnings.append(
                                f"规则 \"{rule_name}\" 的启动路径包含危险位置: '{path}'"
                            )
                            break

    return warnings


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

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

    result = {k: v for k, v in config.items() if k != "processes"}
    result["rules"] = rules
    return result if rules else config


def get_config() -> Dict[str, Any]:
    """加载配置，启用签名校验和防篡改检测。"""
    config_dir = os.path.dirname(CONFIG_FILE)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)

    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        print("[DEBUG] Default config created.")
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as config_file:
            raw = config_file.read()
        config = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ERROR] 配置文件损坏 ({e})，尝试从备份恢复...", file=sys.stderr)
        recovered = _try_recover_from_backup()
        if recovered is not None:
            return recovered
        print("[ERROR] 备份也无效，使用默认配置覆盖", file=sys.stderr)
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)

    # --- 签名校验 ---
    signature = config.pop(_SIGNATURE_KEY, "")
    has_secret = _is_secret_installed()

    if has_secret:
        if not signature:
            print("[WARN] 配置文件缺少签名，可能被篡改！已回退默认配置并告警", file=sys.stderr)
            _try_recover_from_backup()
            return copy.deepcopy(DEFAULT_CONFIG)

        if not _verify_config(config, signature):
            print("[!!] 配置文件签名校验失败！文件可能被篡改，尝试从备份恢复", file=sys.stderr)
            recovered = _try_recover_from_backup()
            if recovered is not None:
                return recovered
            print("[!!] 备份也无效，加载默认安全配置", file=sys.stderr)
            save_config(DEFAULT_CONFIG)
            return copy.deepcopy(DEFAULT_CONFIG)

    # 签名验证通过，重新签名保存（修复签名老化问题）
    if has_secret:
        save_config(config)
    else:
        # 首次安装，创建密钥并签名
        _get_or_create_secret()
        save_config(config)

    normalized = _normalize_config(config)
    if normalized != config:
        save_config(normalized)
        print("[DEBUG] Legacy config migrated to new rule format.")
        config = normalized

    # --- 运行时安全校验 ---
    rules = config.get("rules", [])
    safety_warnings = _validate_rules_safety(rules)
    if safety_warnings:
        for w in safety_warnings:
            print(f"[Config] [安全] {w}", file=sys.stderr)

    print("[DEBUG] Config loaded:", config)
    return config


def _try_recover_from_backup() -> Dict[str, Any] | None:
    """尝试从备份文件恢复配置。"""
    if not os.path.exists(_backup_path()):
        return None
    try:
        with open(_backup_path(), "r", encoding="utf-8") as f:
            raw = f.read()
        config = json.loads(raw)
        signature = config.pop(_SIGNATURE_KEY, "")
        if _is_secret_installed() and signature and _verify_config(config, signature):
            print("[INFO] 从备份成功恢复配置", file=sys.stderr)
            save_config(config)
            return config
        # 备份没有签名或签名无效 — 可能是旧版配置直接使用
        print("[WARN] 备份文件无有效签名，但尝试使用", file=sys.stderr)
        save_config(config)
        return config
    except Exception as e:
        print(f"[ERROR] 备份恢复失败: {e}", file=sys.stderr)
        return None
