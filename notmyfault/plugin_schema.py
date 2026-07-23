import ast
import json
import os
import re
from typing import Any, Dict, List, Tuple

_REQUIRED_META_FIELDS = {"id", "name", "description", "enabled", "version_code", "version", "package_name"}
_TRIGGER_OPTIONAL_FIELDS = {"semantic", "params", "permissions", "origin", "trigger_api"}
_ACTION_OPTIONAL_FIELDS = {"params", "permissions", "origin", "execution_api", "precondition_api", "outputs"}
_ALLOWED_SEMANTICS = {"state", "oneshot"}
_ALLOWED_PARAM_TYPES = {"string", "number", "select", "bool", "time", "hotkey", "path", "textarea"}
_REQUIRED_PARAM_FIELDS = {"name", "type", "label"}
_ALLOWED_ORIGINS = {"builtin", "user", "third_party"}
_PACKAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
# id 仅允许字母/数字/下划线/连字符，禁止路径分隔符（防 ../ 路径穿越）
_PLUGIN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

# =========================================================================
# 权限注册表
# =========================================================================

PERM_RISK_NONE = "none"
PERM_RISK_LOW = "low"
PERM_RISK_MEDIUM = "medium"
PERM_RISK_HIGH = "high"

PermissionInfo = Dict[str, Any]  # {label, risk, description}

PERMISSION_REGISTRY: Dict[str, PermissionInfo] = {
    "notification": {
        "label": "发送通知",
        "risk": PERM_RISK_NONE,
        "description": "允许插件发送系统通知",
    },
    "audio": {
        "label": "音频",
        "risk": PERM_RISK_LOW,
        "description": "允许插件录制或播放音频",
    },
    "clipboard": {
        "label": "剪贴板",
        "risk": PERM_RISK_MEDIUM,
        "description": "允许插件读写剪贴板",
    },
    "network": {
        "label": "网络访问",
        "risk": PERM_RISK_MEDIUM,
        "description": "允许插件访问网络",
    },
    "external_binary": {
        "label": "外部程序",
        "risk": PERM_RISK_MEDIUM,
        "description": "允许插件调用外部可执行程序",
    },
    "native_api": {
        "label": "原生 API",
        "risk": PERM_RISK_MEDIUM,
        "description": "允许插件调用 Windows 原生 API（ctypes/PyWin32）",
    },
    "filesystem": {
        "label": "文件系统",
        "risk": PERM_RISK_HIGH,
        "description": "允许插件读写插件目录外的文件",
    },
    "process": {
        "label": "进程管理",
        "risk": PERM_RISK_HIGH,
        "description": "允许插件创建或终止进程",
    },
    "registry": {
        "label": "注册表",
        "risk": PERM_RISK_HIGH,
        "description": "允许插件读写 Windows 注册表",
    },
    "screen_reader": {
        "label": "屏幕读取",
        "risk": PERM_RISK_HIGH,
        "description": "允许插件读取屏幕内容或获取窗口信息",
    },
    "admin": {
        "label": "管理员权限",
        "risk": PERM_RISK_HIGH,
        "description": "允许插件以管理员身份执行操作",
    },
}

_ALLOWED_PERMISSIONS = set(PERMISSION_REGISTRY.keys())


def get_permission_info(perm: str) -> PermissionInfo | None:
    return PERMISSION_REGISTRY.get(perm)


def is_known_permission(perm: str) -> bool:
    return perm in PERMISSION_REGISTRY


# =========================================================================
# 安全扫描器 — 静态分析 .py 中的危险模式
# =========================================================================

# (pattern_name, risk_label, risk_level, search_terms)
_RISK_PATTERNS: List[Tuple[str, str, str, List[str]]] = [
    ("code_injection", "代码注入", PERM_RISK_HIGH, ["eval(", "exec(", "compile(", "__import__"]),
    ("subprocess", "子进程", PERM_RISK_HIGH, ["subprocess.", "os.system", "os.popen"]),
    ("dynamic_import", "动态导入", PERM_RISK_MEDIUM, ["importlib.", "__import__"]),
    ("file_write", "文件写入", PERM_RISK_MEDIUM, ["open(", "shutil.copy", "shutil.move"]),
    ("network_request", "网络请求", PERM_RISK_MEDIUM, ["requests.", "urllib.", "socket."]),
    ("registry_access", "注册表访问", PERM_RISK_HIGH, ["winreg.", "_winreg."]),
    ("native_call", "原生调用", PERM_RISK_MEDIUM, ["ctypes.", "ctypes.windll"]),
]


def scan_plugin_security(plugin_dir: str) -> List[Dict[str, Any]]:
    """扫描插件目录下的 .py 文件，返回发现的风险列表。"""
    risks: List[Dict[str, Any]] = []
    if not os.path.isdir(plugin_dir):
        return risks

    for fname in sorted(os.listdir(plugin_dir)):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(plugin_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError:
            continue

        for pattern_id, label, level, terms in _RISK_PATTERNS:
            for term in terms:
                if term in source:
                    risks.append({
                        "id": pattern_id,
                        "label": label,
                        "level": level,
                        "detail": f"文件 \"{fname}\" 中发现 \"{term}\"",
                        "file": fname,
                    })
                    break  # 每种风险只报一次
    return risks


def check_permissions_conform(
    perms: List[str],
) -> Tuple[bool, List[str]]:
    """检查权限列表是否符合规范（所有权限均在已知注册表中）。"""
    unknown = [p for p in perms if not is_known_permission(p)]
    if unknown:
        return False, [f"未知权限: {p}" for p in unknown]
    return True, []


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

    if "trigger_api" in meta and meta["trigger_api"] not in ("legacy", "event-v1"):
        errors.append("trigger_api 必须为 legacy 或 event-v1")

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

    if "execution_api" in meta and meta["execution_api"] not in ("legacy", "context-v1"):
        errors.append("execution_api 必须为 legacy 或 context-v1")
    if "precondition_api" in meta and meta["precondition_api"] != "context-v1":
        errors.append("precondition_api 目前仅支持 context-v1")
    if "outputs" in meta:
        outputs = meta["outputs"]
        if not isinstance(outputs, list) or not all(isinstance(item, str) and item for item in outputs):
            errors.append("outputs 必须是非空字符串数组")

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
                if ptype == "select":
                    if "options" not in param:
                        errors.append(f"params[{i}] (type=select) 必须提供 'options' 字段")
                    elif not isinstance(param["options"], list):
                        errors.append(f"params[{i}] options 必须是数组")
                    else:
                        for opt in param["options"]:
                            # 支持两种格式：字符串 或 {"value": "...", "label": "..."}
                            if isinstance(opt, str):
                                continue
                            if isinstance(opt, dict) and isinstance(opt.get("value"), str):
                                continue
                            errors.append(f"params[{i}] options 元素必须是字符串或含 value 字符串的对象")

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
