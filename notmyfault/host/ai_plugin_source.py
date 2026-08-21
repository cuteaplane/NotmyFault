"""AI 生成插件源码的只读审查。

review_plugin_source 吃四样结构化输入：kind、清单、源码和用户同意生成的
插件草稿 id，产出可 JSON 化的审查产物。它只做校验和 AST 扫描，不写盘、不签名、
不导入、不执行也不重载任何东西。
"""

from __future__ import annotations

import ast
from copy import deepcopy
from typing import Any, Mapping

from notmyfault.security.plugin_schema import is_valid_plugin_id, validate_plugin_meta
from notmyfault.security.plugins import analyze_plugin_source

_PLUGIN_KINDS = frozenset({"trigger", "action"})

# 会引入构建钩子、备用入口、组件或贡献行为的清单字段。
_UNSUPPORTED_MANIFEST_FIELDS = frozenset(
    {"build", "entrypoints", "components", "contributes"}
)

# 自行提权（self_elevation）和动态执行（dynamic_exec）。
_FORBIDDEN_CAPABILITIES = frozenset({"self_elevation", "dynamic_exec"})


class AIPluginSourceError(ValueError):
    """AI 生成插件源码审查失败，code 稳定可断言。"""

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
    # vararg 时上限不封顶。
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

    # 触发器 event-v1 / event-v2 的 run 按 run(meta, config, emit_event, shutdown_event) 绑定。
    if manifest.get("trigger_api") in ("event-v1", "event-v2"):
        if not _accepts_positional(run, 4):
            raise AIPluginSourceError(
                "missing_entrypoint",
                f"{manifest['trigger_api']} 的 run() 必须接受 4 个位置参数",
            )


def review_plugin_source(
    kind: str,
    manifest: Mapping[str, Any],
    source: str,
    expected_id: str,
) -> dict[str, Any]:
    """校验并审查一份 AI 生成的插件源码，通过则返回只读审查产物。"""
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
        "findings": {
            "capabilities": sorted(capabilities),
            "uses_sudo": uses_sudo,
            "borrowed": list(borrowed),
        },
    }
