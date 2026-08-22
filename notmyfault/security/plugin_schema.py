import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_REQUIRED_META_FIELDS = {"id", "name", "description", "enabled", "version_code", "version", "package_name"}
_TRIGGER_OPTIONAL_FIELDS = {
    "semantic", "params", "permissions", "origin", "trigger_api", "platforms",
    "entrypoints", "outputs", "build", "components", "contributes",
    "requires_capabilities", "engines",
}
_ACTION_OPTIONAL_FIELDS = {
    "params", "permissions", "origin", "execution_api", "precondition_api",
    "outputs", "platforms", "entrypoints", "build", "idempotent",
    "cancellation_api", "components", "contributes", "requires_capabilities",
    "execution_mode", "engines",
}
_ALLOWED_EXECUTION_MODES = {"in-process", "isolated"}
_ALLOWED_SEMANTICS = {"state", "oneshot"}
_ALLOWED_PARAM_TYPES = {
    "string", "number", "select", "bool", "time", "hotkey", "path",
    "textarea", "uia_selector", "macro", "plugin_data",
}
_ALLOWED_OUTPUT_TYPES = {"string", "number", "bool", "array", "object", "any"}
_ALLOWED_SUMMARY_POLICIES = {"shape", "value", "hidden"}
_REQUIRED_PARAM_FIELDS = {"name", "type", "label"}
_REQUIRED_OUTPUT_FIELDS = {"name", "type", "label"}
_ALLOWED_ORIGINS = {"builtin", "user", "third_party"}
_ALLOWED_PLATFORMS = {"windows", "linux", "macos"}
_PACKAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_COMPONENT_REQUIRED_FIELDS = {"id", "name", "entrypoint"}
_COMPONENT_OPTIONAL_FIELDS = {"description", "api", "ui", "param_types"}
_COMPONENT_APIS = {"component-v1"}
_COMPONENT_UI_FIELDS = {"button_label", "icon", "description"}
_CONTRIBUTION_FIELDS = {"commands", "parameter_editors", "views", "data_types"}
_COMMAND_FIELDS = {"id", "title", "description", "handler"}
_VIEW_FIELDS = {
    "id", "title", "description", "page", "commands", "window_controls",
}
_DATA_TYPE_FIELDS = {"id", "version", "binding"}
_PARAMETER_EDITOR_FIELDS = {
    "id", "parameter", "data_type", "command", "view", "ui", "accepts_legacy",
}
_PARAMETER_EDITOR_UI_FIELDS = {
    "control", "icon", "empty_label", "description",
}
_DATA_BINDING_POLICIES = {"private"}
_EDITOR_CONTROLS = {"button"}
# plugin id 只允许字母、数字、下划线和连字符，路径分隔符会把 id 变成路径。
_PLUGIN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_COMPONENT_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


def is_valid_plugin_id(plugin_id: str) -> bool:
    return bool(_PLUGIN_ID_RE.match(plugin_id))


