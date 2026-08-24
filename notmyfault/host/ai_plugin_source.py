from __future__ import annotations

import ast
from copy import deepcopy
from typing import Any, Mapping

from notmyfault.security.plugin_schema import (
    current_platform_name,
    get_permission_info,
    is_valid_plugin_id,
    scan_plugin_source_security,
    validate_plugin_meta,
)
from notmyfault.security.plugins import analyze_plugin_source

_PLUGIN_KINDS = frozenset({"trigger", "action"})

_UNSUPPORTED_MANIFEST_FIELDS = frozenset(
    {"build", "entrypoints", "components", "contributes"}
)

_FORBIDDEN_CAPABILITIES = frozenset({"self_elevation", "dynamic_exec"})


class AIPluginSourceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _top_level_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    functions: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            functions[node.name] = node
    return functions


def _positional_arity(func: ast.FunctionDef) -> tuple[int, int | None]:
    args = func.args
    required = len(args.posonlyargs) + len(args.args) - len(args.defaults)
    if args.vararg is not None:
        return required, None
    return required, len(args.posonlyargs) + len(args.args)


def _accepts_positional(func: ast.FunctionDef, count: int) -> bool:
    required, maximum = _positional_arity(func)
    return required <= count and (maximum is None or count <= maximum)


def _require_entrypoints(
    kind: str,
    manifest: Mapping[str, Any],
    functions: dict[str, ast.FunctionDef],
) -> None:
    if "run" not in functions:
        raise AIPluginSourceError("missing_entrypoint", "source 缺少顶层 run() 函数")

    run = functions["run"]
    if kind == "action":
        if not _accepts_positional(run, 2):
            raise AIPluginSourceError(
                "missing_entrypoint", "action 的 run() 必须接受 2 个位置参数"
            )
        if manifest.get("execution_api") == "context-v1":
            if "run_with_context" not in functions:
                raise AIPluginSourceError(
                    "missing_entrypoint",
                    "execution_api=context-v1 要求定义 run_with_context()",
                )
            if not _accepts_positional(functions["run_with_context"], 3):
                raise AIPluginSourceError(
                    "missing_entrypoint",
                    "run_with_context() 必须接受 3 个位置参数",
                )
        if manifest.get("precondition_api") == "context-v1":
            if "check_precondition" not in functions:
                raise AIPluginSourceError(
                    "missing_entrypoint",
                    "precondition_api=context-v1 要求定义 check_precondition()",
                )
            if not _accepts_positional(functions["check_precondition"], 3):
                raise AIPluginSourceError(
                    "missing_entrypoint",
                    "check_precondition() 必须接受 3 个位置参数",
                )
        return

    if manifest.get("trigger_api") in ("event-v1", "event-v2"):
        if not _accepts_positional(run, 4):
            raise AIPluginSourceError(
                "missing_entrypoint",
                f"{manifest['trigger_api']} 的 run() 必须接受 4 个位置参数",
            )


def _generated_test_source(kind: str) -> str:
    source_name = "action.py" if kind == "action" else "trigger.py"
    return (
        "import importlib.util\n"
        "from pathlib import Path\n\n\n"
        "def test_plugin_entrypoint_is_importable():\n"
        f"    source = Path(__file__).with_name({source_name!r})\n"
        "    spec = importlib.util.spec_from_file_location('plugin_under_test', source)\n"
        "    assert spec is not None and spec.loader is not None\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(module)\n"
        "    assert callable(module.run)\n"
    )


def _compatibility_notes(kind: str, manifest: Mapping[str, Any]) -> list[str]:
    platforms = manifest.get("platforms")
    if isinstance(platforms, list) and platforms:
        platform_note = "声明支持平台: " + ", ".join(platforms)
    else:
        platform_note = "未限制平台；实际兼容性仍取决于源码和运行环境"
    capabilities = manifest.get("requires_capabilities")
    capability_note = (
        "所需系统能力: " + ", ".join(capabilities)
        if isinstance(capabilities, list) and capabilities
        else "未声明额外系统能力"
    )
    api_name = manifest.get("execution_api") if kind == "action" else manifest.get("trigger_api")
    return [
        platform_note,
        capability_note,
        f"当前检查平台: {current_platform_name()}",
        f"入口 API: {api_name or 'legacy'}",
    ]


