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
        "actions": []
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


# ---------------------------------------------------------------------------
# 配置签名与完整性校验
# ---------------------------------------------------------------------------

def _secure_write_secret(path: str, data: bytes) -> None:
    """安全写入密钥文件，限制权限仅当前用户可访问。

    - 用 os.open 创建文件并设置 0o600（Unix 生效；Windows 部分生效）
    - Windows 上额外用 icacls 移除继承权限，仅保留当前用户 Full control
      （需要 F 权限而非 R，因为 _get_or_create_secret 可能需要重新写入 secret；
      os.getlogin() 在某些环境下返回的用户名不被 icacls 识别，改用 %USERNAME%）
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if os.name == "nt":
            try:
                import subprocess as _sp
                # os.getlogin()/USERNAME 只返回用户名，icacls 会把计算机名
                # 当成域名解析失败（如 CUTEAPLANE\:(R) 无人有权限）。
                # 必须用 domain\user 完整格式。优先 %USERDOMAIN%\%USERNAME%。
                userdomain = os.environ.get("USERDOMAIN", "")
                username = os.environ.get("USERNAME") or os.getlogin()
                full_user = f"{userdomain}\\{username}" if userdomain else username
                # 先 grant 再 inheritance：如果 grant 失败（用户名解析问题），
                # 不移除继承权限，至少保留默认权限让文件可读写。
                r1 = _sp.run(
                    ["icacls", path, "/grant:r", f"{full_user}:F"],
                    capture_output=True, timeout=5,
                )
                if r1.returncode == 0:
                    _sp.run(
                        ["icacls", path, "/inheritance:r"],
                        capture_output=True, timeout=5,
                    )
            except Exception:
                pass
    except OSError:
        pass


def _get_or_create_secret() -> bytes:
    """获取或创建配置签名密钥。

    密钥存储在 %APPDATA%/NotmyFault/.config_secret 中。
    每个安装实例有自己的唯一密钥。
    文件权限限制为仅当前用户可读（PoC-8 修复）。
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
    _secure_write_secret(_secret_path(), secret)
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
    """统一保存配置接口，规范化、自动签名并备份。

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

        # 先清理界面已废弃字段，再对实际落盘内容签名。
        to_save = _normalize_config(config)
        if not isinstance(to_save, dict):
            raise ValueError("配置根节点必须是对象")
        to_save = dict(to_save)
        to_save[_SIGNATURE_KEY] = _sign_config(to_save)
        tmp_path = CONFIG_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, CONFIG_FILE)
        except OSError:
            # os.replace 失败（Windows 下目标被占用）：退回直接写活配置文件。
            # 但 'w' 会先截断，若写到一半失败（磁盘满等）会把活配置写坏，
            # 所以先备份当前内容，写失败时还原。
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            backup = None
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as bf:
                    backup = bf.read()
            except OSError:
                pass
            try:
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(to_save, f, ensure_ascii=False, indent=4)
            except OSError:
                if backup is not None:
                    try:
                        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                            f.write(backup)
                    except OSError:
                        pass
                raise
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
    # 高危：脚本块/间接调用绕过（PoC-3 修复）
    "[scriptblock]::create", "[scriptblock]::",
    "get-command", "get-alias",
    ".invoke()",
    "icm",  # Invoke-Command 别名
    "iex ",  # Invoke-Expression 别名（带空格避免误匹配子串）
]

# launch_program 危险路径黑名单（命中归入 errors 拒绝）
_DANGEROUS_LAUNCH_PATHS = [
    "\\\\", "temp\\", "%tmp%\\", "%temp%\\",
    "powershell.exe", "cmd.exe", "wscript.exe", "cscript.exe",
    "powers~",   # 8.3 短名绕过
    "rundll32", "regsvr32", "wmic", "mshta", "certutil", "bitsadmin",
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


def _validate_rules_safety(rules: list) -> tuple[list[str], list[str]]:
    """校验规则中的动作参数是否安全。

    Returns:
        (warnings, errors):
        - warnings: 提醒类问题（非标准 action 类型等），不阻止写入
        - errors: 危险模式命中（危险命令/危险路径），应拒绝写入
    """
    warnings: list[str] = []
    errors: list[str] = []
    for i, rule in enumerate(rules):
        rule_name = rule.get("name", f"规则 #{i+1}")
        for j, action in enumerate(rule.get("actions", [])):
            action_type = action.get("type", "")

            # 危险命令检测（error - 命中拒绝）
            if action_type in ("run_powershell",):
                cmd = action.get("params", {}).get("command", "")
                if cmd:
                    danger = _has_dangerous_command(cmd)
                    if danger:
                        errors.append(
                            f"规则 \"{rule_name}\" 的 PowerShell 命令包含危险模式: '{danger}'"
                        )

            # launch_program 路径检查（error - 命中拒绝）
            if action_type == "launch_program":
                path = action.get("params", {}).get("path", "")
                if path:
                    path_lower = path.lower()
                    for dl in _DANGEROUS_LAUNCH_PATHS:
                        if dl in path_lower:
                            errors.append(
                                f"规则 \"{rule_name}\" 的启动路径包含危险位置: '{path}'"
                            )
                            break

    return warnings, errors


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

class ConfigValidationError(ValueError):
    """运行时配置未通过完整性或安全校验。"""


def load_verified_config() -> Dict[str, Any]:
    """读取一份可安全应用到运行中引擎的配置快照。

    与 :func:`get_config` 的启动恢复策略不同，这个入口绝不回退默认配置、
    也不写回磁盘。热重载必须保持当前已验证的运行快照，直到新文件同时
    通过签名、结构和规则安全校验。
    """
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as config_file:
            raw = json.load(config_file)
    except (json.JSONDecodeError, OSError) as e:
        raise ConfigValidationError(f"配置文件无法解析: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigValidationError("配置根节点必须是对象")

    signature = raw.pop(_SIGNATURE_KEY, "")
    if not _is_secret_installed():
        raise ConfigValidationError("配置签名密钥缺失")
    if not signature:
        raise ConfigValidationError("配置缺少签名")
    if not _verify_config(raw, signature):
        raise ConfigValidationError("配置签名校验失败")

    normalized = _normalize_config(raw)
    if not isinstance(normalized, dict):
        raise ConfigValidationError("规范化后的配置必须是对象")
    rules = normalized.get("rules", [])
    if not isinstance(rules, list):
        raise ConfigValidationError("rules 必须是列表")

    _warnings, errors = _validate_rules_safety(rules)
    if errors:
        raise ConfigValidationError("规则安全校验失败: " + "; ".join(errors[:3]))
    return normalized

def _normalize_condition(condition: Any) -> Any:
    """把旧条件树转换成统一的 ``op + children`` 格式。"""
    if not isinstance(condition, dict):
        return condition

    copied = dict(condition)
    children = copied.get("children", copied.get("events"))
    # 有 children/events 的节点是条件组；没有二者且带 type 的节点是事件叶子，
    # 叶子的 type 绝不能被当作组操作符删除。
    if not isinstance(children, list):
        return copied

    op = copied.get("op", copied.get("type", "any"))
    copied["op"] = "all" if op in ("all", "and") else "any"
    copied["children"] = [_normalize_condition(child) for child in children]
    copied.pop("events", None)
    copied.pop("type", None)
    if copied["op"] == "any":
        # within_seconds 只有 all 条件组才有意义；旧版界面曾只隐藏它而没有删除。
        copied.pop("within_seconds", None)
    return copied


def _unwrap_single_condition(condition: Any) -> Dict[str, Any] | None:
    """从只有一个分支的条件组中取出事件，用于消除旧版重复字段。"""
    current = condition
    while isinstance(current, dict):
        children = current.get("children")
        if isinstance(children, list):
            if len(children) != 1:
                return None
            current = children[0]
            continue
        return current if isinstance(current.get("type"), str) else None
    return None


def _replace_step_references(value: Any, replacements: Dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(f"steps.{old}.", f"steps.{new}.")
        return value
    if isinstance(value, list):
        return [_replace_step_references(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_step_references(item, replacements) for key, item in value.items()}
    return value


def _normalize_rule_actions(actions: Any) -> Any:
    """移除旧步骤 ID，并把能确定的旧引用改为自动步骤名。"""
    if not isinstance(actions, list):
        return actions

    ids: Dict[str, str] = {}
    duplicate_ids = set()
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        old_id = action.get("id")
        if not isinstance(old_id, str) or not old_id:
            continue
        generated = f"{action.get('type', 'action')}_{index + 1}"
        if old_id in ids:
            duplicate_ids.add(old_id)
        else:
            ids[old_id] = generated
    replacements = {old: new for old, new in ids.items() if old not in duplicate_ids}

    normalized = []
    for action in actions:
        if not isinstance(action, dict):
            normalized.append(action)
            continue
        copied = dict(action)
        copied.pop("id", None)
        normalized.append(_replace_step_references(copied, replacements))
    return normalized


def _normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return config

    if isinstance(config.get("rules"), list):
        normalized_rules = []
        for rule in config.get("rules", []):
            if not isinstance(rule, dict):
                continue
            copied = dict(rule)
            if "trigger" in copied:
                if "event" not in copied:
                    copied["event"] = copied["trigger"]
                copied.pop("trigger", None)
            if "condition" in copied:
                copied["condition"] = _normalize_condition(copied["condition"])
                if "event" not in copied and isinstance(copied["condition"], dict):
                    if not isinstance(copied["condition"].get("children"), list):
                        copied["event"] = copied.pop("condition")
                elif isinstance(copied.get("event"), dict):
                    condition_event = _unwrap_single_condition(copied["condition"])
                    if condition_event == copied["event"]:
                        copied.pop("condition", None)
            if "actions" in copied:
                copied["actions"] = _normalize_rule_actions(copied["actions"])
            normalized_rules.append(copied)

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

        _rule = {
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
        rules.append(_rule)

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

    normalized = _normalize_config(config)
    migrated = normalized != config
    config = normalized

    # 签名验证通过后，以规范化结果重新签名并原子写回。
    if has_secret:
        save_config(config)
    else:
        # 首次安装，创建密钥并签名
        _get_or_create_secret()
        save_config(config)

    if migrated:
        print("[DEBUG] Legacy config migrated to new rule format.")

    # --- 运行时安全校验 ---
    rules = config.get("rules", [])
    safety_warnings, safety_errors = _validate_rules_safety(rules)
    if safety_warnings:
        for w in safety_warnings:
            print(f"[Config] [安全] {w}", file=sys.stderr)
    if safety_errors:
        for e in safety_errors:
            print(f"[Config] [安全-严重] {e}", file=sys.stderr)

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
            normalized = _normalize_config(config)
            save_config(normalized)
            return normalized
        # 备份没有签名或签名无效 — 可能是旧版配置直接使用
        print("[WARN] 备份文件无有效签名，但尝试使用", file=sys.stderr)
        normalized = _normalize_config(config)
        save_config(normalized)
        return normalized
    except Exception as e:
        print(f"[ERROR] 备份恢复失败: {e}", file=sys.stderr)
        return None
