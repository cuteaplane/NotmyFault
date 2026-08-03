import copy
import hmac
import hashlib
import json
import os
import re
import secrets
import sys
from typing import Any, Dict, List

from notmyfault.platform.platform_support import get_config_dir
from notmyfault.core.bindings import is_reference


_BINDING_ID_RE = re.compile(r"^[tap]_[a-z0-9_]{6,64}$")
_LEGACY_TEMPLATE_RE = re.compile(r"{{\s*([a-zA-Z_][\w.]*)\s*}}")


def _new_binding_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _ensure_condition_binding_ids(
    condition: Any,
    seen: set[str],
) -> Any:
    """给条件树叶子补充持久、可被动作引用的运行时身份"""
    if not isinstance(condition, dict):
        return condition
    copied = dict(condition)
    children = copied.get("children")
    if isinstance(children, list):
        copied["children"] = [
            _ensure_condition_binding_ids(child, seen) for child in children
        ]
        return copied

    binding_id = copied.get("binding_id")
    if (
        not isinstance(binding_id, str)
        or not _BINDING_ID_RE.fullmatch(binding_id)
        or not binding_id.startswith("t_")
        or binding_id in seen
    ):
        binding_id = _new_binding_id("t")
    copied["binding_id"] = binding_id
    seen.add(binding_id)
    return copied


def ensure_rule_binding_ids(rule: Dict[str, Any]) -> Dict[str, Any]:
    """规范化一条规则中可产生/消费运行数据的节点身份"""
    copied = dict(rule)
    seen: set[str] = set()
    if isinstance(copied.get("event"), dict):
        copied["event"] = _ensure_condition_binding_ids(copied["event"], seen)
    if isinstance(copied.get("condition"), dict):
        copied["condition"] = _ensure_condition_binding_ids(
            copied["condition"], seen
        )

    for field, prefix in (("preconditions", "p"), ("actions", "a")):
        items = copied.get(field)
        if not isinstance(items, list):
            continue
        normalized = []
        for item in items:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            item_copy = dict(item)
            binding_id = item_copy.get("binding_id")
            if (
                not isinstance(binding_id, str)
                or not _BINDING_ID_RE.fullmatch(binding_id)
                or not binding_id.startswith(f"{prefix}_")
                or binding_id in seen
            ):
                binding_id = _new_binding_id(prefix)
            item_copy["binding_id"] = binding_id
            seen.add(binding_id)
            normalized.append(item_copy)
        copied[field] = normalized
    return copied

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

CONFIG_FILE = os.path.join(get_config_dir(), "config.json")
def _backup_path() -> str:
    return CONFIG_FILE + ".bak"


def _secret_path() -> str:
    return os.path.join(os.path.dirname(CONFIG_FILE), ".config_secret")
_SIGNATURE_KEY = "_signature"


def _secure_write_secret(path: str, data: bytes) -> None:
    """写入配置密钥并把文件权限限制为当前用户"""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if os.name == "nt":
            try:
                import subprocess as _sp
                # icacls 需要 domain\\user 格式，USERDOMAIN 和 USERNAME 能组成可识别的账户名
                userdomain = os.environ.get("USERDOMAIN", "")
                username = os.environ.get("USERNAME") or os.getlogin()
                full_user = f"{userdomain}\\{username}" if userdomain else username
                # 先确认授权成功再移除继承权限，授权失败时保留默认权限
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
    """读取配置签名密钥，不存在时创建一个只供当前用户访问的密钥"""
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
    """按排序后的 JSON 计算可复现的 HMAC-SHA256 配置签名"""
    secret = _get_or_create_secret()
    content = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hmac.new(secret, content.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_config(config: dict, signature: str) -> bool:
    expected = _sign_config(config)
    return hmac.compare_digest(expected, signature)


def _is_secret_installed() -> bool:
    return os.path.exists(_secret_path())


def save_config(config: Dict[str, Any]) -> bool:
    """规范化配置并写入签名，同时保留上一份备份"""
    try:
        config_dir = os.path.dirname(CONFIG_FILE)
        if config_dir:
            os.makedirs(config_dir, exist_ok=True)

        # 写入新配置前复制当前文件作为备份
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as src:
                    with open(_backup_path(), "w", encoding="utf-8") as dst:
                        dst.write(src.read())
            except OSError:
                pass  # 备份失败不是致命错误

        # 先删除界面留下的废弃字段，再为实际写入内容计算签名
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
            # Windows 目标文件被占用时直接写入原文件，并在写入失败时用备份恢复
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
    # 高危：脚本块/间接调用绕过
    "[scriptblock]::create", "[scriptblock]::",
    "get-command", "get-alias",
    ".invoke()",
    "icm",  # Invoke-Command 别名
    "iex ",  # 带空格的 Invoke-Expression 别名
]

