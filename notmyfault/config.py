import copy
import hmac
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from typing import Any, Dict, List

from notmyfault.application_paths import ApplicationPaths
from notmyfault.core.bindings import is_reference, is_literal, is_typed_value
from notmyfault.core.data_types import DataTypeError
from notmyfault.core.value_codec import decode_value, encode_value


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
            if item_copy.get("type") == "if":
                for branch in ("then", "else"):
                    if branch in item_copy:
                        item_copy[branch] = normalize_items(
                            item_copy[branch], "a", include_failures=include_failures
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
    "settings": {}
}


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

_SIGNATURE_KEY = "_signature"


def _secure_write_secret(path: str, data: bytes) -> None:
    """写入配置密钥并把文件权限限制为当前用户"""
    fd = -1
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        if os.name == "nt":
            from notmyfault.security.api_key_store import _restrict_key_file

            _restrict_key_file(path)
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        raise


def _validate_secret_permissions(path: str) -> None:
    if os.name != "nt":
        file_stat = os.stat(path)
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise ConfigValidationError("配置签名密钥所有者无效")
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ConfigValidationError("配置签名密钥权限过宽")
        return

    import pywintypes
    import win32api
    import win32con
    import win32security

    try:
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        try:
            current_sid = win32security.GetTokenInformation(
                token, win32security.TokenUser
            )[0]
        finally:
            token.Close()
        allowed = {
            win32security.ConvertSidToStringSid(current_sid),
            "S-1-5-18",
            "S-1-5-32-544",
        }
        descriptor = win32security.GetNamedSecurityInfo(
            path,
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION,
        )
        owner = descriptor.GetSecurityDescriptorOwner()
        if owner is None or win32security.ConvertSidToStringSid(owner) not in allowed:
            raise ConfigValidationError("配置签名密钥所有者无效")
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            raise ConfigValidationError("配置签名密钥权限过宽")
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if ace[0][0] in (
                win32security.ACCESS_ALLOWED_ACE_TYPE,
                win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
            ):
                if win32security.ConvertSidToStringSid(ace[-1]) not in allowed:
                    raise ConfigValidationError("配置签名密钥权限过宽")
            elif ace[0][0] not in (
                win32security.ACCESS_DENIED_ACE_TYPE,
                win32security.ACCESS_DENIED_OBJECT_ACE_TYPE,
            ):
                raise ConfigValidationError("配置签名密钥权限无法验证")
    except (pywintypes.error, NotImplementedError) as error:
        raise ConfigValidationError("配置签名密钥权限无法验证") from error


def normalize_rules(rules: Any) -> List[Dict[str, Any]]:
    return _normalize_rules(rules)


class ConfigValidationError(ValueError):
    """运行时配置未通过完整性或安全校验"""


def _validate_rules_for_runtime(rules: List[Dict[str, Any]]) -> None:
    """拒绝不符合规则结构的运行时配置"""
    from notmyfault.core.rules import validate_rules_structure

    structure_errors = validate_rules_structure(rules)
    if structure_errors:
        raise ConfigValidationError(
            "规则结构校验失败: " + "; ".join(structure_errors[:3])
        )


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
    copied["op"] = {"and": "all", "or": "any"}.get(op, op) if isinstance(op, str) else op
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
        if current.get("op") == "not":
            return None
        children = current.get("children")
        if isinstance(children, list):
            if len(children) != 1:
                return None
            current = children[0]
            continue
        return current if isinstance(current.get("type"), str) else None
    return None


