"""加载插件并完成元数据校验、安全检查、导入、注册和 setup，加载器只依赖调用方提供的运行时协作者且不持有 AutomationEngine。"""

import hashlib
import importlib.util
import inspect
import json
import os
import posixpath
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional, Tuple

from notmyfault.core.logging import engine_error, engine_info, engine_warn
from notmyfault.extensions.registry import ExtensionRegistry
from notmyfault.security.plugin_schema import (
    check_permissions_conform,
    is_known_permission,
    validate_plugin_meta,
)
from notmyfault.security.plugins import (
    analyze_plugin_source,
    check_sudo_import,
    plugin_signature_kind,
    plugin_signature_kind_from_payload,
    scan_borrowed_privilege,
    scan_plugin_capabilities,
    verify_plugin_integrity,
    verify_plugin_integrity_from_hashes,
    verify_plugin_sig,
)
from notmyfault.security.security import SecurityMode
from notmyfault.security.signing import plugin_files
from notmyfault.security import plugin_resources

# 旧调用仍通过这两个别名访问校验函数。
_validate_plugin_meta = validate_plugin_meta
_check_sudo_import = check_sudo_import

PluginKind = Literal["trigger", "action"]


def _version_info(meta: Dict[str, Any]) -> str:
    if meta.get("version_code") is not None:
        return f" v{meta['version_code']}"
    return ""


def _perm_info(meta: Dict[str, Any]) -> str:
    if meta.get("permissions"):
        return f" [权限: {', '.join(meta['permissions'])}]"
    return ""

_IGNORED_PLUGIN_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        "__pypackages__",
        "node_modules",
    }
)


def _is_ignored_plugin_directory(folder_name: str) -> bool:
    """识别插件根目录里由解释器或开发工具生成的目录"""
    return folder_name.startswith(".") or folder_name in _IGNORED_PLUGIN_DIRECTORY_NAMES


def _snapshot_plugin_files(folder_path: str) -> Dict[str, str] | None:
    snapshot: Dict[str, str] = {}
    try:
        for path in plugin_files(folder_path):
            relative = os.path.relpath(str(path), folder_path).replace(os.sep, "/")
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None
    return snapshot


@dataclass
class PluginTree:
    files: list[Path]
    file_snapshot: dict[str, str]
    py_sources: dict[str, str]
    payload: bytes


def inspect_plugin_tree(folder_path: str) -> Optional[PluginTree]:
    """一次读完整棵插件目录，签名校验和源码导入共用这一份数据。"""
    try:
        files = plugin_files(folder_path)
        snapshot: dict[str, str] = {}
        py_sources: dict[str, str] = {}
        parts: list[bytes] = []
        for path in files:
            data = path.read_bytes()
            relative = os.path.relpath(str(path), folder_path).replace(os.sep, "/")
            snapshot[relative] = hashlib.sha256(data).hexdigest()
            parts.append(data)
            if path.suffix == ".py":
                py_sources[str(path)] = data.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return PluginTree(
        files=files,
        file_snapshot=snapshot,
        py_sources=py_sources,
        payload=b"".join(parts),
    )


def _current_platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform


def is_plugin_platform_compatible(meta: Dict[str, Any]) -> bool:
    """清单缺少 platforms 时返回 True，表示所有平台。"""
    platforms = meta.get("platforms")
    entrypoints = meta.get("entrypoints")
    if entrypoints:
        return _current_platform_name() in entrypoints
    return not platforms or _current_platform_name() in platforms


def resolve_plugin_entrypoint(
    folder_path: str,
    meta: Dict[str, Any],
    default_filename: str,
) -> str:
    """入口路径归一化后仍须位于插件根目录。"""
    entrypoints = meta.get("entrypoints") or {}
    relative_path = entrypoints.get(_current_platform_name(), default_filename)
    path_api = posixpath if sys.platform.startswith("linux") else os.path
    plugin_root = path_api.realpath(folder_path)
    entrypoint = path_api.realpath(path_api.join(plugin_root, relative_path))
    if path_api.commonpath((plugin_root, entrypoint)) != plugin_root:
        raise ValueError(f"插件入口逃逸插件目录: {relative_path}")
    return entrypoint