# launch_program 命中这些路径时加入 errors 并拒绝执行
_DANGEROUS_LAUNCH_PATHS = [
    "\\\\", "temp\\", "%tmp%\\", "%temp%\\",
    "powershell.exe", "cmd.exe", "wscript.exe", "cscript.exe",
    "powers~",   # 8.3 短名绕过
    "rundll32", "regsvr32", "wmic", "mshta", "certutil", "bitsadmin",
]

# 正则覆盖空格、变量拼接和调用运算符等子串黑名单漏掉的写法
_DANGEROUS_RE_PATTERNS = [
    (r"\bpowershell(\.exe)?\b", "嵌套 PowerShell"),
    (r"\bpwsh\b", "嵌套 PowerShell"),
    (r"\biex\b", "Invoke-Expression 别名"),
    (r"\bGet-Command\b", "动态获取命令"),
    (r"&\s*[\$\(]", "间接调用运算符 &"),
    (r"\$PSHOME", "路径变量拼接"),
    (r"\$env:\w+\s*[+)]", "环境变量拼接"),
    (r"\.\s*invoke\s*\(", "脚本块 Invoke"),
]

def _has_dangerous_command(command: str) -> str | None:
    """检查命令是否命中字符串或正则危险模式"""
    cmd_lower = command.lower()
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.lower() in cmd_lower:
            return pattern
    for regex, label in _DANGEROUS_RE_PATTERNS:
        if re.search(regex, command, re.IGNORECASE):
            return label
    return None


def _validate_rules_safety(rules: list) -> tuple[list[str], list[str]]:
    """检查规则动作参数并返回警告和错误列表"""
    warnings: list[str] = []
    errors: list[str] = []
    for i, rule in enumerate(rules):
        rule_name = rule.get("name", f"规则 #{i+1}")
        for j, action in enumerate(rule.get("actions", [])):
            action_type = action.get("type", "")

            if action_type in ("run_powershell",):
                cmd = action.get("params", {}).get("command", "")
                if isinstance(cmd, str) and cmd:
                    danger = _has_dangerous_command(cmd)
                    if danger:
                        errors.append(
                            f"规则 \"{rule_name}\" 的 PowerShell 命令包含危险模式: '{danger}'"
                        )

            if action_type == "launch_program":
                path = action.get("params", {}).get("path", "")
                if isinstance(path, str) and path:
                    path_lower = path.lower()
                    for dl in _DANGEROUS_LAUNCH_PATHS:
                        if dl in path_lower:
                            errors.append(
                                f"规则 \"{rule_name}\" 的启动路径包含危险位置: '{path}'"
                            )
                            break

    return warnings, errors


class ConfigValidationError(ValueError):
    """运行时配置未通过完整性或安全校验"""


def load_verified_config() -> Dict[str, Any]:
    """读取通过签名、结构和安全校验的运行时配置快照"""
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
    """把旧条件树转换成统一的 op 和 children 格式"""
    if not isinstance(condition, dict):
        return condition

    copied = dict(condition)
    children = copied.get("children", copied.get("events"))
    # 带 children 或 events 的节点是条件组，叶子的 type 字段必须保留
    if not isinstance(children, list):
        return copied

    op = copied.get("op", copied.get("type", "any"))
    copied["op"] = "all" if op in ("all", "and") else "any"
    copied["children"] = [_normalize_condition(child) for child in children]
    copied.pop("events", None)
    copied.pop("type", None)
    if copied["op"] == "any":
        # any 条件组不使用 within_seconds，旧界面只隐藏过这个字段
        copied.pop("within_seconds", None)
    return copied


def _unwrap_single_condition(condition: Any) -> Dict[str, Any] | None:
    """从单分支条件组中取出事件，消除旧版重复字段"""
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
    """移除旧步骤 ID，并把能确定的旧引用改为自动步骤名"""
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
        # display_control 的旧亮度动作在加载时转换为新参数，旧规则仍可执行
        if copied.get("type") == "display_control":
            params = copied.get("params")
            if isinstance(params, dict):
                legacy_action = params.get("action")
                if legacy_action in ("low_brightness", "high_brightness"):
                    copied_params = dict(params)
                    copied_params["action"] = "set_brightness"
                    copied_params["brightness"] = (
                        10 if legacy_action == "low_brightness" else 90
                    )
                    copied["params"] = copied_params
        normalized.append(_replace_step_references(copied, replacements))
    return normalized


def _legacy_template_ref(
    dotted_path: str,
    step_refs: Dict[str, str],
) -> Dict[str, Any] | None:
    """把旧模板路径解析为结构化 $ref，无法定位来源时返回 None"""
    parts = dotted_path.split(".")
    if len(parts) >= 3 and parts[:2] == ["event", "payload"]:
        return {"scope": "event", "path": parts[2:]}
    if len(parts) >= 4 and parts[0] == "steps" and parts[2] == "result":
        new_id = step_refs.get(parts[1])
        if new_id is None:
            return None
        return {"scope": "step", "node": new_id, "path": parts[3:]}
    return None