def _replace_step_references(value: Any, replacements: Dict[str, str]) -> Any:
    if is_literal(value) or is_typed_value(value):
        return copy.deepcopy(value)
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            parts = match.group(1).split(".")
            if len(parts) >= 3 and parts[0] == "steps" and parts[1] in replacements:
                parts[1] = replacements[parts[1]]
                return match.group(0).replace(match.group(1), ".".join(parts), 1)
            return match.group(0)

        return _LEGACY_TEMPLATE_RE.sub(replace, value)
    if isinstance(value, list):
        return [_replace_step_references(item, replacements) for item in value]
    if isinstance(value, dict):
        if is_reference(value):
            reference = dict(value["$ref"])
            if reference.get("scope") == "step":
                node = reference.get("node")
                if node in replacements:
                    reference["node"] = replacements[node]
            return {"$ref": reference}
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
    """把纯模板字符串转换为结构化 $ref，并更新混合模板中的步骤 ID"""
    if is_literal(value) or is_typed_value(value):
        return copy.deepcopy(value)
    if isinstance(value, str):
        full = _LEGACY_TEMPLATE_RE.fullmatch(value)
        if full:
            reference = _legacy_template_ref(full.group(1), step_refs)
            if reference is not None:
                return {"$ref": reference}
        # 混合模板需要更新旧步骤 ID 引用
        return _replace_step_references(value, step_refs)
    if isinstance(value, list):
        return [_upgrade_legacy_templates(item, step_refs) for item in value]
    if isinstance(value, dict):
        if is_reference(value):
            return _replace_step_references(value, step_refs)
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
    settings.pop("admin_authorization_mode", None)
    settings.pop("admin_rule_key_verification", None)
    if "ai_drafting" in settings:
        settings["ai_drafting"] = get_ai_drafting_settings(result)
    result["settings"] = settings
    return result


def _default_v2_config() -> Dict[str, Any]:
    normalized = _normalize_config(copy.deepcopy(DEFAULT_CONFIG))
    assert isinstance(normalized, dict)
    return normalized