class PluginRegistry:
    """保存插件元数据、入口函数和模块对象，旧 engine 属性仍引用其中的字典。"""

    def __init__(self) -> None:
        # 引擎上的同名属性仍指向这些字典。
        self.triggers_meta: Dict[str, Dict[str, Any]] = {}
        self.triggers_funcs: Dict[str, Callable[..., Any]] = {}
        self.actions_meta: Dict[str, Dict[str, Any]] = {}
        self.actions_funcs: Dict[str, Callable[..., Any]] = {}
        # 模块表按 plugin id 存储模块，trigger 和 action 不能使用同一个 id。
        self.modules: Dict[str, Any] = {}
        # 已发现但还没导入的动作插件，第一次执行时才导入。
        self.pending: Dict[str, Dict[str, Any]] = {}
        # 插件根目录，plugin_resource 靠它定位插件自带的二进制和资源。
        self.plugin_roots: Dict[str, str] = {}
        self.plugin_kinds: Dict[str, PluginKind] = {}
        # 插件声明的组件模块，按插件 id 再按组件 id 索引。
        self.components: Dict[str, Dict[str, Any]] = {}
        self.extensions = ExtensionRegistry()
        # 每个插件插进 sys.path 的目录，卸载时按这份清单移除。
        self.sys_path_entries: Dict[str, str] = {}
        self._action_materializer: Optional[Callable[[str], Any]] = None
        self._trigger_materializer: Optional[Callable[[str], Any]] = None

    def set_action_materializer(self, callback: Callable[[str], Any]) -> None:
        self._action_materializer = callback

    def set_trigger_materializer(self, callback: Callable[[str], Any]) -> None:
        self._trigger_materializer = callback

    def resolve_action(self, plugin_id: str) -> Optional[Callable[..., Any]]:
        """返回动作入口函数，还没导入的插件在这里触发首次导入。"""
        run = self.actions_funcs.get(plugin_id)
        if run is not None:
            return run
        materializer = self._action_materializer
        if materializer is None:
            return None
        materializer(plugin_id)
        return self.actions_funcs.get(plugin_id)

    def resolve_trigger(self, plugin_id: str) -> Optional[Callable[..., Any]]:
        """返回触发器入口函数，还没导入的插件在这里触发首次导入。"""
        run = self.triggers_funcs.get(plugin_id)
        if run is not None:
            return run
        materializer = self._trigger_materializer
        if materializer is None:
            return None
        materializer(plugin_id)
        return self.triggers_funcs.get(plugin_id)

    def stores(
        self, kind: PluginKind
    ) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Callable[..., Any]]]:
        if kind == "trigger":
            return self.triggers_meta, self.triggers_funcs
        return self.actions_meta, self.actions_funcs

    def get_module(self, plugin_id: str) -> Optional[Any]:
        return self.modules.get(plugin_id)

    def register(
        self,
        kind: PluginKind,
        plugin_id: str,
        meta: Dict[str, Any],
        run: Callable[..., Any],
        module: Any,
    ) -> None:
        # 注册项必须同时保存元数据、入口函数和模块对象。
        meta_store, func_store = self.stores(kind)
        func_store[plugin_id] = run
        meta_store[plugin_id] = meta
        self.modules[plugin_id] = module
        self.plugin_kinds[plugin_id] = kind

    def unregister(self, kind: PluginKind, plugin_id: str) -> None:
        # 回滚和卸载都从三个表中删除同一 plugin id。
        meta_store, func_store = self.stores(kind)
        func_store.pop(plugin_id, None)
        meta_store.pop(plugin_id, None)
        module = self.modules.pop(plugin_id, None)
        if module is not None:
            sys.modules.pop(getattr(module, "__name__", ""), None)
        self.pending.pop(plugin_id, None)
        self.plugin_roots.pop(plugin_id, None)
        self.plugin_kinds.pop(plugin_id, None)
        components = self.components.pop(plugin_id, None)
        if components:
            for component in components.values():
                sys.modules.pop(getattr(component, "__name__", ""), None)
        self.extensions.unregister_plugin(plugin_id)
        self._cleanup_plugin_path(plugin_id)

    def clear(self) -> None:
        """清理本代引擎加载的插件模块和导入路径。"""
        for plugin_id in tuple(self.sys_path_entries):
            self._cleanup_plugin_path(plugin_id)
        for module in tuple(self.modules.values()):
            sys.modules.pop(getattr(module, "__name__", ""), None)
        for components in tuple(self.components.values()):
            for component in components.values():
                sys.modules.pop(getattr(component, "__name__", ""), None)
        self.triggers_meta.clear()
        self.triggers_funcs.clear()
        self.actions_meta.clear()
        self.actions_funcs.clear()
        self.modules.clear()
        self.pending.clear()
        self.plugin_roots.clear()
        self.plugin_kinds.clear()
        self.components.clear()
        self.extensions.clear()

    def _cleanup_plugin_path(self, plugin_id: str) -> None:
        """移除插件插进 sys.path 的目录和从该目录导入的模块"""
        root = self.sys_path_entries.pop(plugin_id, None)
        if root is None:
            return
        try:
            sys.path.remove(root)
        except ValueError:
            pass
        for name, mod in list(sys.modules.items()):
            if name.startswith("notmyfault."):
                continue
            mod_file = getattr(mod, "__file__", None)
            if not mod_file:
                continue
            try:
                inside = os.path.commonpath((os.path.realpath(mod_file), root)) == root
            except ValueError:
                inside = False
            if inside:
                sys.modules.pop(name, None)