def current_platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform

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
    "input_monitor": {
        "label": "监听键盘和鼠标",
        "risk": PERM_RISK_HIGH,
        "description": "允许插件在录制期间读取全局键盘和鼠标输入",
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


_RISK_INFO = {
    "code_injection": ("代码注入", PERM_RISK_HIGH),
    "subprocess": ("子进程", PERM_RISK_HIGH),
    "dynamic_import": ("动态导入", PERM_RISK_MEDIUM),
    "file_write": ("文件写入", PERM_RISK_MEDIUM),
    "network_request": ("网络请求", PERM_RISK_MEDIUM),
    "registry_access": ("注册表访问", PERM_RISK_HIGH),
    "native_call": ("原生调用", PERM_RISK_MEDIUM),
}


def _resolved_name(
    node: ast.AST,
    module_aliases: Dict[str, str],
    imported_symbols: Dict[str, str],
) -> str | None:
    if isinstance(node, ast.Name):
        return imported_symbols.get(node.id, module_aliases.get(node.id, node.id))
    if isinstance(node, ast.Attribute):
        owner = _resolved_name(node.value, module_aliases, imported_symbols)
        if owner:
            return owner + "." + node.attr
    return None


def _risk_ids_for_name(name: str, is_call: bool) -> List[str]:
    risk_ids: List[str] = []
    if is_call and name in {
        "eval", "exec", "compile", "__import__",
        "builtins.eval", "builtins.exec", "builtins.compile", "builtins.__import__",
    }:
        risk_ids.append("code_injection")
    if name in {"__import__", "builtins.__import__"}:
        if "code_injection" not in risk_ids:
            risk_ids.append("code_injection")
        risk_ids.append("dynamic_import")
    if name.startswith("subprocess.") or name in {"os.system", "os.popen"}:
        risk_ids.append("subprocess")
    if name.startswith("importlib."):
        risk_ids.append("dynamic_import")
    if is_call and (
        name in {"open", "builtins.open"}
        or name.startswith("shutil.copy")
        or name == "shutil.move"
    ):
        risk_ids.append("file_write")
    if name.startswith(("requests.", "urllib.", "socket.")):
        risk_ids.append("network_request")
    if name.startswith(("winreg.", "_winreg.")):
        risk_ids.append("registry_access")
    if name.startswith("ctypes."):
        risk_ids.append("native_call")
    return risk_ids


def scan_plugin_security(plugin_dir: str) -> List[Dict[str, Any]]:
    """扫描插件目录下的 .py 文件，返回发现的风险列表"""
    risks: List[Dict[str, Any]] = []
    if not os.path.isdir(plugin_dir):
        return risks

    for fpath_obj in sorted(Path(plugin_dir).rglob("*.py")):
        if not fpath_obj.is_file():
            continue
        fpath = str(fpath_obj)
        fname = fpath_obj.relative_to(plugin_dir).as_posix()
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=fpath)
        except SyntaxError:
            continue

        module_aliases: Dict[str, str] = {}
        imported_symbols: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local_name = alias.asname or alias.name.split(".")[0]
                    module_aliases[local_name] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                for alias in node.names:
                    imported_symbols[alias.asname or alias.name] = (
                        module_name + "." + alias.name
                    ).strip(".")

        findings: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _resolved_name(node.func, module_aliases, imported_symbols)
                is_call = True
            elif isinstance(node, ast.Attribute):
                name = _resolved_name(node, module_aliases, imported_symbols)
                is_call = False
            elif isinstance(node, ast.Name) and node.id == "__import__":
                name = _resolved_name(node, module_aliases, imported_symbols)
                is_call = False
            else:
                continue
            if not name:
                continue
            for risk_id in _risk_ids_for_name(name, is_call):
                findings.setdefault(risk_id, name)

        for risk_id, (label, level) in _RISK_INFO.items():
            evidence = findings.get(risk_id)
            if evidence is None:
                continue
            risks.append({
                "id": risk_id,
                "label": label,
                "level": level,
                "detail": f"文件 \"{fname}\" 中发现 \"{evidence}\"",
                "file": fname,
            })
    return risks


def check_permissions_conform(
    perms: List[str],
) -> Tuple[bool, List[str]]:
    """检查权限列表中的每项是否都在权限注册表中。"""
    unknown = [p for p in perms if not is_known_permission(p)]
    if unknown:
        return False, [f"未知权限: {p}" for p in unknown]
    return True, []


def _validate_build_field(build: Any) -> List[str]:
    """校验安装期编译钩子字段，command 和 outputs 都是可选子字段"""
    errors: List[str] = []
    if not isinstance(build, dict):
        return ["字段 'build' 必须是对象"]
    for key in build:
        if key not in ("command", "outputs"):
            errors.append(f"build 包含未知字段: '{key}'")
    if "command" in build:
        command = build["command"]
        if not isinstance(command, list) or not command:
            errors.append("build.command 必须是非空字符串数组")
        else:
            if len(command) > 10:
                errors.append("build.command 最多 10 项")
            for i, item in enumerate(command):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"build.command[{i}] 必须是非空字符串")
    if "outputs" in build:
        outputs = build["outputs"]
        if not isinstance(outputs, list):
            errors.append("build.outputs 必须是数组")
        else:
            for i, item in enumerate(outputs):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"build.outputs[{i}] 必须是非空字符串")
                    continue
                normalized = item.replace("\\", "/")
                if (
                    "\\" in item
                    or normalized.startswith("/")
                    or any(part in ("", ".", "..") for part in normalized.split("/"))
                ):
                    errors.append(
                        f"build.outputs[{i}] 必须是插件目录内的相对路径: {item!r}"
                    )
    return errors


