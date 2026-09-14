import copy
import hmac
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
from typing import Any, Dict, List

from notmyfault.application_paths import ApplicationPaths
from notmyfault.core.rule_model import (
    ensure_rule_id, ensure_rule_binding_ids, normalize_rules, normalize_rule_shape,
    _extract_legacy_rules,
)
from notmyfault.core.data_types import DataTypeError
from notmyfault.core.value_codec import decode_value, encode_value


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


class ConfigValidationError(ValueError):
    """运行时配置未通过完整性或安全校验"""


def _validate_rules_for_runtime(rules: List[Dict[str, Any]]) -> None:
    """拒绝不符合规则结构的运行时配置"""
    from notmyfault.core.rules import validate_rules_structure

    try:
        shaped = [normalize_rule_shape(rule) if isinstance(rule, dict) else rule for rule in rules]
        structure_errors = validate_rules_structure(shaped)
    except ValueError as error:
        structure_errors = [str(error)]
    if structure_errors:
        raise ConfigValidationError(
            "规则结构校验失败: " + "; ".join(structure_errors[:3])
        )


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
        try:
            normalized = normalize_rules(rules)
        except ValueError as error:
            print(f"[Config] 保存规则失败: {error}", file=sys.stderr)
            return False
        data = {"schema_version": 2, "value_encoding": "typed-v1", "rules": encode_value(normalized)}
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
        normalized = normalize_rules(rules)
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
        rules = normalize_rules(self._decode_rules_data(data).get("rules", []))
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

    def inspect_files(self) -> Dict[str, Any]:
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

        status["rules"] = rules
        return status

    def load_unsigned_files(self) -> tuple[dict, list[dict] | None]:
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
                raise ConfigValidationError("rules 必须是列表")
            _validate_rules_for_runtime(rules)
            normalized_rules = normalize_rules(rules)
            _validate_rules_for_runtime(normalized_rules)
        return normalized_config, normalized_rules