class PluginLoader:
    """触发器和动作共用的加载器，按发现、校验、导入、注册和 setup 的顺序处理插件。"""

    def __init__(
        self,
        registry: PluginRegistry,
        config: Dict[str, Any],
        diagnostics: Any,
        security_mode: SecurityMode,
        sudo: Any,
        engine_token: str,
        integrity_errors: list[str],
    ) -> None:
        # 加载器只保存 registry、config 和 diagnostics 等传入对象。
        self._registry = registry
        self._config = config
        self._diagnostics = diagnostics
        self._security_mode = security_mode
        self._sudo = sudo
        self._engine_token = engine_token
        self._integrity_errors = integrity_errors
        # 多个工作流线程可能同时首次执行同一个懒加载动作。
        self._materialize_lock = threading.Lock()
        registry.set_action_materializer(self.materialize_pending_action)
        registry.set_trigger_materializer(self.materialize_pending_trigger)
        # plugin_resource 需要注册表里的插件根目录才能定位插件自带资源。
        plugin_resources.set_registry(registry)

    def _restore_override(
        self,
        plugin_type: PluginKind,
        plugin_id: str,
        prev: Optional[Tuple[Any, Any, Any]],
        func_store: Dict[str, Any],
        meta_store: Dict[str, Dict[str, Any]],
    ) -> None:
        """覆盖加载失败时恢复旧插件，旧模块尚未执行 teardown 时可直接重新注册。"""
        if not prev:
            return
        old_module, old_meta, old_run = prev
        if old_module is None or old_meta is None or old_run is None:
            return
        self._registry.register(plugin_type, plugin_id, old_meta, old_run, old_module)
        # 独立存储字典的调用也需要同步写入。
        func_store[plugin_id] = old_run
        meta_store[plugin_id] = old_meta

    def load(
        self,
        base_dir: str,
        plugins_dir: str,
        json_filename: str,
        py_filename: str,
        module_prefix: str,
        meta_store: Dict[str, Dict[str, Any]],
        func_store: Dict[str, Any],
        store_name: str,
        origin: str = "builtin",
    ) -> Tuple[int, int]:
        """加载插件目录并返回成功数和失败数。"""
        plugin_type: PluginKind = "trigger" if store_name == "Trigger" else "action"
        root_dir = os.path.join(base_dir, plugins_dir)
        loaded_count = 0
        failed_count = 0
        # 成功名单攒到最后一条输出，避免每个插件 print 一次拖慢启动。
        loaded_summaries: list[str] = []

        if not os.path.isdir(root_dir):
            print(f"[Engine] 插件目录不存在，跳过: {root_dir}", file=sys.stderr)
            return 0, 0

        for folder_name in sorted(os.listdir(root_dir)):
            if _is_ignored_plugin_directory(folder_name):
                continue
            folder_path = os.path.join(root_dir, folder_name)
            if not os.path.isdir(folder_path):
                continue

            json_file = os.path.join(folder_path, json_filename)
            py_file = os.path.join(folder_path, py_filename)

            # 没有元数据的目录可能只是文档或缓存。
            if not os.path.exists(json_file):
                print(
                    f"[Engine] 插件目录缺少 {json_filename}，跳过: {folder_path}",
                    file=sys.stderr,
                )
                continue
            try:
                with open(json_file, "r", encoding="utf-8") as fp:
                    meta = json.load(fp)
            except json.JSONDecodeError as e:
                print(
                    f"[Engine] 插件 JSON 解析失败 ({json_file}): {e}",
                    file=sys.stderr,
                )
                failed_count += 1
                self._diagnostics.record_plugin_error(
                    store_name, folder_name, f"JSON 解析失败: {e}"
                )
                engine_error("plugin_load_failed", plugin=folder_name, type=store_name, reason=f"JSON 解析失败: {e}")
                continue
            except OSError as e:
                print(
                    f"[Engine] 无法读取插件元数据 ({json_file}): {e}",
                    file=sys.stderr,
                )
                failed_count += 1
                self._diagnostics.record_plugin_error(
                    store_name, folder_name, f"读取文件失败: {e}"
                )
                engine_error("plugin_load_failed", plugin=folder_name, type=store_name, reason=f"读取文件失败: {e}")
                continue

            is_valid, errors = validate_plugin_meta(meta, plugin_type)
            # meta 可能是 JSON 数组等非 dict 类型，报错信息里退回目录名
            plugin_id = meta.get("id", folder_name) if isinstance(meta, dict) else folder_name
            if not is_valid:
                print(
                    f"[Engine] 插件 \"{plugin_id}\" schema 校验失败 ({json_file}):",
                    file=sys.stderr,
                )
                for err in errors:
                    print(f"         - {err}", file=sys.stderr)
                failed_count += 1
                self._diagnostics.record_plugin_error(
                    store_name, plugin_id, f"schema 校验失败: {'; '.join(errors[:3])}"
                )
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=f"schema 校验失败: {'; '.join(errors[:3])}")
                continue
            if not meta["enabled"]:
                print(
                    f"[Engine] 插件 \"{plugin_id}\" ({meta['name']}) 已禁用，跳过"
                )
                continue

            if not is_plugin_platform_compatible(meta):
                supported = ", ".join(
                    meta.get("platforms") or (meta.get("entrypoints") or {}).keys()
                )
                print(
                    f'[Engine] 插件 "{plugin_id}" ({meta["name"]}) 不支持当前平台 '
                    f'{_current_platform_name()}（支持: {supported}），跳过'
                )
                engine_info(
                    f"plugin_platform_skipped: {plugin_id} "
                    f"current={_current_platform_name()} supported={supported}"
                )
                continue

            # build 钩子是作者现场编译的直通通道，内置插件走构建流程不需要它
            if origin == "builtin" and isinstance(meta.get("build"), dict):
                reason = "内置插件不允许携带 build 编译钩子"
                print(
                    f'[Engine] [!!] 插件 "{plugin_id}" {reason}，跳过',
                    file=sys.stderr,
                )
                failed_count += 1
                self._diagnostics.record_plugin_error(store_name, plugin_id, reason)
                engine_error(
                    "plugin_load_failed",
                    plugin=plugin_id,
                    type=store_name,
                    reason=reason,
                )
                continue

            try:
                py_file = resolve_plugin_entrypoint(
                    folder_path,
                    meta,
                    py_filename,
                )
            except ValueError as error:
                failed_count += 1
                self._diagnostics.record_plugin_error(
                    store_name, plugin_id, str(error)
                )
                engine_error(
                    "plugin_load_failed",
                    plugin=plugin_id,
                    type=store_name,
                    reason=str(error),
                )
                continue
            if not os.path.isfile(py_file):
                relative_entry = os.path.relpath(py_file, folder_path)
                if not meta.get("entrypoints"):
                    print(
                        f"[Engine] 插件目录缺少 {py_filename}，跳过: {folder_path}",
                        file=sys.stderr,
                    )
                    continue
                reason = f"当前平台入口不存在: {relative_entry}"
                print(
                    f'[Engine] 插件 "{plugin_id}" {reason}，跳过',
                    file=sys.stderr,
                )
                failed_count += 1
                self._diagnostics.record_plugin_error(
                    store_name, plugin_id, reason
                )
                engine_error(
                    "plugin_load_failed",
                    plugin=plugin_id,
                    type=store_name,
                    reason=reason,
                )
                continue

            # 禁用名单写在 config 里，对内置和用户插件都生效，json 里的 enabled 只表示作者出厂状态。
            disabled_cfg = self._config.get("disabled_plugins", {})
            ptype_key = "triggers" if store_name == "Trigger" else "actions"
            disabled_list = disabled_cfg.get(ptype_key, []) if isinstance(disabled_cfg, dict) else []
            if plugin_id in disabled_list:
                print(
                    f'[Engine] 插件 "{plugin_id}" ({meta["name"]}) 已被用户禁用，跳过'
                )
                continue

            # 单次遍历：读文件、算哈希、拼签名 payload、留 py 源码，后面的检查共用这份结果。
            tree = inspect_plugin_tree(folder_path)
            if tree is None:
                reason = "无法读取插件文件，拒绝加载"
                failed_count += 1
                self._diagnostics.record_plugin_error(store_name, plugin_id, reason)
                engine_error(
                    "plugin_load_failed",
                    plugin=plugin_id,
                    type=store_name,
                    reason=reason,
                )
                continue
            file_snapshot = tree.file_snapshot

            # strict 模式在导入前检查签名，其他模式保留签名失败时的降级加载。
            signature_kind = plugin_signature_kind_from_payload(
                folder_path, origin, tree.payload
            )
            if (
                self._security_mode == SecurityMode.STRICT
                and origin == "user"
                and signature_kind == "author"
                and "admin" not in (meta.get("permissions") or [])
            ):
                from notmyfault.security.signing import (
                    verify_author_key_counter_signature,
                )
                from notmyfault.security.signing_keys import get_public_keys

                if not verify_author_key_counter_signature(
                    Path(folder_path), get_public_keys()
                ):
                    signature_kind = "none"
            signature_ok = signature_kind != "none"
            if not signature_ok:
                if self._security_mode == SecurityMode.STRICT:
                    reason = "签名无效"
                    print(
                        f"[Engine] [!!] {store_name} \"{plugin_id}\" {reason}，不加载",
                        file=sys.stderr,
                    )
                    failed_count += 1
                    self._diagnostics.record_plugin_error(store_name, plugin_id, reason)
                    engine_error(
                        "plugin_load_failed",
                        plugin=plugin_id,
                        type=store_name,
                        reason=reason,
                    )
                    continue
                if self._security_mode == SecurityMode.NORMAL:
                    print(
                        f"[Engine] [!!] {store_name} \"{plugin_id}\" 签名无效，降级加载",
                        file=sys.stderr,
                    )

            # permissive 和 normal 接受未知权限，strict 在导入前拒绝。
            if self._security_mode == SecurityMode.STRICT:
                perms = meta.get("permissions") or []
                perm_conform, _ = check_permissions_conform(perms)
                if not perm_conform:
                    unknown = [p for p in perms if not is_known_permission(p)]
                    reason = f"包含未知权限: {', '.join(unknown)}"
                    print(
                        f"[Engine] [!!] {store_name} \"{plugin_id}\" {reason}，"
                        "strict 模式不加载",
                        file=sys.stderr,
                    )
                    failed_count += 1
                    self._diagnostics.record_plugin_error(store_name, plugin_id, reason)
                    engine_error(
                        "plugin_load_failed",
                        plugin=plugin_id,
                        type=store_name,
                        reason=reason,
                    )
                    continue

            # 每个 .py 只 parse 一次；内置插件不做借壳扫描。
            caps: set[str] = set()
            uses_sudo = False
            borrowed_findings: list[str] = []
            include_borrowed = origin != "builtin"
            for _py_path, source in tree.py_sources.items():
                file_caps, file_uses_sudo, file_borrowed = analyze_plugin_source(
                    source, include_borrowed=include_borrowed
                )
                caps |= file_caps
                uses_sudo = uses_sudo or file_uses_sudo
                if include_borrowed:
                    borrowed_findings.extend(file_borrowed)

            # self_elevation 始终拒绝，插件只能通过 sudo.run_as_admin 提权并声明 admin。
            if "self_elevation" in caps:
                cap_msg = "self_elevation（自行提权：必须改走 notmyfault.security.sudo.run_as_admin 并声明 admin）"
                print(f'[Engine] [安全] 插件 "{plugin_id}" 触发禁止能力: {cap_msg}', file=sys.stderr)
                engine_warn(f"forbidden_capability: {plugin_id} {cap_msg}")
                failed_count += 1
                self._diagnostics.record_plugin_error(store_name, plugin_id, f"禁止能力: {cap_msg}")
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=f"禁止能力: {cap_msg}")
                continue

            # dynamic_exec 始终拒绝，因为它可绕过 AST 能力扫描。
            if "dynamic_exec" in caps:
                cap_msg = "dynamic_exec（exec/eval/compile/__import__ 动态执行：可绕过所有能力检测）"
                print(f'[Engine] [安全] 插件 "{plugin_id}" 触发禁止能力: {cap_msg}', file=sys.stderr)
                engine_warn(f"forbidden_capability: {plugin_id} {cap_msg}")
                failed_count += 1
                self._diagnostics.record_plugin_error(store_name, plugin_id, f"禁止能力: {cap_msg}")
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=f"禁止能力: {cap_msg}")
                continue

            declared_perms = set(meta.get("permissions") or [])
            # self_elevation 和 dynamic_exec 不参与清单权限差集。
            undeclared = (caps - {"self_elevation", "dynamic_exec"}) - declared_perms
            if undeclared:
                cap_msg = ", ".join(sorted(undeclared))
                print(f'[Engine] [安全] 插件 "{plugin_id}" 使用了未在清单声明的能力: {cap_msg}', file=sys.stderr)
                engine_warn(f"undeclared_capability: {plugin_id} {cap_msg}")
                if self._security_mode == SecurityMode.STRICT:
                    failed_count += 1
                    self._diagnostics.record_plugin_error(store_name, plugin_id, f"未声明能力: {cap_msg}")
                    engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=f"未声明能力: {cap_msg}")
                    continue

            # strict 模式要求导入 sudo 与声明 admin 同时出现，缺一项即拒绝。
            has_admin = "admin" in (meta.get("permissions") or [])
            # admin 能走 sudo 提权，作者自签的公钥谁都能造，strict 只认官方签名。
            if (
                self._security_mode == SecurityMode.STRICT
                and has_admin
                and signature_kind not in ("official", "official-legacy")
            ):
                reason = "声明了 'admin' 权限但未使用官方签名"
                print(
                    f'[Engine] [!!] 插件 "{plugin_id}" {reason}，'
                    "strict 模式不加载",
                    file=sys.stderr,
                )
                failed_count += 1
                self._diagnostics.record_plugin_error(store_name, plugin_id, reason)
                engine_error(
                    "plugin_load_failed",
                    plugin=plugin_id,
                    type=store_name,
                    reason=reason,
                )
                continue
            if uses_sudo and not has_admin:
                reason = "import 了 notmyfault.security.sudo 但未在元数据中声明 'admin' 权限"
                print(
                    f'[Engine] [!!] 插件 "{plugin_id}" {reason}',
                    file=sys.stderr,
                )
                if self._security_mode == SecurityMode.STRICT:
                    failed_count += 1
                    self._diagnostics.record_plugin_error(store_name, plugin_id, reason)
                    engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=reason)
                    continue
            elif has_admin and not uses_sudo:
                reason = "声明了 'admin' 权限但未通过 notmyfault.security.sudo 使用提权通道"
                print(
                    f'[Engine] [!!] 插件 "{plugin_id}" {reason}',
                    file=sys.stderr,
                )
                if self._security_mode == SecurityMode.STRICT:
                    failed_count += 1
                    self._diagnostics.record_plugin_error(store_name, plugin_id, reason)
                    engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=reason)
                    continue

            # 内置插件使用构建时 Ed25519 签名，用户插件记录首次文件哈希，完整性失败只告警。
            if origin != "builtin":
                integrity_ok, integrity_msg = verify_plugin_integrity_from_hashes(
                    plugin_id, file_snapshot
                )
                if not integrity_ok:
                    warning = (
                        f"[Engine] [安全] 插件 \"{plugin_id}\" 完整性校验失败："
                        + integrity_msg
                    )
                    print(warning, file=sys.stderr)
                    engine_warn(f"integrity_check: {integrity_msg}")
                    self._integrity_errors.append(warning)

                if borrowed_findings:
                    warning = (
                        f"[Engine] [安全] 插件 \"{plugin_id}\" 存在借壳提权嫌疑: "
                        + "；".join(sorted(set(borrowed_findings))[:3])
                    )
                    print(warning, file=sys.stderr)
                    engine_warn(f"borrowed_privilege: {plugin_id} {'; '.join(borrowed_findings)}")
                    self._integrity_errors.append(warning)

            # 发现阶段到此为止，元数据先入账，模块导入推迟到物化阶段。
            existing_kind = self._registry.plugin_kinds.get(plugin_id)
            if existing_kind is not None and existing_kind != plugin_type:
                reason = (
                    f"插件 id 已被{'触发器' if existing_kind == 'trigger' else '动作'}使用"
                )
                failed_count += 1
                self._diagnostics.record_plugin_error(store_name, plugin_id, reason)
                engine_error(
                    "plugin_load_failed",
                    plugin=plugin_id,
                    type=store_name,
                    reason=reason,
                )
                continue

            previous_root = self._registry.plugin_roots.get(plugin_id)
            previous_path = self._registry.sys_path_entries.get(plugin_id)
            meta_with_origin = {
                **meta,
                "origin": origin,
                "signature_kind": signature_kind,
            }
            self._registry.plugin_roots[plugin_id] = folder_path
            self._registry.plugin_kinds[plugin_id] = plugin_type
            self._registry.extensions.register_manifest(
                plugin_id, plugin_type, meta_with_origin, folder_path
            )
            entry = {
                "kind": plugin_type,
                "plugin_id": plugin_id,
                "folder_path": folder_path,
                "entry_path": py_file,
                "meta": meta_with_origin,
                "origin": origin,
                "module_prefix": module_prefix,
                "store_name": store_name,
                "func_store": func_store,
                "meta_store": meta_store,
                "prev": None,
                "previous_root": previous_root,
                "previous_path": previous_path,
                "file_snapshot": file_snapshot,
                "signature_kind": signature_kind,
            }

            if plugin_type == "action":
                old_module = self._registry.get_module(plugin_id)
                if old_module is not None:
                    # 覆盖已加载的旧动作时立即导入，meta 和函数保持同一代
                    entry["prev"] = (
                        old_module,
                        meta_store.get(plugin_id),
                        func_store.get(plugin_id),
                    )
                    if self.materialize_entry(entry) is None:
                        failed_count += 1
                        continue
                else:
                    # 动作第一次被执行时才导入，先写 meta 让规则校验认识它
                    self._registry.pending[plugin_id] = entry
                    meta_store[plugin_id] = meta_with_origin
                loaded_count += 1
                loaded_summaries.append(plugin_id)
                continue

            # 触发器第一次被规则引用时才导入，先写 meta 让规则校验认识它
            old_module = self._registry.get_module(plugin_id)
            if old_module is not None:
                # 覆盖已加载的旧触发器时立即导入，meta 和函数保持同一代
                entry["prev"] = (
                    old_module,
                    meta_store.get(plugin_id),
                    func_store.get(plugin_id),
                )
                if self.materialize_entry(entry, announce=False) is None:
                    failed_count += 1
                    continue
            else:
                self._registry.pending[plugin_id] = entry
                meta_store[plugin_id] = meta_with_origin
            loaded_count += 1
            loaded_summaries.append(plugin_id)

        if loaded_summaries:
            print(
                f"[Engine] 装载{store_name} {loaded_count} 个: "
                + ", ".join(loaded_summaries)
            )
        return loaded_count, failed_count

    def materialize_pending_action(self, plugin_id: str) -> Optional[Any]:
        """导入还没加载的动作插件，没有对应待物化条目时返回 None"""
        entry = self._registry.pending.get(plugin_id)
        if entry is None:
            return None
        return self.materialize_entry(entry)

    def materialize_pending_trigger(self, plugin_id: str) -> Optional[Any]:
        """导入还没加载的触发器插件，没有对应待物化条目时返回 None"""
        entry = self._registry.pending.get(plugin_id)
        if entry is None:
            return None
        return self.materialize_entry(entry)

    def materialize_entry(
        self, entry: Dict[str, Any], *, announce: bool = True
    ) -> Optional[Any]:
        """导入插件模块并完成注册、setup 和提权授权，失败返回 None"""
        plugin_type = entry["kind"]
        plugin_id = entry["plugin_id"]
        folder_path = entry["folder_path"]
        py_file = entry["entry_path"]
        module_prefix = entry["module_prefix"]
        store_name = entry["store_name"]
        meta = entry["meta"]
        origin = entry["origin"]
        func_store = entry["func_store"]
        meta_store = entry["meta_store"]
        prev = entry.get("prev")
        previous_root = entry.get("previous_root")
        previous_path = entry.get("previous_path")

        with self._materialize_lock:
            # 覆盖场景带着 prev 进来，必须先走完替换，缓存短路只对首次物化生效
            if prev is None and plugin_id in func_store:
                return func_store[plugin_id]

            if prev is None and self._registry.get_module(plugin_id) is not None:
                old_origin = meta_store.get(plugin_id, {}).get("origin", "builtin")
                engine_info(f"{store_name} \"{plugin_id}\": {old_origin} -> {origin} override")
                prev = (
                    self._registry.get_module(plugin_id),
                    meta_store.get(plugin_id),
                    func_store.get(plugin_id),
                )
            if prev is not None:
                self._registry.unregister(plugin_type, plugin_id)
                # 直接调用 _load_plugins() 时也要清理传入的存储字典。
                func_store.pop(plugin_id, None)
                meta_store.pop(plugin_id, None)

            self._registry.plugin_roots[plugin_id] = folder_path
            self._registry.plugin_kinds[plugin_id] = plugin_type
            self._registry.extensions.register_manifest(
                plugin_id, plugin_type, meta, folder_path
            )

            # 插件目录放进 sys.path，入口文件才能 import 到兄弟模块
            root = os.path.realpath(folder_path)
            if root not in sys.path:
                sys.path.insert(0, root)
            self._registry.sys_path_entries[plugin_id] = root

            module_name = f"{module_prefix}{plugin_id}"

            def fail(reason: str) -> None:
                sys.modules.pop(module_name, None)
                self._registry.extensions.unregister_plugin(plugin_id)
                self._registry._cleanup_plugin_path(plugin_id)
                self._registry.plugin_roots.pop(plugin_id, None)
                self._registry.plugin_kinds.pop(plugin_id, None)
                if prev is not None:
                    self._restore_override(
                        plugin_type, plugin_id, prev, func_store, meta_store
                    )
                    if previous_root is not None:
                        self._registry.plugin_roots[plugin_id] = previous_root
                    if previous_path is not None:
                        if previous_path not in sys.path:
                            sys.path.insert(0, previous_path)
                        self._registry.sys_path_entries[plugin_id] = previous_path
                    old_module, old_meta, _old_run = prev
                    if old_meta is not None and previous_root is not None:
                        self._registry.extensions.register_manifest(
                            plugin_id, plugin_type, old_meta, previous_root
                        )
                        old_path = getattr(old_module, "__file__", None)
                        if isinstance(old_path, str):
                            self._load_extensions(
                                plugin_id,
                                previous_root,
                                old_meta,
                                module_prefix,
                                old_module,
                                old_path,
                            )
                else:
                    self._registry.pending.pop(plugin_id, None)
                    func_store.pop(plugin_id, None)
                    meta_store.pop(plugin_id, None)
                self._diagnostics.record_plugin_error(store_name, plugin_id, reason)
                engine_error(
                    "plugin_load_failed",
                    plugin=plugin_id,
                    type=store_name,
                    reason=reason,
                )

            expected_signature = entry.get("signature_kind", "none")
            # 物化前再读一轮目录，签名和快照共用这一次结果。
            tree = inspect_plugin_tree(folder_path)
            if tree is None:
                fail("无法读取插件文件，拒绝导入")
                return None
            if (
                plugin_signature_kind_from_payload(folder_path, origin, tree.payload)
                != expected_signature
            ):
                fail("插件签名在物化前发生变化，拒绝导入")
                return None
            if tree.file_snapshot != entry.get("file_snapshot"):
                fail("插件文件在校验后发生变化，拒绝导入")
                return None

            sys.modules.pop(module_name, None)
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                print(
                    f"[Engine] 无法创建模块规格，跳过: {py_file}",
                    file=sys.stderr,
                )
                fail("无法创建模块规格")
                return None

            try:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            except Exception:
                print(
                    f"[Engine] 插件 \"{plugin_id}\" Python 加载失败 ({py_file}):",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                fail(f"Python 加载异常: {traceback.format_exc()[-200:]}")
                return None

            if not hasattr(module, "run"):
                print(
                    f"[Engine] 插件 \"{plugin_id}\" ({meta['name']}) 缺少 run() 函数，跳过",
                    file=sys.stderr,
                )
                fail("缺少 run() 函数")
                return None

            # event-v1 接收配置列表，event-v2 为每条规则使用独立配置，启动线程前先验证入口。
            if plugin_type == "trigger" and meta.get("trigger_api") in ("event-v1", "event-v2"):
                try:
                    config = [] if meta.get("trigger_api") == "event-v1" else {}
                    emit = lambda *_args: None
                    inspect.signature(module.run).bind({}, config, emit, threading.Event())
                except (TypeError, ValueError) as exc:
                    message = f"{meta['trigger_api']} 入口不兼容: {exc}"
                    print(f'[Engine] 触发器 "{plugin_id}" {message}', file=sys.stderr)
                    fail(message)
                    return None

            self._registry.register(
                plugin_type, plugin_id, meta, getattr(module, "run"), module
            )
            # 显式写入维持 _load_plugins() 返回字典的历史行为。
            func_store[plugin_id] = getattr(module, "run")
            meta_store[plugin_id] = meta
            self._registry.pending.pop(plugin_id, None)

            # setup() 返回失败时回滚注册表，覆盖加载还会恢复旧插件。
            if plugin_type == "trigger" and hasattr(module, "setup"):
                try:
                    result = module.setup(meta)
                    if result is False:
                        print(
                            f"[Engine] 触发器 \"{plugin_id}\" setup() 返回 False，"
                            f"卸载该插件",
                            file=sys.stderr,
                        )
                        self._registry.unregister(plugin_type, plugin_id)
                        func_store.pop(plugin_id, None)
                        meta_store.pop(plugin_id, None)
                        fail("setup() 返回 False")
                        return None
                except Exception:
                    print(
                        f"[Engine] 触发器 \"{plugin_id}\" setup() 执行异常:",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                    self._registry.unregister(plugin_type, plugin_id)
                    func_store.pop(plugin_id, None)
                    meta_store.pop(plugin_id, None)
                    fail("setup() 执行异常")
                    return None

            # 新插件 setup() 成功后才调用旧插件 teardown()。
            previous_module = prev[0] if prev is not None else None
            if previous_module is not None and hasattr(previous_module, "teardown"):
                try:
                    previous_module.teardown()
                except Exception:
                    engine_warn(f"override teardown \"{plugin_id}\" 异常: {traceback.format_exc()[-200:]}")

            # teardown 走完再撤销旧模块授权，随后按新模块声明授权。
            if prev is not None:
                self._sudo.deauthorize_plugin(plugin_id, self._engine_token)
            if "admin" in (meta.get("permissions") or []):
                try:
                    self._sudo.authorize_plugin(
                        plugin_id, self._engine_token, module=module
                    )
                    print(f"[Engine] [安全] 插件 \"{plugin_id}\" 已注册管理员权限")
                except PermissionError as e:
                    print(f"[Engine] [!!] 插件 \"{plugin_id}\" 管理员权限注册失败: {e}", file=sys.stderr)
                    engine_error("admin_registration_failed", plugin=plugin_id, error=str(e))

            # 组件是附加协议：只有声明了 components 的插件才进入组件加载。
            if isinstance(meta.get("components"), list) and meta["components"]:
                self._load_components(
                    plugin_id, folder_path, meta, module_prefix
                )
            if isinstance(meta.get("contributes"), dict):
                self._load_extensions(
                    plugin_id, folder_path, meta, module_prefix, module, py_file
                )

            if announce:
                print(
                    f"[Engine] 装载{store_name}: {meta['name']} ({plugin_id})"
                    f"{_version_info(meta)}{_perm_info(meta)}"
                )
            return module

    def _load_components(
        self,
        plugin_id: str,
        folder_path: str,
        meta: Dict[str, Any],
        module_prefix: str,
    ) -> Optional[Any]:
        """导入插件声明的组件，入口缺 invoke 或导入失败时不注册"""
        components = meta.get("components")
        if not isinstance(components, list) or not components:
            return None
        root = os.path.realpath(folder_path)
        loaded: Dict[str, Any] = {}
        for component in components:
            if not isinstance(component, dict):
                continue
            component_id = component.get("id")
            if not isinstance(component_id, str):
                continue
            entrypoint = str(component.get("entrypoint", "component.py"))
            component_path = os.path.realpath(os.path.join(root, entrypoint))
            try:
                inside = os.path.commonpath((component_path, root)) == root
            except ValueError:
                inside = False
            if not inside or not os.path.isfile(component_path):
                self._diagnostics.record_plugin_error(
                    "plugin", plugin_id, f"组件 {component_id} 入口缺失: {entrypoint}"
                )
                continue

            module_name = f"{module_prefix}{plugin_id}__component_{component_id}"
            sys.modules.pop(module_name, None)
            spec = importlib.util.spec_from_file_location(
                module_name, component_path
            )
            if spec is None or spec.loader is None:
                self._diagnostics.record_plugin_error(
                    "plugin", plugin_id, f"组件 {component_id} 无法创建模块规格"
                )
                continue
            try:
                component_module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = component_module
                spec.loader.exec_module(component_module)
            except Exception:
                print(
                    f"[Engine] 插件 \"{plugin_id}\" 组件 {component_id} "
                    f"导入异常 ({component_path}):",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                self._diagnostics.record_plugin_error(
                    "plugin", plugin_id, f"组件 {component_id} 导入异常"
                )
                continue

            if not hasattr(component_module, "invoke"):
                self._diagnostics.record_plugin_error(
                    "plugin", plugin_id, f"组件 {component_id} 缺少 invoke 函数"
                )
                continue
            loaded[component_id] = component_module
        if loaded:
            self._registry.components[plugin_id] = loaded
        return loaded or None

    def _load_extensions(
        self,
        plugin_id: str,
        folder_path: str,
        meta: Dict[str, Any],
        module_prefix: str,
        primary_module: Any,
        primary_path: str,
    ) -> Optional[Any]:
        """导入命令处理函数，同一个 Python 文件只创建一个模块。"""
        contributes = meta.get("contributes")
        if not isinstance(contributes, dict):
            return None
        commands = contributes.get("commands")
        if not isinstance(commands, list) or not commands:
            return None
        root = os.path.realpath(folder_path)
        primary_path = os.path.realpath(primary_path)
        loaded_modules: Dict[str, Any] = {primary_path: primary_module}
        registered = 0
        for command in commands:
            if not isinstance(command, dict):
                continue
            command_id = command.get("id")
            handler_ref = command.get("handler")
            if not isinstance(command_id, str) or not isinstance(handler_ref, str):
                continue
            entrypoint, symbol = handler_ref.rsplit(":", 1)
            command_path = os.path.realpath(os.path.join(root, entrypoint))
            try:
                inside = os.path.commonpath((command_path, root)) == root
            except ValueError:
                inside = False
            if not inside or not os.path.isfile(command_path):
                self._diagnostics.record_plugin_error(
                    "plugin", plugin_id, f"扩展命令 {command_id} 入口缺失: {entrypoint}"
                )
                continue

            command_module = loaded_modules.get(command_path)
            if command_module is None:
                module_name = (
                    f"{module_prefix}{plugin_id}__extension_"
                    f"{len(loaded_modules)}"
                )
                sys.modules.pop(module_name, None)
                spec = importlib.util.spec_from_file_location(module_name, command_path)
                if spec is None or spec.loader is None:
                    self._diagnostics.record_plugin_error(
                        "plugin", plugin_id, f"扩展命令 {command_id} 无法创建模块规格"
                    )
                    continue
                try:
                    command_module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = command_module
                    spec.loader.exec_module(command_module)
                except Exception:
                    print(
                        f"[Engine] 插件 \"{plugin_id}\" 扩展命令 {command_id} "
                        f"导入异常 ({command_path}):",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                    self._diagnostics.record_plugin_error(
                        "plugin", plugin_id, f"扩展命令 {command_id} 导入异常"
                    )
                    sys.modules.pop(module_name, None)
                    continue
                loaded_modules[command_path] = command_module

            handler = getattr(command_module, symbol, None)
            if not callable(handler):
                self._diagnostics.record_plugin_error(
                    "plugin", plugin_id, f"扩展命令 {command_id} 缺少函数 {symbol}"
                )
                continue
            try:
                inspect.signature(handler).bind(object(), {})
            except (TypeError, ValueError) as exc:
                self._diagnostics.record_plugin_error(
                    "plugin",
                    plugin_id,
                    f"扩展命令 {command_id} 参数不兼容: {exc}",
                )
                continue
            self._registry.extensions.register_command(
                plugin_id, command_id, handler, command_module
            )
            registered += 1
        return registered or None
