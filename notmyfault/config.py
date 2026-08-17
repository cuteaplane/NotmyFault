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
_RULE_ID_RE = re.compile(r"^r_[a-z0-9_]{6,64}$")
_LEGACY_TEMPLATE_RE = re.compile(r"{{\s*([a-zA-Z_][\w.]*)\s*}}")


def _new_binding_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def ensure_rule_id(
    rule: Dict[str, Any],
    seen: set[str] | None = None,
) -> Dict[str, Any]:
    """给规则补充跨保存和重排保持不变的身份"""
    copied = dict(rule)
    used = seen if seen is not None else set()
    rule_id = copied.get("rule_id")
    if (
        not isinstance(rule_id, str)
        or not _RULE_ID_RE.fullmatch(rule_id)
        or rule_id in used
    ):
        rule_id = f"r_{secrets.token_hex(6)}"
        while rule_id in used:
            rule_id = f"r_{secrets.token_hex(6)}"
    copied["rule_id"] = rule_id
    used.add(rule_id)
    return copied


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

    def normalize_items(items: Any, prefix: str, *, include_failures: bool) -> Any:
        if not isinstance(items, list):
            return items
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
            if include_failures and "failure_actions" in item_copy:
                item_copy["failure_actions"] = normalize_items(
                    item_copy["failure_actions"], "a", include_failures=False
                )
            normalized.append(item_copy)
        return normalized

    for field, prefix in (("preconditions", "p"), ("actions", "a")):
        items = copied.get(field)
        if not isinstance(items, list):
            continue
        copied[field] = normalize_items(
            items, prefix, include_failures=field == "actions"
        )
    return copied

# 规则单独存放在 rules.json，这里只保留轻量设置。
DEFAULT_CONFIG: Dict[str, Any] = {
    "disabled_plugins": {
        "triggers": [],
        "actions": []
    },
    "settings": {
        "admin_authorization_mode": "per_execution",
        "admin_rule_key_verification": True
    }
}

ADMIN_AUTHORIZATION_MODES = {"per_execution", "engine_start"}


def get_admin_authorization_mode(config: Dict[str, Any]) -> str:
    """读取管理员授权方式，无效或缺失时使用逐次确认。"""
    settings = config.get("settings") if isinstance(config, dict) else None
    mode = settings.get("admin_authorization_mode") if isinstance(settings, dict) else None
    return mode if mode in ADMIN_AUTHORIZATION_MODES else "per_execution"


def get_admin_rule_key_verification(config: Dict[str, Any]) -> bool:
    """读取创建管理员规则时是否验证签名私钥，缺失时默认验证。"""
    settings = config.get("settings") if isinstance(config, dict) else None
    value = settings.get("admin_rule_key_verification") if isinstance(settings, dict) else None
    return value if isinstance(value, bool) else True


def get_ai_drafting_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """返回不含秘密字段的 AI 规则草稿设置。"""
    settings = config.get("settings") if isinstance(config, dict) else None
    raw = settings.get("ai_drafting") if isinstance(settings, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": raw.get("enabled") if isinstance(raw.get("enabled"), bool) else False,
        "endpoint_url": (
            raw.get("endpoint_url")
            if isinstance(raw.get("endpoint_url"), str)
            else ""
        ),
        "model": raw.get("model") if isinstance(raw.get("model"), str) else "",
        "api_format": (
            raw.get("api_format")
            if raw.get("api_format") in {"chat_completions", "responses"}
            else "chat_completions"
        ),
    }

CONFIG_FILE = os.path.join(get_config_dir(), "config.json")
RULES_FILE = os.path.join(get_config_dir(), "rules.json")
def _backup_path() -> str:
    return CONFIG_FILE + ".bak"


def _rules_backup_path() -> str:
    return RULES_FILE + ".bak"


def _premigration_backup_path() -> str:
    return CONFIG_FILE + ".premigration.bak"


def _keep_premigration_backup() -> None:
    """拆分前留一份旧配置全量快照，.bak 只有一代，后面的保存会把它冲掉"""
    backup = _premigration_backup_path()
    if os.path.exists(backup):
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as src:
            content = src.read()
        with open(backup, "w", encoding="utf-8") as dst:
            dst.write(content)
    except OSError:
        pass  # 备份失败不是致命错误


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