class SignedConfigStore:
    def __init__(self, paths: ApplicationPaths) -> None:
        self.paths = paths
        self._secret_cache: bytes | None = None

    @property
    def config_path(self) -> str:
        return str(self.paths.config_file)

    @property
    def rules_path(self) -> str:
        return str(self.paths.rules_file)

    @property
    def plugin_manifest_path(self) -> str:
        return str(self.paths.plugin_manifest_file)

    @property
    def _secret_path(self) -> str:
        return str(self.paths.config_secret_file)

    def _get_or_create_secret(self) -> bytes:
        if self._secret_cache is not None:
            return self._secret_cache
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            secret = self.paths.config_secret_file.read_bytes()
        except FileNotFoundError:
            secret = secrets.token_bytes(32)
            try:
                _secure_write_secret(self._secret_path, secret)
                created = True
            except FileExistsError:
                secret = self.paths.config_secret_file.read_bytes()
        except OSError as error:
            raise ConfigValidationError("配置签名密钥无法读取") from error
        if len(secret) < 32:
            raise ConfigValidationError("配置签名密钥格式无效")
        if not created:
            _validate_secret_permissions(self._secret_path)
        self._secret_cache = secret
        return self._secret_cache

    def _sign(self, data: Dict[str, Any]) -> str:
        content = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        return hmac.new(
            self._get_or_create_secret(),
            content.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _verify(self, data: Dict[str, Any], signature: str) -> bool:
        return hmac.compare_digest(self._sign(data), signature)

    def _write_signed_json(
        self,
        path: str,
        backup_path: str,
        data: Dict[str, Any],
    ) -> bool:
        try:
            target_dir = os.path.dirname(path)
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)
            to_save = dict(data)
            to_save[_SIGNATURE_KEY] = self._sign(to_save)
            content = json.dumps(
                to_save,
                ensure_ascii=False,
                indent=4,
            ).encode("utf-8")

            def atomic_write(target: str, payload: bytes) -> None:
                descriptor, tmp_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(target)}.",
                    suffix=".tmp",
                    dir=target_dir or None,
                )
                try:
                    with os.fdopen(descriptor, "wb") as file:
                        file.write(payload)
                        file.flush()
                        os.fsync(file.fileno())
                    os.replace(tmp_path, target)
                finally:
                    try:
                        os.remove(tmp_path)
                    except FileNotFoundError:
                        pass

            if os.path.exists(path):
                with open(path, "rb") as source:
                    previous = source.read()
                atomic_write(backup_path, previous)
            atomic_write(path, content)
            return True
        except OSError as error:
            print(
                f"[Config] 写入 {os.path.basename(path)} 失败: {error}",
                file=sys.stderr,
            )
            return False

    def save_config(self, config: Dict[str, Any]) -> bool:
        normalized = _normalize_config(config)
        if not isinstance(normalized, dict):
            print("[Config] 保存配置失败: 配置根节点必须是对象", file=sys.stderr)
            return False
        return self._write_signed_json(
            self.config_path,
            self.config_path + ".bak",
            normalized,
        )

    def save_rules(self, rules: List[Dict[str, Any]]) -> bool:
        if not isinstance(rules, list):
            print("[Config] 保存规则失败: rules 必须是列表", file=sys.stderr)
            return False
        data = {"schema_version": 2, "value_encoding": "typed-v1", "rules": encode_value(_normalize_rules(rules))}
        return self._write_signed_json(
            self.rules_path,
            self.rules_path + ".bak",
            data,
        )

    def _read_signed(self, path: str, label: str) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as file:
                raw = json.load(file)
        except (json.JSONDecodeError, OSError) as error:
            raise ConfigValidationError(f"{label}文件无法解析: {error}") from error
        if not isinstance(raw, dict):
            raise ConfigValidationError(f"{label}文件根节点必须是对象")
        signature = raw.pop(_SIGNATURE_KEY, "")
        if not self.paths.config_secret_file.is_file():
            raise ConfigValidationError(f"{label}签名密钥缺失")
        if not signature:
            raise ConfigValidationError(f"{label}缺少签名")
        if not self._verify(raw, signature):
            raise ConfigValidationError(f"{label}签名校验失败")
        return self._decode_rules_data(raw)

    @staticmethod
    def _decode_rules_data(data):
        encoding = data.get("value_encoding")
        if encoding is None:
            return data
        if encoding != "typed-v1":
            raise ConfigValidationError("规则数据编码版本不受支持")
        try:
            return {**data, "rules": decode_value(data.get("rules"))}
        except DataTypeError as error:
            raise ConfigValidationError(str(error)) from error

    def load_verified_config(self) -> Dict[str, Any]:
        normalized = _normalize_config(self._read_signed(self.config_path, "配置"))
        if not isinstance(normalized, dict):
            raise ConfigValidationError("规范化后的配置必须是对象")
        return normalized

    def load_verified_rules(self, *, for_editing: bool = False) -> List[Dict[str, Any]]:
        raw = self._read_signed(self.rules_path, "规则")
        rules = raw.get("rules")
        if not isinstance(rules, list):
            raise ConfigValidationError("rules 必须是列表")
        def validate(items):
            if for_editing:
                items = [
                    {key: value for key, value in rule.items() if key != "preconditions"}
                    if isinstance(rule, dict) else rule
                    for rule in items
                ]
            _validate_rules_for_runtime(items)

        validate(rules)
        normalized = _normalize_rules(rules)
        validate(normalized)
        return normalized

    def _keep_premigration_backup(self) -> None:
        backup = self.config_path + ".premigration.bak"
        if os.path.exists(backup):
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as src:
                content = src.read()
            with open(backup, "w", encoding="utf-8") as dst:
                dst.write(content)
        except OSError:
            pass

    def _merge_legacy_rules(self, legacy_rules: List[Dict[str, Any]]) -> bool:
        existing = self.load_verified_rules()
        existing_ids = {rule.get("rule_id") for rule in existing}
        additions = [
            rule for rule in legacy_rules if rule.get("rule_id") not in existing_ids
        ]
        return self.save_rules(existing + additions)

    def _migrate_rules_file(self) -> None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as file:
                raw = json.load(file)
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict) or (
            "rules" not in raw and "processes" not in raw
        ):
            return

        self._keep_premigration_backup()
        legacy = dict(raw)
        legacy.pop(_SIGNATURE_KEY, None)
        legacy_rules = _extract_legacy_rules(legacy)
        if os.path.exists(self.rules_path):
            if legacy_rules and not self._merge_legacy_rules(legacy_rules):
                raise ConfigValidationError("规则文件写入失败，无法完成规则合并")
        elif not self.save_rules(legacy_rules):
            raise ConfigValidationError("规则文件写入失败，无法完成规则拆分")

        stripped = {
            key: value
            for key, value in raw.items()
            if key not in ("rules", "processes", _SIGNATURE_KEY)
        }
        if not self.save_config(stripped):
            raise ConfigValidationError("配置文件写入失败，无法完成规则拆分")
        if legacy_rules:
            print(
                f"[Config] 已把 {len(legacy_rules)} 条规则从 config.json 拆分到 rules.json"
            )

    def _recover_config(self) -> Dict[str, Any] | None:
        backup = self.config_path + ".bak"
        try:
            with open(backup, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        signature = data.pop(_SIGNATURE_KEY, "")
        if not self.paths.config_secret_file.is_file():
            return None
        if not signature or not self._verify(data, signature):
            return None
        legacy_rules = _extract_legacy_rules(data)
        if legacy_rules:
            merged = (
                self._merge_legacy_rules(legacy_rules)
                if os.path.exists(self.rules_path)
                else self.save_rules(legacy_rules)
            )
            if not merged:
                return None
        normalized = _normalize_config(data)
        if not isinstance(normalized, dict) or not self.save_config(normalized):
            return None
        return normalized

    def _recover_rules(self) -> List[Dict[str, Any]] | None:
        backup = self.rules_path + ".bak"
        try:
            with open(backup, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        signature = data.pop(_SIGNATURE_KEY, "")
        if not self.paths.config_secret_file.is_file():
            return None
        if not signature or not self._verify(data, signature):
            return None
        rules = _normalize_rules(self._decode_rules_data(data).get("rules", []))
        _validate_rules_for_runtime(rules)
        return rules if self.save_rules(rules) else None

    def load_config(self) -> Dict[str, Any]:
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        if not os.path.exists(self.config_path):
            default = _default_v2_config()
            self.save_config(default)
            return default
        try:
            config = self.load_verified_config()
        except ConfigValidationError as error:
            if "无法解析" not in str(error):
                raise
            recovered = self._recover_config()
            if recovered is not None:
                return recovered
            default = _default_v2_config()
            self.save_config(default)
            return default
        self._migrate_rules_file()
        return config

    def load_rules(self) -> List[Dict[str, Any]]:
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_rules_file()
        if not os.path.exists(self.rules_path):
            self.save_rules([])
            return []
        try:
            rules = self.load_verified_rules()
        except ConfigValidationError as error:
            if not any(
                marker in str(error) for marker in ("无法解析", "根节点")
            ):
                raise
            recovered = self._recover_rules()
            if recovered is not None:
                return recovered
            self.save_rules([])
            return []
        return rules

    def inspect_security(
        self,
        actions_meta: Dict[str, Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        status: Dict[str, Any] = {"status": "ok", "reason": "", "summary": None}
        has_secret = self.paths.config_secret_file.is_file()
        if not has_secret:
            status["status"] = "tampered"
            status["reason"] = (
                "配置签名密钥缺失，无法验证配置是否被篡改。"
                "引擎已暂停，请核对下方配置摘要后选择处理方式。"
            )
        try:
            with open(self.config_path, "r", encoding="utf-8") as file:
                config = json.load(file)
        except (json.JSONDecodeError, OSError):
            return {"status": "unreadable", "reason": "配置文件无法读取", "summary": None}
        if not isinstance(config, dict):
            return {"status": "unreadable", "reason": "配置根节点不是对象", "summary": None}
        signature = config.pop(_SIGNATURE_KEY, "")
        if has_secret and (not signature or not self._verify(config, signature)):
            status["status"] = "tampered"
            status["reason"] = (
                "配置签名校验失败，文件可能被篡改。"
                "引擎已暂停，请核对下方配置摘要后选择处理方式。"
            )

        rules: List[Any] = []
        if os.path.exists(self.rules_path):
            try:
                with open(self.rules_path, "r", encoding="utf-8") as file:
                    rules_data = json.load(file)
            except (json.JSONDecodeError, OSError):
                return {"status": "unreadable", "reason": "规则文件无法读取", "summary": None}
            if not isinstance(rules_data, dict):
                return {"status": "unreadable", "reason": "规则文件根节点不是对象", "summary": None}
            rules_signature = rules_data.pop(_SIGNATURE_KEY, "")
            if has_secret and (
                not rules_signature or not self._verify(rules_data, rules_signature)
            ):
                status["status"] = "tampered"
                status["reason"] = (
                    "规则签名校验失败，文件可能被篡改。"
                    "引擎已暂停，请核对下方规则摘要后选择处理方式。"
                )
            loaded = rules_data.get("rules", [])
            if isinstance(loaded, list):
                rules = loaded

        from notmyfault.security.plugin_schema import requires_admin_rule_approval

        action_schema = actions_meta or {}

        def summarize_params(params: Any) -> Dict[str, Any]:
            if not isinstance(params, dict):
                return {}
            return {
                str(key): "***" if value not in (None, "") else ""
                for key, value in params.items()
            }

        def summarize_item(item: Any) -> Dict[str, Any]:
            if not isinstance(item, dict):
                return {"type": "?", "high_risk": False, "params": {}}
            action_type = item.get("type", "?")
            summary = {
                "type": action_type,
                "high_risk": requires_admin_rule_approval(
                    action_schema.get(action_type, {})
                ),
                "params": summarize_params(item.get("params")),
            }
            child_fields = ("then", "else", "failure_actions") if action_type == "if" else ("failure_actions",)
            for field in child_fields:
                children = item.get(field, [])
                if isinstance(children, list) and (children or field in ("then", "else")):
                    summary[field] = [summarize_item(child) for child in children]
                    summary["high_risk"] |= any(child["high_risk"] for child in summary[field])
            return summary

        status["summary"] = {
            "rule_count": len(rules),
            "rules": [
                {
                    "name": (
                        rule.get("name", f"规则 #{index + 1}")
                        if isinstance(rule, dict)
                        else f"规则 #{index + 1}"
                    ),
                    "preconditions": (
                        [
                            summarize_item(item)
                            for item in rule.get("preconditions", [])
                        ]
                        if isinstance(rule, dict)
                        and isinstance(rule.get("preconditions", []), list)
                        else []
                    ),
                    "actions": (
                        [summarize_item(action) for action in rule.get("actions", [])]
                        if isinstance(rule, dict)
                        and isinstance(rule.get("actions", []), list)
                        else []
                    ),
                }
                for index, rule in enumerate(rules)
            ],
        }
        return status

    def approve_current_files(
        self,
        schema: Dict[str, Dict[str, Dict[str, Any]]],
        admin_key_password: str | None,
    ) -> None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as file:
                config = json.load(file)
        except (json.JSONDecodeError, OSError) as error:
            raise ConfigValidationError(f"配置文件无法解析: {error}") from error
        if not isinstance(config, dict):
            raise ConfigValidationError("配置根节点不是对象")
        config.pop(_SIGNATURE_KEY, None)
        normalized_config = _normalize_config(config)
        if not isinstance(normalized_config, dict):
            raise ConfigValidationError("配置根节点不是对象")

        normalized_rules: List[Dict[str, Any]] | None = None
        if os.path.exists(self.rules_path):
            try:
                with open(self.rules_path, "r", encoding="utf-8") as file:
                    rules_data = json.load(file)
            except (json.JSONDecodeError, OSError) as error:
                raise ConfigValidationError(f"规则文件无法解析: {error}") from error
            if not isinstance(rules_data, dict):
                raise ConfigValidationError("规则文件根节点不是对象")
            rules_data.pop(_SIGNATURE_KEY, None)
            rules_data = self._decode_rules_data(rules_data)
            rules = rules_data.get("rules", [])
            if not isinstance(rules, list):
                rules = []
            normalized_rules = _normalize_rules(rules)
            _validate_rules_for_runtime(normalized_rules)
            from notmyfault.core.rules import (
                validate_rule_bindings,
                validate_rules_structure,
            )

            structure_errors = validate_rules_structure(normalized_rules)
            if structure_errors:
                raise ConfigValidationError(
                    "规则包含结构无效的规则，拒绝重新签名: "
                    + "; ".join(structure_errors[:3])
                )
            binding_errors = []
            for index, rule in enumerate(normalized_rules):
                for issue in validate_rule_bindings(
                    rule,
                    schema.get("triggers", {}),
                    schema.get("actions", {}),
                ):
                    binding_errors.append(
                        f"规则 #{index + 1} {issue.get('message', '数据绑定无效')}"
                    )
            if binding_errors:
                raise ConfigValidationError(
                    "规则数据绑定无效，拒绝重新签名: "
                    + "; ".join(binding_errors[:3])
                )

            from notmyfault.security.rule_approval import (
                require_admin_rule_approval,
            )

            require_admin_rule_approval(
                [],
                normalized_rules,
                schema,
                admin_key_password,
            )

        if not self.save_config(normalized_config):
            raise ConfigValidationError("重新签名失败")
        if normalized_rules is not None and not self.save_rules(normalized_rules):
            raise ConfigValidationError("规则重新签名失败")