def _upgrade_legacy_templates(
    value: Any,
    step_refs: Dict[str, str],
) -> Any:
    """把纯模板字符串转换为结构化 $ref，并保留混合模板"""
    if isinstance(value, str):
        full = _LEGACY_TEMPLATE_RE.fullmatch(value)
        if full:
            reference = _legacy_template_ref(full.group(1), step_refs)
            if reference is not None:
                return {"$ref": reference}
        return value
    if isinstance(value, list):
        return [_upgrade_legacy_templates(item, step_refs) for item in value]
    if isinstance(value, dict):
        if is_reference(value):
            return value
        return {
            key: _upgrade_legacy_templates(item, step_refs)
            for key, item in value.items()
        }
    return value


def _upgrade_rule_templates(rule: Dict[str, Any]) -> Dict[str, Any]:
    """升级规则动作和确认参数中的旧模板引用"""
    step_refs: Dict[str, str] = {}
    actions = rule.get("actions")
    if isinstance(actions, list):
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            legacy_key = f"{action.get('type', 'action')}_{index + 1}"
            step_refs[legacy_key] = action.get("binding_id", legacy_key)

    copied = dict(rule)
    for field in ("preconditions", "actions"):
        items = copied.get(field)
        if not isinstance(items, list):
            continue
        normalized = []
        for item in items:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            item_copy = dict(item)
            if isinstance(item_copy.get("params"), dict):
                item_copy["params"] = _upgrade_legacy_templates(
                    item_copy["params"], step_refs
                )
            normalized.append(item_copy)
        copied[field] = normalized
    return copied


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
            normalized = ensure_rule_binding_ids(copied)
            normalized_rules.append(_upgrade_rule_templates(normalized))

        result = dict(config)
        result["schema_version"] = 2
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
    return _normalize_config(result) if rules else config


def _default_v2_config() -> Dict[str, Any]:
    normalized = _normalize_config(copy.deepcopy(DEFAULT_CONFIG))
    assert isinstance(normalized, dict)
    return normalized


def get_config() -> Dict[str, Any]:
    """加载配置并校验签名和安全规则"""
    config_dir = os.path.dirname(CONFIG_FILE)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)

    if not os.path.exists(CONFIG_FILE):
        default_config = _default_v2_config()
        save_config(default_config)
        print("[DEBUG] Default config created.")
        return default_config

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
        default_config = _default_v2_config()
        save_config(default_config)
        return default_config

    signature = config.pop(_SIGNATURE_KEY, "")
    has_secret = _is_secret_installed()

    if not has_secret:
        # 缺少密钥时无法验证配置，攻击者可伪造文件，因此暂停引擎并要求用户确认
        raise ConfigValidationError(
            "配置签名密钥缺失，无法验证配置完整性，文件可能被篡改。"
            f"引擎已暂停。请在 Dashboard「安全与权限」页核对配置摘要后重新签名；"
            f"确认无异常后可删除 {CONFIG_FILE} 让引擎重新生成默认配置。"
        )

    if not signature:
        # 没有签名时无法验证配置，暂停引擎等待用户确认
        raise ConfigValidationError(
            "配置文件缺少签名，可能被篡改。引擎已暂停。"
            "请在 Dashboard「安全与权限」页核对配置摘要后重新签名。"
        )

    if not _verify_config(config, signature):
        # 签名校验失败说明文件被修改，暂停引擎并保留用户配置
        raise ConfigValidationError(
            "配置文件签名校验失败，文件可能被篡改。引擎已暂停。"
            "请在 Dashboard「安全与权限」页核对配置摘要后重新签名；"
            "如需找回旧版本，可检查 config.json.bak。"
        )

    normalized = _normalize_config(config)
    migrated = normalized != config
    config = normalized

    # 校验通过后用规范化结果重新签名并原子写回
    save_config(config)

    if migrated:
        print("[DEBUG] Legacy config migrated to new rule format.")

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
    """校验备份签名后恢复配置"""
    if not os.path.exists(_backup_path()):
        return None
    try:
        with open(_backup_path(), "r", encoding="utf-8") as f:
            raw = f.read()
        config = json.loads(raw)
        signature = config.pop(_SIGNATURE_KEY, "")
        if not _is_secret_installed():
            # 备份也无法验证，缺少签名密钥时拒绝恢复
            print("[WARN] 签名密钥缺失，无法验证备份，拒绝恢复", file=sys.stderr)
            return None
        if signature and _verify_config(config, signature):
            print("[INFO] 从备份成功恢复配置", file=sys.stderr)
            normalized = _normalize_config(config)
            save_config(normalized)
            return normalized
        print("[WARN] 备份文件无有效签名，拒绝恢复", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[ERROR] 备份恢复失败: {e}", file=sys.stderr)
        return None