def _write_signed_json(path: str, backup_path: str, data: Dict[str, Any]) -> bool:
    """备份、签名、tmp 原子写入，写入失败时用备份回滚"""
    try:
        target_dir = os.path.dirname(path)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)

        # 写入新内容前复制当前文件作为备份
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as src:
                    with open(backup_path, "w", encoding="utf-8") as dst:
                        dst.write(src.read())
            except OSError:
                pass  # 备份失败不是致命错误

        # 先为实际写入内容计算签名
        to_save = dict(data)
        to_save[_SIGNATURE_KEY] = _sign_config(to_save)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, path)
        except OSError:
            # Windows 目标文件被占用时直接写入原文件，并在写入失败时用备份恢复
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            backup = None
            try:
                with open(path, "r", encoding="utf-8") as bf:
                    backup = bf.read()
            except OSError:
                pass
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(to_save, f, ensure_ascii=False, indent=4)
            except OSError:
                if backup is not None:
                    try:
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(backup)
                    except OSError:
                        pass
                raise
        return True
    except OSError as e:
        print(f"[Config] 写入 {os.path.basename(path)} 失败: {e}", file=sys.stderr)
        return False


def save_config(config: Dict[str, Any]) -> bool:
    """规范化设置并写入签名，规则单独存放在 rules.json"""
    to_save = _normalize_config(config)
    if not isinstance(to_save, dict):
        print("[Config] 保存配置失败: 配置根节点必须是对象", file=sys.stderr)
        return False
    return _write_signed_json(CONFIG_FILE, _backup_path(), to_save)


def save_rules(rules: list) -> bool:
    """规范化规则列表并签名写入 rules.json"""
    if not isinstance(rules, list):
        print("[Config] 保存规则失败: rules 必须是列表", file=sys.stderr)
        return False
    data = {"schema_version": 2, "rules": _normalize_rules(rules)}
    return _write_signed_json(RULES_FILE, _rules_backup_path(), data)


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
    # 高危：提权
    "runas", "processstartinfo", "system.diagnostics.process",
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
        actions = []
        for action in rule.get("actions", []):
            actions.append(action)
            if isinstance(action, dict) and isinstance(action.get("failure_actions"), list):
                actions.extend(action["failure_actions"])
        for action in actions:
            if not isinstance(action, dict):
                continue
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


def _validate_rules_for_runtime(rules: List[Dict[str, Any]]) -> None:
    """打印安全提醒，并拒绝会在运行时执行的危险规则。"""
    safety_warnings, safety_errors = _validate_rules_safety(rules)
    for warning in safety_warnings:
        print(f"[Config] [安全] {warning}", file=sys.stderr)
    if safety_errors:
        for error in safety_errors:
            print(f"[Config] [安全-严重] {error}", file=sys.stderr)
        raise ConfigValidationError(
            "规则安全校验失败: " + "; ".join(safety_errors[:3])
        )


def load_verified_config() -> Dict[str, Any]:
    """读取通过签名和结构校验的设置快照，规则校验见 load_verified_rules"""
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
    return normalized


