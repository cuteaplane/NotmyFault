import json
import os
import re
from typing import Any, Dict, List, Tuple

_REQUIRED_META_FIELDS = {"id", "name", "description", "enabled", "version_code", "version", "package_name"}
_TRIGGER_OPTIONAL_FIELDS = {"semantic", "params", "permissions", "origin"}
_ACTION_OPTIONAL_FIELDS = {"params", "permissions", "origin"}
_ALLOWED_SEMANTICS = {"state", "oneshot"}
_ALLOWED_PARAM_TYPES = {"string", "number", "select", "bool"}
_REQUIRED_PARAM_FIELDS = {"name", "type", "label"}
_ALLOWED_PERMISSIONS = {"admin", "external_binary", "native_api"}
_ALLOWED_ORIGINS = {"builtin", "user", "third_party"}
_PACKAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
# id 仅允许字母/数字/下划线/连字符，禁止路径分隔符（防 ../ 路径穿越）
_PLUGIN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def validate_plugin_meta(
    meta: Dict[str, Any], plugin_type: str
) -> Tuple[bool, List[str]]:
    if plugin_type not in ("trigger", "action", "triggers", "actions"):
        return False, [f"未知插件类型: '{plugin_type}'（应为 trigger 或 action）"]

    errors: List[str] = []

    if not isinstance(meta, dict):
        return False, ["插件元数据不是有效的 JSON 对象"]

    for field in sorted(_REQUIRED_META_FIELDS):
        if field not in meta:
            errors.append(f"缺少必填字段: {field}")

    if "id" in meta and not isinstance(meta["id"], str):
        errors.append(f"字段 'id' 必须是字符串，实际: {type(meta['id']).__name__}")
    elif "id" in meta and not _PLUGIN_ID_RE.match(meta["id"]):
        errors.append(
            f"字段 'id' 含非法字符: '{meta['id']}'"
            f"（仅允许字母/数字/下划线/连字符，禁止路径分隔符）"
        )
    if "name" in meta and not isinstance(meta["name"], str):
        errors.append(f"字段 'name' 必须是字符串")
    if "description" in meta and not isinstance(meta["description"], str):
        errors.append(f"字段 'description' 必须是字符串")
    if "enabled" in meta and not isinstance(meta["enabled"], bool):
        errors.append(f"字段 'enabled' 必须为布尔值 (true/false)，实际: {type(meta['enabled']).__name__}")
    if "version_code" in meta:
        if not isinstance(meta["version_code"], int) or isinstance(meta["version_code"], bool):
            errors.append(f"字段 'version_code' 必须为整数，实际: {type(meta['version_code']).__name__}")
        elif meta["version_code"] < 1:
            errors.append(f"字段 'version_code' 必须 >= 1，实际: {meta['version_code']}")
    if "version" in meta and not isinstance(meta["version"], str):
        errors.append(f"字段 'version' 必须是字符串，实际: {type(meta['version']).__name__}")
    if "package_name" in meta:
        if not isinstance(meta["package_name"], str):
            errors.append(f"字段 'package_name' 必须是字符串，实际: {type(meta['package_name']).__name__}")
        elif not _PACKAGE_NAME_RE.match(meta["package_name"]):
            errors.append(f"字段 'package_name' 格式无效: '{meta['package_name']}'（应类似 com.example.plugin）")

    if "semantic" in meta:
        if meta["semantic"] not in _ALLOWED_SEMANTICS:
            errors.append(
                f"字段 'semantic' 无效: '{meta['semantic']}'"
                f"（允许: {', '.join(sorted(_ALLOWED_SEMANTICS))}）"
            )

    if "permissions" in meta:
        perms = meta["permissions"]
        if not isinstance(perms, list):
            errors.append(f"字段 'permissions' 必须是数组")
        else:
            for perm in perms:
                if not isinstance(perm, str):
                    errors.append(f"permissions 中的值必须是字符串，实际: {type(perm).__name__}")
                elif perm not in _ALLOWED_PERMISSIONS:
                    errors.append(
                        f"未知权限类型: '{perm}'（目前仅支持: {', '.join(sorted(_ALLOWED_PERMISSIONS))}）"
                    )

    if "origin" in meta:
        if meta["origin"] not in _ALLOWED_ORIGINS:
            errors.append("origin invalid: " + meta["origin"])

    if "params" in meta:
        params = meta["params"]
        if not isinstance(params, list):
            errors.append(f"字段 'params' 必须是数组")
        else:
            for i, param in enumerate(params):
                if not isinstance(param, dict):
                    errors.append(f"params[{i}] 必须是对象")
                    continue
                for field in sorted(_REQUIRED_PARAM_FIELDS):
                    if field not in param:
                        errors.append(f"params[{i}] 缺少必填字段: {field}")
                ptype = param.get("type", "")
                if ptype and ptype not in _ALLOWED_PARAM_TYPES:
                    errors.append(
                        f"params[{i}].type 无效: '{ptype}'"
                        f"（允许: {', '.join(sorted(_ALLOWED_PARAM_TYPES))}）"
                    )
                if ptype == "select" and "options" not in param:
                    errors.append(f"params[{i}] (type=select) 必须提供 'options' 字段")

    is_trigger = plugin_type in ("trigger", "triggers")
    allowed_fields = (
        _REQUIRED_META_FIELDS | _TRIGGER_OPTIONAL_FIELDS
        if is_trigger
        else _REQUIRED_META_FIELDS | _ACTION_OPTIONAL_FIELDS
    )
    for key in meta:
        if key not in allowed_fields:
            errors.append(f"包含未知字段: '{key}'")

    return len(errors) == 0, errors


def scan_plugins(base_dir: str, plugins_dir: str, json_filename: str) -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    root = os.path.join(base_dir, plugins_dir)
    if not os.path.isdir(root):
        return result

    for folder_name in sorted(os.listdir(root)):
        folder_path = os.path.join(root, folder_name)
        if not os.path.isdir(folder_path):
            continue

        json_file = os.path.join(folder_path, json_filename)
        if not os.path.exists(json_file):
            continue

        try:
            with open(json_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue

        plugin_id = meta.get("id")
        if not plugin_id:
            continue

        if meta.get("enabled") is False:
            continue

        plugin_type = "trigger" if plugins_dir == "triggers" else "action"
        is_valid, _ = validate_plugin_meta(meta, plugin_type)
        if not is_valid:
            continue

        result[plugin_id] = meta

    return result
