from __future__ import annotations

import ast
import hashlib
import json
import os
import posixpath
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from notmyfault.security.plugin_schema import (
    current_platform_name,
    scan_plugin_source_security,
    validate_plugin_meta,
)
from notmyfault.security.plugins import (
    analyze_plugin_source, plugin_integrity_problems, plugin_signature_kind_from_payload,
)
from notmyfault.security.security import SecurityMode
from notmyfault.security.signing import plugin_files, plugin_payload_from_entries

PluginKind = Literal["trigger", "action"]


def plugin_directories(root: str | Path):
    root = Path(root)
    if not root.is_dir():
        return
    for folder in sorted(root.iterdir()):
        name = folder.name
        if (
            folder.is_dir()
            and not name.startswith(".")
            and name not in {"__pycache__", "__pypackages__", "node_modules"}
            and not name.endswith(".nmf-backup")
        ):
            yield folder


def is_plugin_platform_compatible(meta: dict) -> bool:
    entrypoints = meta.get("entrypoints")
    platforms = meta.get("platforms")
    if isinstance(entrypoints, dict) and entrypoints:
        return current_platform_name() in entrypoints
    return not platforms or current_platform_name() in platforms


def resolve_plugin_entrypoint(folder_path: str, meta: dict, default_filename: str) -> str:
    entrypoints = meta.get("entrypoints") or {}
    relative_path = entrypoints.get(current_platform_name(), default_filename)
    path_api = posixpath if sys.platform.startswith("linux") else os.path
    plugin_root = path_api.realpath(folder_path)
    entrypoint = path_api.realpath(path_api.join(plugin_root, relative_path))
    if path_api.commonpath((plugin_root, entrypoint)) != plugin_root:
        raise ValueError(f"插件入口逃逸插件目录: {relative_path}")
    return entrypoint


@dataclass
class PluginTree:
    files: list[Path]
    file_snapshot: dict[str, str]
    py_sources: dict[str, bytes]
    payload: bytes
    legacy_payload: bytes
    contents: dict[str, bytes]