def load_verified_rules() -> List[Dict[str, Any]]:
    """读取通过签名、结构和安全校验的规则快照，供热重载使用"""
    try:
        with open(RULES_FILE, "r", encoding="utf-8") as rules_file:
            raw = json.load(rules_file)
    except (json.JSONDecodeError, OSError) as e:
        raise ConfigValidationError(f"规则文件无法解析: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigValidationError("规则文件根节点必须是对象")

    signature = raw.pop(_SIGNATURE_KEY, "")
    if not _is_secret_installed():
        raise ConfigValidationError("规则签名密钥缺失")
    if not signature:
        raise ConfigValidationError("规则缺少签名")
    if not _verify_config(raw, signature):
        raise ConfigValidationError("规则签名校验失败")

    rules = raw.get("rules")
    if not isinstance(rules, list):
        raise ConfigValidationError("rules 必须是列表")

    normalized = _normalize_rules(rules)
    _validate_rules_for_runtime(normalized)
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


def _normalize_rules(rules: Any) -> List[Dict[str, Any]]:
    """规范化规则列表并补齐规则和节点身份"""
    if not isinstance(rules, list):
        return []
    normalized_rules = []
    seen_rule_ids: set[str] = set()
    for rule in rules:
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
        normalized = ensure_rule_id(copied, seen_rule_ids)
        normalized = ensure_rule_binding_ids(normalized)
        normalized_rules.append(_upgrade_rule_templates(normalized))
    return normalized_rules


def _extract_legacy_rules(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从旧 config 提取规则，兼容更早的进程列表格式"""
    if isinstance(config.get("rules"), list):
        return _normalize_rules(config["rules"])

    processes = config.get("processes")
    if not isinstance(processes, list):
        return []

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

    return _normalize_rules(rules)


def _normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return config

    # 规则已拆到 rules.json，丢弃旧文件里残留的规则和进程字段
    result = dict(config)
    result.pop("rules", None)
    result.pop("processes", None)
    result["schema_version"] = 2
    settings = result.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    else:
        settings = dict(settings)
    settings["admin_authorization_mode"] = get_admin_authorization_mode(result)
    settings["admin_rule_key_verification"] = get_admin_rule_key_verification(result)
    if "ai_drafting" in settings:
        settings["ai_drafting"] = get_ai_drafting_settings(result)
    result["settings"] = settings
    return result


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

    # 验签通过后才做拆分，避免把被篡改的规则搬进 rules.json
    _migrate_rules_file()

    normalized = _normalize_config(config)
    migrated = normalized != config
    config = normalized

    # 校验通过后用规范化结果重新签名并原子写回
    save_config(config)

    if migrated:
        print("[DEBUG] Legacy config migrated to new rule format.")

    print("[DEBUG] Config loaded:", config)
    return config


def _read_rules_content() -> List[Dict[str, Any]]:
    """读取通过签名和安全校验的现有规则。"""
    return load_verified_rules()


def _merge_rules_into_file(legacy_rules: List[Dict[str, Any]]) -> bool:
    """把旧配置的规则并进 rules.json，rule_id 已在文件里的跳过，迁移重跑不追加重复条目"""
    existing = _read_rules_content()
    existing_ids = {rule.get("rule_id") for rule in existing}
    new_rules = [
        rule for rule in legacy_rules if rule.get("rule_id") not in existing_ids
    ]
    return save_rules(existing + new_rules)


def _migrate_rules_file() -> None:
    """把旧 config.json 的规则拆进 rules.json，rules.json 已存在时追加旧规则再瘦身"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return  # config.json 不可读时由 get_config 的容错链路处理
    if not isinstance(raw, dict) or ("rules" not in raw and "processes" not in raw):
        return

    _keep_premigration_backup()
    legacy = dict(raw)
    legacy.pop(_SIGNATURE_KEY, None)
    legacy_rules = _extract_legacy_rules(legacy)
    if os.path.exists(RULES_FILE):
        # rules.json 已存在也要合并，跳过直接瘦身会把旧规则静默丢掉
        if legacy_rules and not _merge_rules_into_file(legacy_rules):
            raise ConfigValidationError("规则文件写入失败，无法完成规则合并")
    else:
        # 先写 rules.json 再瘦身 config.json，任一步失败都中止，避免规则丢失
        if not save_rules(legacy_rules):
            raise ConfigValidationError("规则文件写入失败，无法完成规则拆分")
    stripped = {
        k: v for k, v in raw.items()
        if k not in ("rules", "processes", _SIGNATURE_KEY)
    }
    if not save_config(stripped):
        raise ConfigValidationError("配置文件写入失败，无法完成规则拆分")
    if legacy_rules:
        print(
            f"[Config] 已把 {len(legacy_rules)} 条规则从 config.json 拆分到 rules.json"
        )


def get_rules() -> List[Dict[str, Any]]:
    """加载规则，首次运行时从旧 config.json 迁移"""
    rules_dir = os.path.dirname(RULES_FILE)
    if rules_dir and not os.path.exists(rules_dir):
        os.makedirs(rules_dir, exist_ok=True)

    _migrate_rules_file()

    if not os.path.exists(RULES_FILE):
        # 全新安装没有可迁移的规则，写入空规则文件
        save_rules([])
        print("[DEBUG] Empty rules.json created.")
        return []

    try:
        with open(RULES_FILE, "r", encoding="utf-8") as rules_file:
            data = json.loads(rules_file.read())
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ERROR] 规则文件损坏 ({e})，尝试从备份恢复...", file=sys.stderr)
        recovered = _try_recover_rules_from_backup()
        if recovered is not None:
            return recovered
        print("[ERROR] 备份也无效，重置为空规则", file=sys.stderr)
        save_rules([])
        return []

    if not isinstance(data, dict):
        print("[ERROR] 规则文件结构异常，尝试从备份恢复...", file=sys.stderr)
        recovered = _try_recover_rules_from_backup()
        if recovered is not None:
            return recovered
        save_rules([])
        return []

    signature = data.pop(_SIGNATURE_KEY, "")
    if not _is_secret_installed():
        # 缺少密钥时无法验证规则，暂停引擎并要求用户确认
        raise ConfigValidationError(
            "规则签名密钥缺失，无法验证规则完整性，文件可能被篡改。"
            f"引擎已暂停。请在 Dashboard「安全与权限」页核对规则摘要后重新签名；"
            f"确认无异常后可删除 {RULES_FILE} 从空规则重新开始。"
        )

    if not signature:
        raise ConfigValidationError(
            "规则文件缺少签名，可能被篡改。引擎已暂停。"
            "请在 Dashboard「安全与权限」页核对规则摘要后重新签名。"
        )

    if not _verify_config(data, signature):
        raise ConfigValidationError(
            "规则文件签名校验失败，文件可能被篡改。引擎已暂停。"
            "请在 Dashboard「安全与权限」页核对规则摘要后重新签名；"
            "如需找回旧版本，可检查 rules.json.bak。"
        )

    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigValidationError("rules 必须是列表")
    rules = _normalize_rules(raw_rules)
    _validate_rules_for_runtime(rules)

    # 校验通过后用规范化结果重新签名并原子写回
    save_rules(rules)

    print(f"[DEBUG] Rules loaded: {len(rules)} 条")
    return rules


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
            # 先确认旧规则写进 rules.json 再瘦身 config.json，写失败时备份原文件不动
            legacy_rules = _extract_legacy_rules(config)
            if legacy_rules:
                merged = (
                    _merge_rules_into_file(legacy_rules)
                    if os.path.exists(RULES_FILE)
                    else save_rules(legacy_rules)
                )
                if not merged:
                    print("[ERROR] 规则文件写入失败，放弃备份恢复", file=sys.stderr)
                    return None
            print("[INFO] 从备份成功恢复配置", file=sys.stderr)
            normalized = _normalize_config(config)
            save_config(normalized)
            return normalized
        print("[WARN] 备份文件无有效签名，拒绝恢复", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[ERROR] 备份恢复失败: {e}", file=sys.stderr)
        return None


def _try_recover_rules_from_backup() -> List[Dict[str, Any]] | None:
    """校验 rules.json.bak 签名后恢复规则"""
    backup = _rules_backup_path()
    if not os.path.exists(backup):
        return None
    try:
        with open(backup, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        if not isinstance(data, dict):
            print("[WARN] 规则备份结构异常，拒绝恢复", file=sys.stderr)
            return None
        signature = data.pop(_SIGNATURE_KEY, "")
        if not _is_secret_installed():
            print("[WARN] 签名密钥缺失，无法验证规则备份，拒绝恢复", file=sys.stderr)
            return None
        if signature and _verify_config(data, signature):
            print("[INFO] 从备份成功恢复规则", file=sys.stderr)
            rules = _normalize_rules(data.get("rules", []))
            _validate_rules_for_runtime(rules)
            save_rules(rules)
            return rules
        print("[WARN] 规则备份无有效签名，拒绝恢复", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[ERROR] 规则备份恢复失败: {e}", file=sys.stderr)
        return None