def _relative_plugin_path_ok(value: Any) -> bool:
    """返回相对路径是否满足单层安全约束，不展开真实路径"""
    if not isinstance(value, str) or not value:
        return False
    normalized = value.replace("\\", "/")
    if (
        normalized.startswith("/")
        or "\\" in value
        or any(part in ("", ".", "..") for part in normalized.split("/"))
    ):
        return False
    return True


def _validate_components_field(components: Any) -> List[str]:
    """校验插件声明的组件数组，触发器和动作插件共用同一份结构"""
    errors: List[str] = []
    if not isinstance(components, list):
        return ["字段 'components' 必须是数组"]
    if not components:
        return ["字段 'components' 不能为空数组"]
    seen: set[str] = set()
    for index, component in enumerate(components):
        prefix = f"components[{index}]"
        if not isinstance(component, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        for field in sorted(_COMPONENT_REQUIRED_FIELDS):
            if field not in component:
                errors.append(f"{prefix} 缺少必填字段: {field}")
        for key in component:
            if key not in _COMPONENT_REQUIRED_FIELDS | _COMPONENT_OPTIONAL_FIELDS:
                errors.append(f"{prefix} 包含未知字段: '{key}'")
        component_id = component.get("id")
        if not isinstance(component_id, str) or not _COMPONENT_ID_RE.match(component_id):
            errors.append(
                f"{prefix}.id 必须是以字母开头的 id（字母/数字/下划线/连字符）"
            )
        elif component_id in seen:
            errors.append(f"components 包含重复 id: '{component_id}'")
        else:
            seen.add(component_id)
        if "name" in component and not isinstance(component["name"], str):
            errors.append(f"{prefix}.name 必须是字符串")
        if "description" in component and not isinstance(component["description"], str):
            errors.append(f"{prefix}.description 必须是字符串")
        if "api" in component and component["api"] not in _COMPONENT_APIS:
            errors.append(
                f"{prefix}.api 目前仅支持 {', '.join(sorted(_COMPONENT_APIS))}"
            )
        if "param_types" in component:
            param_types = component["param_types"]
            if not isinstance(param_types, list) or not param_types:
                errors.append(f"{prefix}.param_types 必须是非空字符串数组")
            else:
                for ptype in param_types:
                    if not isinstance(ptype, str) or ptype not in _ALLOWED_PARAM_TYPES:
                        errors.append(
                            f"{prefix}.param_types 包含无效参数类型: {ptype!r}"
                        )
        if "entrypoint" in component:
            entrypoint = component["entrypoint"]
            if not isinstance(entrypoint, str) or not entrypoint.endswith(".py"):
                errors.append(f"{prefix}.entrypoint 必须是相对 .py 文件路径")
            elif not _relative_plugin_path_ok(entrypoint):
                errors.append(
                    f"{prefix}.entrypoint 必须位于插件目录内: {entrypoint!r}"
                )
        if "ui" in component:
            ui = component["ui"]
            if not isinstance(ui, dict):
                errors.append(f"{prefix}.ui 必须是对象")
            else:
                for key in ui:
                    if key not in _COMPONENT_UI_FIELDS:
                        errors.append(f"{prefix}.ui 包含未知字段: '{key}'")
                for key in ("button_label", "icon", "description"):
                    if key in ui and not isinstance(ui[key], str):
                        errors.append(f"{prefix}.ui.{key} 必须是字符串")
    return errors


def _validate_string_id(value: Any, path: str, errors: List[str]) -> bool:
    if not isinstance(value, str) or not _COMPONENT_ID_RE.match(value):
        errors.append(
            f"{path} 必须是以字母开头的 id（字母/数字/下划线/连字符）"
        )
        return False
    return True


def _validate_contribution_items(
    contributes: Dict[str, Any],
    field: str,
    allowed_fields: set[str],
    required_fields: set[str],
    errors: List[str],
) -> List[Dict[str, Any]]:
    value = contributes.get(field, [])
    if not isinstance(value, list):
        errors.append(f"contributes.{field} 必须是数组")
        return []
    seen: set[str] = set()
    items: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        prefix = f"contributes.{field}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        items.append(item)
        for required in sorted(required_fields):
            if required not in item:
                errors.append(f"{prefix} 缺少必填字段: {required}")
        for key in item:
            if key not in allowed_fields:
                errors.append(f"{prefix} 包含未知字段: '{key}'")
        item_id = item.get("id")
        if _validate_string_id(item_id, f"{prefix}.id", errors):
            if item_id in seen:
                errors.append(f"contributes.{field} 包含重复 id: '{item_id}'")
            seen.add(item_id)
    return items


def _validate_contributes_field(
    contributes: Any,
    params: Any,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(contributes, dict) or not contributes:
        return ["字段 'contributes' 必须是非空对象"]
    for key in contributes:
        if key not in _CONTRIBUTION_FIELDS:
            errors.append(f"contributes 包含未知字段: '{key}'")

    commands = _validate_contribution_items(
        contributes,
        "commands",
        _COMMAND_FIELDS,
        {"id", "title", "handler"},
        errors,
    )
    views = _validate_contribution_items(
        contributes,
        "views",
        _VIEW_FIELDS,
        {"id", "title", "page"},
        errors,
    )
    data_types = _validate_contribution_items(
        contributes,
        "data_types",
        _DATA_TYPE_FIELDS,
        {"id", "version"},
        errors,
    )
    editors = _validate_contribution_items(
        contributes,
        "parameter_editors",
        _PARAMETER_EDITOR_FIELDS,
        {"id", "parameter", "data_type", "command", "ui"},
        errors,
    )

    command_ids = {
        item.get("id") for item in commands if isinstance(item.get("id"), str)
    }
    view_ids = {
        item.get("id") for item in views if isinstance(item.get("id"), str)
    }
    data_type_ids = {
        item.get("id") for item in data_types if isinstance(item.get("id"), str)
    }
    param_names = {
        item.get("name")
        for item in params if isinstance(item, dict) and isinstance(item.get("name"), str)
    } if isinstance(params, list) else set()

    for index, command in enumerate(commands):
        prefix = f"contributes.commands[{index}]"
        for key in ("title", "description"):
            if key in command and not isinstance(command[key], str):
                errors.append(f"{prefix}.{key} 必须是字符串")
        handler = command.get("handler")
        if not isinstance(handler, str) or ":" not in handler:
            errors.append(f"{prefix}.handler 必须是“相对.py路径:函数名”")
            continue
        entrypoint, symbol = handler.rsplit(":", 1)
        if not entrypoint.endswith(".py") or not _relative_plugin_path_ok(entrypoint):
            errors.append(f"{prefix}.handler 必须位于插件目录内")
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", symbol):
            errors.append(f"{prefix}.handler 函数名无效")

    for index, view in enumerate(views):
        prefix = f"contributes.views[{index}]"
        for key in ("title", "description"):
            if key in view and not isinstance(view[key], str):
                errors.append(f"{prefix}.{key} 必须是字符串")
        page = view.get("page")
        if not isinstance(page, str) or not page.endswith(".html"):
            errors.append(f"{prefix}.page 必须是相对 .html 文件路径")
        elif not _relative_plugin_path_ok(page):
            errors.append(f"{prefix}.page 必须位于插件目录内")
        allowed_commands = view.get("commands", [])
        if not isinstance(allowed_commands, list):
            errors.append(f"{prefix}.commands 必须是数组")
        else:
            for command_id in allowed_commands:
                if command_id not in command_ids:
                    errors.append(
                        f"{prefix}.commands 引用了未声明命令: {command_id!r}"
                    )
        window_controls = view.get("window_controls", [])
        if not isinstance(window_controls, list):
            errors.append(f"{prefix}.window_controls 必须是数组")
        else:
            for action in window_controls:
                if action not in ("minimize", "restore"):
                    errors.append(
                        f"{prefix}.window_controls 包含无效操作: {action!r}"
                    )

    for index, data_type in enumerate(data_types):
        prefix = f"contributes.data_types[{index}]"
        version = data_type.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            errors.append(f"{prefix}.version 必须是大于 0 的整数")
        binding = data_type.get("binding", "private")
        if binding not in _DATA_BINDING_POLICIES:
            errors.append(f"{prefix}.binding 目前仅支持 private")
    editor_params: set[str] = set()
    for index, editor in enumerate(editors):
        prefix = f"contributes.parameter_editors[{index}]"
        parameter = editor.get("parameter")
        if parameter not in param_names:
            errors.append(f"{prefix}.parameter 引用了未声明参数: {parameter!r}")
        elif parameter in editor_params:
            errors.append(f"参数 {parameter!r} 声明了多个 parameter_editor")
        else:
            editor_params.add(parameter)
        if editor.get("data_type") not in data_type_ids:
            errors.append(
                f"{prefix}.data_type 引用了未声明数据类型: {editor.get('data_type')!r}"
            )
        if editor.get("command") not in command_ids:
            errors.append(
                f"{prefix}.command 引用了未声明命令: {editor.get('command')!r}"
            )
        if "view" in editor and editor.get("view") not in view_ids:
            errors.append(
                f"{prefix}.view 引用了未声明视图: {editor.get('view')!r}"
            )
        if "accepts_legacy" in editor and not isinstance(editor["accepts_legacy"], bool):
            errors.append(f"{prefix}.accepts_legacy 必须为布尔值")
        ui = editor.get("ui")
        if not isinstance(ui, dict):
            errors.append(f"{prefix}.ui 必须是对象")
        else:
            for key in ui:
                if key not in _PARAMETER_EDITOR_UI_FIELDS:
                    errors.append(f"{prefix}.ui 包含未知字段: '{key}'")
            if ui.get("control", "button") not in _EDITOR_CONTROLS:
                errors.append(f"{prefix}.ui.control 目前仅支持 button")
            for key in ("icon", "empty_label", "description"):
                if key in ui and not isinstance(ui[key], str):
                    errors.append(f"{prefix}.ui.{key} 必须是字符串")

    if isinstance(params, list):
        for index, param in enumerate(params):
            if not isinstance(param, dict) or param.get("type") != "plugin_data":
                continue
            if param.get("data_type") not in data_type_ids:
                errors.append(
                    f"params[{index}].data_type 引用了未声明数据类型: "
                    f"{param.get('data_type')!r}"
                )
            if param.get("name") not in editor_params:
                errors.append(
                    f"params[{index}] (type=plugin_data) 必须声明 parameter_editor"
                )
    return errors


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

    if "trigger_api" in meta and meta["trigger_api"] not in ("legacy", "event-v1", "event-v2"):
        errors.append("trigger_api 必须为 legacy、event-v1 或 event-v2")

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

    if "execution_mode" in meta:
        # isolated 是给第三方动作做故障隔离的实验字段，内置插件必须走进程内
        if meta.get("execution_mode") not in _ALLOWED_EXECUTION_MODES:
            errors.append(
                "execution_mode 必须是 in-process 或 isolated，实际: "
                f"{meta.get('execution_mode')!r}"
            )
        elif meta.get("origin") == "builtin" and meta["execution_mode"] == "isolated":
            errors.append("内置插件不允许 execution_mode: isolated")

    if "engines" in meta:
        engines = meta["engines"]
        if not isinstance(engines, dict):
            errors.append("engines 必须是对象")
        else:
            api_version = engines.get("notmyfault_api")
            if api_version is None:
                pass
            elif (
                isinstance(api_version, bool)
                or not isinstance(api_version, int)
                or api_version < 1
            ):
                errors.append("engines.notmyfault_api 必须是正整数")

    if "platforms" in meta:
        platforms = meta["platforms"]
        if not isinstance(platforms, list) or not platforms:
            errors.append("字段 'platforms' 必须是非空数组")
        else:
            for platform in platforms:
                if platform not in _ALLOWED_PLATFORMS:
                    errors.append(
                        f"platforms 包含无效平台: {platform!r}"
                        f"（允许: {', '.join(sorted(_ALLOWED_PLATFORMS))}）"
                    )

    if "requires_capabilities" in meta:
        from notmyfault.platform.capabilities import CAPABILITY_IDS

        required = meta["requires_capabilities"]
        if not isinstance(required, list) or not required:
            errors.append("字段 'requires_capabilities' 必须是非空数组")
        else:
            for capability in required:
                if not isinstance(capability, str):
                    errors.append(
                        f"requires_capabilities 中的值必须是字符串，实际: {type(capability).__name__}"
                    )
                elif capability not in CAPABILITY_IDS:
                    errors.append(
                        f"未知能力 id: {capability!r}"
                        f"（允许: {', '.join(sorted(CAPABILITY_IDS))}）"
                    )

    if "entrypoints" in meta:
        entrypoints = meta["entrypoints"]
        if not isinstance(entrypoints, dict) or not entrypoints:
            errors.append("字段 'entrypoints' 必须是非空对象")
        else:
            for platform, entrypoint in entrypoints.items():
                if platform not in _ALLOWED_PLATFORMS:
                    errors.append(f"entrypoints 包含无效平台: {platform!r}")
                    continue
                if not isinstance(entrypoint, str) or not entrypoint.endswith(".py"):
                    errors.append(
                        f"entrypoints.{platform} 必须是相对 .py 文件路径"
                    )
                    continue
                normalized = entrypoint.replace("\\", "/")
                if (
                    normalized.startswith("/")
                    or "\\" in entrypoint
                    or any(part in ("", ".", "..") for part in normalized.split("/"))
                ):
                    errors.append(
                        f"entrypoints.{platform} 必须位于插件目录内: {entrypoint!r}"
                    )
            platforms = meta.get("platforms")
            if isinstance(platforms, list) and set(entrypoints) != set(platforms):
                errors.append("platforms 与 entrypoints 的平台集合必须一致")

    if "execution_api" in meta and meta["execution_api"] not in ("legacy", "context-v1"):
        errors.append("execution_api 必须为 legacy 或 context-v1")
    if "precondition_api" in meta and meta["precondition_api"] != "context-v1":
        errors.append("precondition_api 目前仅支持 context-v1")
    if "cancellation_api" in meta:
        if meta["cancellation_api"] != "runtime-v1":
            errors.append("cancellation_api 目前仅支持 runtime-v1")
        if meta.get("execution_api") != "context-v1":
            errors.append("cancellation_api=runtime-v1 需要 execution_api=context-v1")
    if "build" in meta:
        errors.extend(_validate_build_field(meta["build"]))
    if "components" in meta:
        errors.extend(_validate_components_field(meta["components"]))
    if "contributes" in meta:
        errors.extend(
            _validate_contributes_field(meta["contributes"], meta.get("params", []))
        )
    if "outputs" in meta:
        outputs = meta["outputs"]
        if not isinstance(outputs, list):
            errors.append("outputs 必须是数组")
        else:
            output_names: set[str] = set()
            for i, output in enumerate(outputs):
                # 旧 action 插件可把输出写成字符串列表。
                if isinstance(output, str):
                    if not output:
                        errors.append(f"outputs[{i}] 不能为空")
                        continue
                    output_name = output
                elif isinstance(output, dict):
                    for field in sorted(_REQUIRED_OUTPUT_FIELDS):
                        if field not in output:
                            errors.append(f"outputs[{i}] 缺少必填字段: {field}")
                    output_name = output.get("name")
                    output_type = output.get("type")
                    if output_type and output_type not in _ALLOWED_OUTPUT_TYPES:
                        errors.append(
                            f"outputs[{i}].type 无效: '{output_type}'"
                            f"（允许: {', '.join(sorted(_ALLOWED_OUTPUT_TYPES))}）"
                        )
                    for flag in ("required", "sensitive"):
                        if flag in output and not isinstance(output[flag], bool):
                            errors.append(f"outputs[{i}].{flag} 必须为布尔值")
                    if (
                        "summary" in output
                        and output["summary"] not in _ALLOWED_SUMMARY_POLICIES
                    ):
                        errors.append(
                            f"outputs[{i}].summary 无效: {output['summary']!r}"
                        )
                    if (
                        output_type == "array"
                        and "item_type" in output
                        and output["item_type"] not in _ALLOWED_OUTPUT_TYPES - {"array"}
                    ):
                        errors.append(f"outputs[{i}].item_type 无效")
                else:
                    errors.append(f"outputs[{i}] 必须是字符串或对象")
                    continue

                if not isinstance(output_name, str) or not output_name:
                    errors.append(f"outputs[{i}].name 必须是非空字符串")
                    continue
                if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", output_name):
                    errors.append(f"outputs[{i}].name 含非法字符: {output_name!r}")
                if output_name in output_names:
                    errors.append(f"outputs 包含重复名称: {output_name}")
                output_names.add(output_name)

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
                value_type = param.get("value_type")
                if value_type is not None and value_type not in _ALLOWED_OUTPUT_TYPES:
                    errors.append(
                        f"params[{i}].value_type 无效: '{value_type}'"
                        f"（允许: {', '.join(sorted(_ALLOWED_OUTPUT_TYPES))}）"
                    )
                if ptype == "plugin_data":
                    data_type = param.get("data_type")
                    if not isinstance(data_type, str) or not _COMPONENT_ID_RE.match(data_type):
                        errors.append(
                            f"params[{i}].data_type 必须是以字母开头的 id"
                        )
                for flag in ("required", "sensitive", "capture_only"):
                    if flag in param and not isinstance(param[flag], bool):
                        errors.append(f"params[{i}].{flag} 必须为布尔值")
                if (
                    "summary" in param
                    and param["summary"] not in _ALLOWED_SUMMARY_POLICIES
                ):
                    errors.append(f"params[{i}].summary 无效: {param['summary']!r}")
                if ptype == "select":
                    if "options" not in param:
                        errors.append(f"params[{i}] (type=select) 必须提供 'options' 字段")
                    elif not isinstance(param["options"], list):
                        errors.append(f"params[{i}] options 必须是数组")
                    else:
                        for opt in param["options"]:
                            # options 支持字符串，或带 value 字段的对象。
                            if isinstance(opt, str):
                                continue
                            if isinstance(opt, dict) and isinstance(opt.get("value"), str):
                                continue
                            errors.append(f"params[{i}] options 元素必须是字符串或含 value 字符串的对象")

    if "idempotent" in meta and not isinstance(meta["idempotent"], bool):
        errors.append("字段 'idempotent' 必须是布尔值")

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


def check_payload_contract(
    outputs: Any,
    payload: Dict[str, Any],
) -> List[str]:
    """按 outputs 声明检查 event-v2 payload，检查必填字段、未声明字段和 string、number、bool 的类型，旧插件未声明 outputs 时跳过检查。"""
    declared: Dict[str, Dict[str, Any]] = {}
    if isinstance(outputs, list):
        for output in outputs:
            if isinstance(output, str):
                declared[output] = {"type": "any", "required": True}
            elif isinstance(output, dict) and isinstance(output.get("name"), str):
                declared[output["name"]] = {"required": True, **output}
    if not declared:
        return []

    problems: List[str] = []
    for name, spec in declared.items():
        if spec.get("required") is not False and name not in payload:
            problems.append(f"缺少必填输出字段: {name}")
        elif name in payload:
            value = payload[name]
            output_type = spec.get("type", "any")
            if output_type == "string" and not isinstance(value, str):
                problems.append(
                    f"输出字段 {name} 应为 string，实际为 {type(value).__name__}"
                )
            elif output_type == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    problems.append(
                        f"输出字段 {name} 应为 number，实际为 {type(value).__name__}"
                    )
            elif output_type == "bool" and not isinstance(value, bool):
                problems.append(
                    f"输出字段 {name} 应为 bool，实际为 {type(value).__name__}"
                )
    for key in payload:
        if key not in declared:
            problems.append(f"未声明的输出字段: {key}")
    return problems


def scan_plugins(base_dir: str, plugins_dir: str, json_filename: str) -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    root = os.path.join(base_dir, plugins_dir)
    if not os.path.isdir(root):
        return result

    for folder_name in sorted(os.listdir(root)):
        # 与加载器一致，跳过解释器和开发工具生成的目录；.nmf-backup 是更新时留下的旧版本备份
        if folder_name.startswith(".") or folder_name in (
            "__pycache__", "__pypackages__", "node_modules",
        ) or folder_name.endswith(".nmf-backup"):
            continue
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

        plugin_type = "trigger" if json_filename == "trigger.json" else "action"
        is_valid, _ = validate_plugin_meta(meta, plugin_type)
        if not is_valid:
            continue

        current_platform = current_platform_name()
        entrypoints = meta.get("entrypoints") or {}
        platforms = meta.get("platforms") or list(entrypoints)
        compatible = (
            current_platform in entrypoints
            if entrypoints
            else not platforms or current_platform in platforms
        )
        result[plugin_id] = {
            **meta,
            "platform_compatible": compatible,
            "current_platform": current_platform,
            "selected_entrypoint": entrypoints.get(current_platform),
        }

    return result