def _permission_explanations(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    result = []
    permissions = manifest.get("permissions")
    for permission in permissions if isinstance(permissions, list) else []:
        info = get_permission_info(permission) or {}
        result.append({
            "permission": permission,
            "label": str(info.get("label", permission)),
            "risk": str(info.get("risk", "unknown")),
            "description": str(info.get("description", "")),
        })
    return result


def review_plugin_source(
    kind: str,
    manifest: Mapping[str, Any],
    source: str,
    expected_id: str,
) -> dict[str, Any]:
    if kind not in _PLUGIN_KINDS:
        raise AIPluginSourceError(
            "invalid_kind", f"kind 必须是 trigger 或 action，实际: {kind!r}"
        )

    if not isinstance(expected_id, str) or not is_valid_plugin_id(expected_id):
        raise AIPluginSourceError(
            "invalid_id", f"expected_id 不是合法插件 id: {expected_id!r}"
        )

    if not isinstance(manifest, dict):
        raise AIPluginSourceError("invalid_manifest", "manifest 必须是 JSON 对象")

    manifest_id = manifest.get("id")
    if manifest_id != expected_id:
        raise AIPluginSourceError(
            "id_mismatch",
            f"manifest.id 必须等于同意生成的插件草稿 id: "
            f"expected={expected_id!r}, manifest={manifest_id!r}",
        )

    unsupported = sorted(_UNSUPPORTED_MANIFEST_FIELDS & set(manifest.keys()))
    if unsupported:
        raise AIPluginSourceError(
            "unsupported_manifest",
            f"manifest 含超出简单生成插件范围的字段: {', '.join(unsupported)}",
        )

    valid, errors = validate_plugin_meta(dict(manifest), kind)
    if not valid:
        raise AIPluginSourceError(
            "invalid_manifest", "manifest 校验失败: " + "；".join(errors)
        )

    if not isinstance(source, str):
        raise AIPluginSourceError("invalid_source", "source 必须是字符串")

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as error:
        raise AIPluginSourceError(
            "invalid_source", f"source 不是有效的 Python 语法: {error}"
        ) from error

    functions = _top_level_functions(tree)
    _require_entrypoints(kind, manifest, functions)

    capabilities, uses_sudo, borrowed = analyze_plugin_source(source)
    source_name = "action.py" if kind == "action" else "trigger.py"
    scanner_findings = scan_plugin_source_security(source, source_name)
    if borrowed:
        scanner_findings.append({
            "id": "borrowed_privilege",
            "label": "借用其他插件能力",
            "level": "high",
            "detail": "源码引用了其他已加载插件: " + ", ".join(sorted(set(borrowed))),
            "file": source_name,
        })

    forbidden = sorted(_FORBIDDEN_CAPABILITIES & capabilities)
    if forbidden:
        raise AIPluginSourceError(
            "forbidden_capability", "source 触发禁止能力: " + ", ".join(forbidden)
        )
    if uses_sudo:
        raise AIPluginSourceError(
            "forbidden_import", "source 导入了 notmyfault.security.sudo"
        )

    return {
        "kind": kind,
        "id": manifest_id,
        "manifest": deepcopy(manifest),
        "source": source,
        "tests": _generated_test_source(kind),
        "compatibility_notes": _compatibility_notes(kind, manifest),
        "permissions_explanation": _permission_explanations(manifest),
        "check": {
            "ok": True,
            "schema": "ok",
            "entrypoint": "ok",
            "platform": current_platform_name(),
            "scanner_findings": scanner_findings,
            "revision_required": bool(scanner_findings),
        },
        "findings": {
            "capabilities": sorted(capabilities),
            "uses_sudo": uses_sudo,
            "borrowed": list(borrowed),
            "risks": scanner_findings,
        },
    }