def inspect_plugin_tree(folder_path: str) -> PluginTree | None:
    try:
        files = plugin_files(folder_path)
        contents = {
            path.relative_to(folder_path).as_posix(): path.read_bytes()
            for path in files
        }
    except (OSError, ValueError):
        return None
    return PluginTree(
        files=files,
        file_snapshot={name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
        py_sources={str(Path(folder_path) / name): data for name, data in contents.items() if name.endswith(".py")},
        payload=plugin_payload_from_entries(contents.items()),
        legacy_payload=b"".join(contents.values()),
        contents=contents,
    )


@dataclass(frozen=True)
class PluginSignature:
    kind: str
    format: str = "v1"

    @property
    def source(self) -> str:
        return "local" if self.kind == "official-legacy" else self.kind


def inspect_signature(root: str | Path, origin: str, tree: PluginTree, installed_hashes=None) -> PluginSignature:
    kind = plugin_signature_kind_from_payload(str(root), origin, tree.payload)
    if kind == "none" and origin == "user" and installed_hashes == tree.file_snapshot:
        legacy_kind = plugin_signature_kind_from_payload(str(root), origin, tree.legacy_payload)
        if legacy_kind != "none":
            return PluginSignature(legacy_kind, "legacy")
    return PluginSignature(kind)


def validate_plugin_signature(root: Path, meta: dict, origin: str, mode: SecurityMode, signature_kind: str | None = None) -> str:
    from notmyfault.security.plugins import plugin_signature_kind

    kind = signature_kind if signature_kind is not None else plugin_signature_kind(str(root), origin)
    if mode != SecurityMode.STRICT:
        return kind
    if kind == "none":
        raise ValueError("签名无效")
    if "admin" in (meta.get("permissions") or []):
        if kind != "official":
            raise ValueError("声明了 'admin' 权限但未使用官方签名")
    elif origin == "user" and kind == "author":
        from notmyfault.security.signing import verify_author_key_counter_signature
        from notmyfault.security.signing_keys import get_public_keys

        if not verify_author_key_counter_signature(root, get_public_keys()):
            raise ValueError("作者公钥缺少有效的本地副签")
    return kind


@dataclass
class PluginInspection:
    root: Path
    kind: PluginKind
    origin: str
    tree: PluginTree | None
    meta: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    schema_errors: list[str] = field(default_factory=list)
    platform_compatible: bool = False
    entrypoint: str | None = None
    capability_problems: list[dict] = field(default_factory=list)
    capabilities: set[str] = field(default_factory=set)
    uses_sudo: bool = False
    borrowed: list[str] = field(default_factory=list)
    risks: list[dict] = field(default_factory=list)
    integrity_errors: list[str] = field(default_factory=list)
    signature: PluginSignature = field(default_factory=lambda: PluginSignature("none"))

    @property
    def entrypoint_exists(self) -> bool:
        return bool(self.tree and self.entrypoint in self.tree.py_sources)


def inspect_plugin_metadata(root: str | Path, kind: PluginKind, *, contents=None) -> tuple[dict, list[str], list[str]]:
    try:
        data = contents[f"{kind}.json"] if contents is not None else (Path(root) / f"{kind}.json").read_bytes()
        meta = json.loads(data.decode("utf-8"))
    except (OSError, KeyError, UnicodeError, ValueError) as error:
        return {}, [f"JSON 解析失败: {error}"], []
    _, schema_errors = validate_plugin_meta(meta, kind)
    return meta if isinstance(meta, dict) else {}, [], schema_errors


def inspect_plugin(root: str | Path, kind: PluginKind, origin: str = "user", *, tree: PluginTree | None = None, installed_manifest=None, capability_report=None) -> PluginInspection:
    root = Path(root).resolve()
    tree = tree or inspect_plugin_tree(str(root))
    result = PluginInspection(root, kind, origin, tree)
    if tree is None:
        result.errors.append("无法读取插件文件")
        return result
    meta, result.errors, result.schema_errors = inspect_plugin_metadata(root, kind, contents=tree.contents)
    result.meta = meta
    if result.errors or result.schema_errors:
        return result
    result.platform_compatible = is_plugin_platform_compatible(meta)
    try:
        result.entrypoint = resolve_plugin_entrypoint(str(root), meta, f"{kind}.py")
    except ValueError as error:
        result.errors.append(str(error))
    from notmyfault.platform.capabilities import is_capability_compatible
    from notmyfault.plugin_api import engines_compatibility

    _, result.capability_problems = is_capability_compatible(meta, capability_report)
    engines_ok, reason = engines_compatibility(meta)
    if not engines_ok:
        result.errors.append(reason)
    for filename, source in tree.py_sources.items():
        try:
            syntax = ast.parse(source, filename=filename)
        except (SyntaxError, ValueError):
            syntax = ast.Module(body=[], type_ignores=[])
        caps, sudo, borrowed = analyze_plugin_source(source, tree=syntax)
        result.capabilities.update(caps)
        result.uses_sudo |= sudo
        result.borrowed.extend(borrowed)
        relative = Path(filename).relative_to(root).as_posix()
        result.risks.extend(scan_plugin_source_security(source, relative, tree=syntax))
    installed_hashes = (installed_manifest or {}).get(meta.get("id"))
    if installed_hashes is not None:
        result.integrity_errors = plugin_integrity_problems(tree.file_snapshot, installed_hashes)
    result.signature = inspect_signature(root, origin, tree, installed_hashes)
    return result


def evaluate_plugin(result: PluginInspection, mode: SecurityMode, *, check_signature: bool = True) -> dict:
    errors = list(result.errors)
    warnings = []
    if result.schema_errors:
        errors.append("schema 校验失败: " + "; ".join(result.schema_errors))
    if errors:
        return {"allowed": False, "errors": errors, "warnings": warnings}
    meta = result.meta
    if not result.platform_compatible:
        errors.append("当前平台不支持")
    if not result.entrypoint_exists:
        errors.append("当前平台入口不存在")
    if result.capability_problems:
        errors.append("能力缺失: " + "；".join(f"{p['capability']}: {p['reason']}" for p in result.capability_problems))
    if result.origin == "builtin" and isinstance(meta.get("build"), dict):
        errors.append("内置插件不允许携带 build 编译钩子")
    forbidden = result.capabilities & {"self_elevation", "dynamic_exec"}
    if forbidden:
        errors.append("禁止能力: " + ", ".join(sorted(forbidden)))
    strict_findings = list(result.integrity_errors) if check_signature else []
    undeclared = result.capabilities - {"self_elevation", "dynamic_exec"} - set(meta.get("permissions") or [])
    if undeclared:
        strict_findings.append("未声明能力，使用了未在清单声明的能力: " + ", ".join(sorted(undeclared)))
    has_admin = "admin" in (meta.get("permissions") or [])
    if result.uses_sudo and not has_admin:
        strict_findings.append("import 了 notmyfault.security.sudo 但未在元数据中声明 'admin' 权限")
    elif has_admin and not result.uses_sudo:
        strict_findings.append("声明了 'admin' 权限但未通过 notmyfault.security.sudo 使用提权通道")
    if check_signature:
        try:
            validate_plugin_signature(result.root, meta, result.origin, mode, result.signature.kind)
        except ValueError as error:
            errors.append(str(error))
        if result.signature.kind == "none" and mode == SecurityMode.NORMAL:
            warnings.append("签名无效，降级加载")
    (errors if mode == SecurityMode.STRICT else warnings).extend(strict_findings)
    if result.borrowed:
        warnings.append("借壳提权嫌疑: " + "；".join(sorted(set(result.borrowed))))
    return {"allowed": not errors, "errors": errors, "warnings": warnings}


def inspection_report(result: PluginInspection, mode: SecurityMode) -> dict:
    return {
        "development": evaluate_plugin(result, SecurityMode.STRICT, check_signature=False),
        "load_policy": {"mode": mode.value, **evaluate_plugin(result, mode)},
        "signature": {
            "source": result.signature.source,
            "format": result.signature.format,
        },
    }
