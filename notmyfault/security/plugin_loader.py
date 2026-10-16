"""插件校验后再导入，使用调用方传入的注册表、诊断对象和提权模块。"""

import inspect
import hashlib
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from notmyfault.core.logging import engine_error, engine_info, engine_warn
from notmyfault.extensions.registry import ExtensionRegistry
from notmyfault.security.plugin_schema import admin_executables
from notmyfault.security.plugin_imports import PluginImports
from notmyfault.security.plugin_checks import (
    PluginKind,
    evaluate_plugin,
    inspect_plugin,
    inspect_plugin_tree,
    is_plugin_platform_compatible,
    plugin_directories,
    resolve_plugin_entrypoint,
)
from notmyfault.security.plugins import (
    load_plugin_manifest,
    plugin_signature_kind_from_digest,
    verify_plugin_integrity_from_hashes,
)
from notmyfault.security.security import SecurityMode
from notmyfault.security import plugin_resources


def _version_info(meta: Dict[str, Any]) -> str:
    if meta.get("version_code") is not None:
        return f" v{meta['version_code']}"
    return ""


def _perm_info(meta: Dict[str, Any]) -> str:
    if meta.get("permissions"):
        return f" [权限: {', '.join(meta['permissions'])}]"
    return ""



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
        self.source_digests: Dict[str, str] = {}
        # 已发现但还没导入的动作插件，第一次执行时才导入。
        self.pending: Dict[str, Dict[str, Any]] = {}
        # 插件根目录，plugin_resource 靠它定位插件自带的二进制和资源。
        self.plugin_roots: Dict[str, str] = {}
        self.plugin_kinds: Dict[str, PluginKind] = {}
        self.extensions = ExtensionRegistry()
        self.importers: Dict[str, PluginImports] = {}
        self._materializer: Optional[Callable[[str], Any]] = None

    def set_materializer(self, callback: Callable[[str], Any]) -> None:
        self._materializer = callback

    def resolve_action(self, plugin_id: str) -> Optional[Callable[..., Any]]:
        """返回动作入口函数，还没导入的插件在这里触发首次导入。"""
        run = self.actions_funcs.get(plugin_id)
        if run is not None:
            return run
        materializer = self._materializer
        if materializer is None:
            return None
        materializer(plugin_id)
        return self.actions_funcs.get(plugin_id)

    def resolve_trigger(self, plugin_id: str) -> Optional[Callable[..., Any]]:
        """返回触发器入口函数，还没导入的插件在这里触发首次导入。"""
        run = self.triggers_funcs.get(plugin_id)
        if run is not None:
            return run
        materializer = self._materializer
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

    def resource(self, plugin_id: str, *relative_parts: str) -> str:
        return plugin_resources.resolve_plugin_resource(self.plugin_roots, plugin_id, *relative_parts)

    def register(
        self,
        kind: PluginKind,
        plugin_id: str,
        meta: Dict[str, Any],
        run: Callable[..., Any],
        module: Any,
        *, source_digest: str = "",
    ) -> None:
        # 注册项必须同时保存元数据、入口函数和模块对象。
        meta_store, func_store = self.stores(kind)
        func_store[plugin_id] = run
        meta_store[plugin_id] = meta
        self.modules[plugin_id] = module
        self.source_digests[plugin_id] = source_digest
        self.plugin_kinds[plugin_id] = kind

    def unregister(self, kind: PluginKind, plugin_id: str) -> None:
        # 回滚和卸载都从三个表中删除同一 plugin id。
        meta_store, func_store = self.stores(kind)
        func_store.pop(plugin_id, None)
        meta_store.pop(plugin_id, None)
        module = self.modules.pop(plugin_id, None)
        self.source_digests.pop(plugin_id, None)
        if module is not None:
            sys.modules.pop(getattr(module, "__name__", ""), None)
        self.pending.pop(plugin_id, None)
        self.plugin_roots.pop(plugin_id, None)
        self.plugin_kinds.pop(plugin_id, None)
        self.extensions.unregister_plugin(plugin_id)
        self._cleanup_plugin_imports(plugin_id)

    def clear(self) -> None:
        """清理本代引擎加载的插件模块和导入路径。"""
        for plugin_id in tuple(self.importers):
            self._cleanup_plugin_imports(plugin_id)
        for module in tuple(self.modules.values()):
            sys.modules.pop(getattr(module, "__name__", ""), None)
        self.triggers_meta.clear()
        self.triggers_funcs.clear()
        self.actions_meta.clear()
        self.actions_funcs.clear()
        self.modules.clear()
        self.source_digests.clear()
        self.pending.clear()
        self.plugin_roots.clear()
        self.plugin_kinds.clear()
        self.extensions.clear()

    def _cleanup_plugin_imports(self, plugin_id: str) -> None:
        importer = self.importers.pop(plugin_id, None)
        if importer is not None:
            importer.close()


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
        plugin_manifest_path: str,
    ) -> None:
        # 加载器只保存 registry、config 和 diagnostics 等传入对象。
        self._registry = registry
        self._config = config
        self._diagnostics = diagnostics
        self._security_mode = security_mode
        self._sudo = sudo
        self._engine_token = engine_token
        self._integrity_errors = integrity_errors
        self._plugin_manifest_path = plugin_manifest_path
        # 多个工作流线程可能同时首次执行同一个懒加载动作。
        self._materialize_lock = threading.RLock()
        registry.set_materializer(self.materialize_pending)

    def _restore_override(
        self,
        plugin_type: PluginKind,
        plugin_id: str,
        prev: Optional[Tuple[Any, Any, Any]],
        source_digest: str = "",
    ) -> None:
        """覆盖加载失败时恢复旧插件。"""
        if not prev:
            return
        old_module, old_meta, old_run = prev
        if old_meta is None or old_run is None:
            return
        self._registry.register(plugin_type, plugin_id, old_meta, old_run, old_module, source_digest=source_digest)

    def load(
        self, base_dir: str, kind: PluginKind, origin: str = "builtin"
    ) -> Tuple[int, int]:
        if kind not in ("action", "trigger"):
            raise ValueError(f"未知插件类型: {kind}")
        meta_store, func_store = self._registry.stores(kind)
        store_name = kind.title()
        loaded_count = failed_count = 0
        loaded_summaries = []
        installed = load_plugin_manifest(self._plugin_manifest_path) if origin == "user" else {}

        def reject(plugin_id: str, reason: str) -> None:
            self._diagnostics.record_plugin_error(store_name, plugin_id, reason)
            engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=reason)

        for folder in plugin_directories(base_dir):
            if not (folder / f"{kind}.json").is_file():
                continue
            folder_path = str(folder.resolve())
            tree = inspect_plugin_tree(folder_path)
            if tree is None:
                failed_count += 1
                reject(folder.name, "无法读取插件文件，拒绝加载")
                continue
            inspection = inspect_plugin(folder_path, kind, origin, tree=tree, installed_manifest=installed)
            meta = inspection.meta
            plugin_id = meta.get("id", folder.name)
            if inspection.errors or inspection.schema_errors:
                decision = evaluate_plugin(inspection, self._security_mode)
                failed_count += 1
                reject(plugin_id, "; ".join(decision["errors"]))
                continue
            disabled = self._config.get("disabled_plugins", {})
            if isinstance(disabled, dict) and plugin_id in disabled.get(kind + "s", []):
                print(f'[Engine] 插件 "{plugin_id}" 已被用户禁用，跳过')
                continue
            if not inspection.platform_compatible:
                engine_info(f"plugin_platform_skipped: {plugin_id}")
                continue
            if not inspection.entrypoint_exists and not meta.get("entrypoints"):
                # 平台适用但入口文件不在：插件用不了，必须让用户看到原因
                failed_count += 1
                reject(plugin_id, "当前平台入口不存在")
                continue
            decision = evaluate_plugin(inspection, self._security_mode)
            if not decision["allowed"]:
                failed_count += 1
                reject(plugin_id, "; ".join(decision["errors"]))
                continue
            for warning in decision["warnings"]:
                print(f'[Engine] [安全] 插件 "{plugin_id}" {warning}', file=sys.stderr)
                engine_warn(f"plugin_check: {plugin_id} {warning}")
                if warning.startswith("借壳提权"):
                    self._integrity_errors.append(f"{plugin_id}: {warning}")
            if origin != "builtin":
                integrity_ok, reason = verify_plugin_integrity_from_hashes(
                    plugin_id, tree.file_snapshot, self._plugin_manifest_path
                )
                if not integrity_ok:
                    self._integrity_errors.append(reason)
                    engine_warn(f"integrity_check: {reason}")
                    if self._security_mode == SecurityMode.STRICT:
                        failed_count += 1
                        reject(plugin_id, reason)
                        continue
            existing_kind = self._registry.plugin_kinds.get(plugin_id)
            if existing_kind is not None and existing_kind != kind:
                failed_count += 1
                reject(plugin_id, "插件 id 已被其他类型的插件使用")
                continue
            previous_root = self._registry.plugin_roots.get(plugin_id)
            meta_with_origin = {
                **meta, "origin": origin,
                "signature_kind": inspection.signature.kind,
            }
            self._registry.plugin_roots[plugin_id] = folder_path
            self._registry.plugin_kinds[plugin_id] = kind
            self._registry.extensions.register_manifest(plugin_id, kind, meta_with_origin, folder_path)
            old_module = self._registry.get_module(plugin_id)
            old_run = func_store.get(plugin_id)
            entry = {
                "kind": kind, "plugin_id": plugin_id,
                "folder_path": folder_path, "entry_path": inspection.entrypoint,
                "meta": meta_with_origin, "origin": origin,
                "previous_root": previous_root,
                "file_snapshot": tree.file_snapshot,
                "signature_kind": inspection.signature.kind,
                "prev": (old_module, meta_store.get(plugin_id), old_run) if old_run is not None else None,
            }
            if old_run is not None:
                if self.materialize_entry(entry, announce=kind == "action") is None:
                    failed_count += 1
                    continue
            else:
                self._registry.pending[plugin_id] = entry
                meta_store[plugin_id] = meta_with_origin
            loaded_count += 1
            loaded_summaries.append(plugin_id)
        if loaded_summaries:
            print(f"[Engine] 装载{store_name} {loaded_count} 个: " + ", ".join(loaded_summaries))
        return loaded_count, failed_count

    def materialize_pending(self, plugin_id: str) -> Optional[Any]:
        """导入还没加载的插件，找不到对应条目时返回 None"""
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
        module_prefix = f"notmyfault.{plugin_type}_"
        store_name = plugin_type.title()
        meta = entry["meta"]
        origin = entry["origin"]
        meta_store, func_store = self._registry.stores(plugin_type)
        prev = entry.get("prev")
        previous_root = entry.get("previous_root")

        with self._materialize_lock:
            previous_digest = self._registry.source_digests.get(plugin_id, "")
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
            previous_importer = None
            if prev is not None:
                previous_importer = self._registry.importers.pop(plugin_id, None)
                self._registry.unregister(plugin_type, plugin_id)

            self._registry.plugin_roots[plugin_id] = folder_path
            self._registry.plugin_kinds[plugin_id] = plugin_type
            self._registry.extensions.register_manifest(
                plugin_id, plugin_type, meta, folder_path
            )

            module_name = f"{module_prefix}{plugin_id}"

            def fail(reason: str) -> None:
                sys.modules.pop(module_name, None)
                self._registry.extensions.unregister_plugin(plugin_id)
                self._registry._cleanup_plugin_imports(plugin_id)
                self._registry.plugin_roots.pop(plugin_id, None)
                self._registry.plugin_kinds.pop(plugin_id, None)
                if prev is not None:
                    self._restore_override(
                        plugin_type, plugin_id, prev, previous_digest
                    )
                    if previous_importer is not None:
                        self._registry.importers[plugin_id] = previous_importer
                        sys.modules.update(previous_importer.modules)
                    if previous_root is not None:
                        self._registry.plugin_roots[plugin_id] = previous_root
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
                        elif old_meta.get("execution_mode") == "isolated":
                            self._register_isolated_extensions(plugin_id, _old_run)
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
            # 导入前再读一轮目录，签名和文件快照共用这次结果
            tree = inspect_plugin_tree(folder_path)
            if tree is None:
                fail("无法读取插件文件，拒绝导入")
                return None
            if (
                plugin_signature_kind_from_digest(
                    folder_path, origin, tree.payload_digest
                )
                != expected_signature
            ):
                fail("插件签名在物化前发生变化，拒绝导入")
                return None
            if tree.file_snapshot != entry.get("file_snapshot"):
                fail("插件文件在校验后发生变化，拒绝导入")
                return None

            if (
                plugin_type == "action"
                and meta.get("execution_mode") == "isolated"
            ):
                from notmyfault.core.plugin_worker import run_isolated_action

                entry_relative = os.path.relpath(py_file, folder_path).replace(
                    os.sep, "/"
                )
                entry_hash = tree.file_snapshot.get(entry_relative)
                if not entry_hash:
                    fail("隔离动作入口没有完整性记录")
                    return None

                def isolated_run(
                    _meta,
                    params,
                    context,
                    _entry=py_file,
                    _entry_hash=entry_hash,
                    _info=meta,
                ):
                    ok, result = run_isolated_action(
                        _entry, _entry_hash, _info, params, context,
                        plugin_root=folder_path, file_snapshot=tree.file_snapshot,
                    )
                    if not ok:
                        raise RuntimeError(f"isolated worker 执行失败: {result}")
                    return result

                setattr(isolated_run, "_isolated_proxy", True)

                def check_isolated_load():
                    ok, result = run_isolated_action(
                        py_file, entry_hash, meta, {}, {},
                        plugin_root=folder_path, file_snapshot=tree.file_snapshot,
                        load_only=True,
                    )
                    if not ok:
                        raise RuntimeError(f"isolated worker 导入失败: {result}")

                setattr(isolated_run, "check_load", check_isolated_load)
                extension_workers = {}

                def isolated_command(context, payload, command_id):
                    from notmyfault.core.plugin_worker import ExtensionWorker

                    session_id = context.session.session_id
                    worker = extension_workers.get(session_id)
                    if worker is None:
                        worker = ExtensionWorker(
                            py_file, entry_hash, meta, folder_path, tree.file_snapshot, context,
                        )
                        extension_workers[session_id] = worker

                        def close_worker():
                            extension_workers.pop(session_id, None)
                            worker.close()

                        context.register_cleanup(close_worker)
                    return worker.invoke(command_id, payload)

                setattr(isolated_run, "extension_command", isolated_command)
                self._register_isolated_extensions(plugin_id, isolated_run)
                if prev is not None:
                    old_module = prev[0]
                    if old_module is not None and hasattr(old_module, "teardown"):
                        try:
                            old_module.teardown()
                        except Exception:
                            engine_warn(f"override teardown \"{plugin_id}\" 异常: {traceback.format_exc()[-200:]}")
                    self._sudo.deauthorize_plugin(plugin_id, self._engine_token)
                if previous_importer is not None:
                    previous_importer.close()
                self._registry.register(plugin_type, plugin_id, meta, isolated_run, None, source_digest=hashlib.sha256(json.dumps(tree.file_snapshot, sort_keys=True).encode()).hexdigest())
                self._registry.pending.pop(plugin_id, None)
                return meta

            try:
                importer = PluginImports(folder_path, module_name, tree.py_sources, resource_roots=self._registry.plugin_roots)
                self._registry.importers[plugin_id] = importer
                module = importer.load_entry(py_file)
            except Exception:
                print(
                    f"[Engine] 插件 \"{plugin_id}\" Python 加载失败 ({py_file}):",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                fail(f"Python 加载异常: {traceback.format_exc()[-200:]}")
                return None

            entry_name = "run_with_context" if plugin_type == "action" else "run"
            if not callable(getattr(module, entry_name, None)):
                print(
                    f"[Engine] 插件 \"{plugin_id}\" ({meta['name']}) 缺少 {entry_name}() 函数，跳过",
                    file=sys.stderr,
                )
                fail(f"缺少 {entry_name}() 函数")
                return None

            if plugin_type == "action":
                try:
                    inspect.signature(getattr(module, entry_name)).bind({}, {}, {})
                except (TypeError, ValueError) as exc:
                    message = f"动作入口参数不符: {exc}"
                    fail(message)
                    return None

            if plugin_type == "trigger" and meta.get("sampling_api") == "poll-v1":
                try:
                    inspect.signature(module.create_sampler).bind(None)
                except (AttributeError, TypeError, ValueError):
                    fail("poll-v1 触发器必须提供 create_sampler(context)")
                    return None

            if plugin_type == "trigger":
                try:
                    emit = lambda *_args: None
                    inspect.signature(module.run).bind({}, {}, emit, threading.Event())
                except (TypeError, ValueError) as exc:
                    message = f"触发器入口参数不符: {exc}"
                    print(f'[Engine] 触发器 "{plugin_id}" {message}', file=sys.stderr)
                    fail(message)
                    return None

            self._registry.register(
                plugin_type, plugin_id, meta, getattr(module, entry_name), module,
                source_digest=hashlib.sha256(json.dumps(tree.file_snapshot, sort_keys=True).encode()).hexdigest(),
            )
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
                        fail("setup() 返回 False")
                        return None
                except Exception:
                    print(
                        f"[Engine] 触发器 \"{plugin_id}\" setup() 执行异常:",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                    self._registry.unregister(plugin_type, plugin_id)
                    fail("setup() 执行异常")
                    return None

            # 新插件 setup() 成功后才调用旧插件 teardown()。
            previous_module = prev[0] if prev is not None else None
            if previous_module is not None and hasattr(previous_module, "teardown"):
                try:
                    previous_module.teardown()
                except Exception:
                    engine_warn(f"override teardown \"{plugin_id}\" 异常: {traceback.format_exc()[-200:]}")
            if previous_importer is not None:
                previous_importer.close()

            # teardown 走完再撤销旧模块授权，随后按新模块声明授权。
            if prev is not None:
                self._sudo.deauthorize_plugin(plugin_id, self._engine_token)
            if "admin" in (meta.get("permissions") or []):
                try:
                    def authorize_module(loaded_module):
                        self._sudo.authorize_plugin(
                            plugin_id, self._engine_token, module=loaded_module,
                            allowed_executables=admin_executables(meta),
                        )

                    for loaded_module in importer.modules.values():
                        authorize_module(loaded_module)
                    importer.on_module_loaded = authorize_module
                    print(f"[Engine] [安全] 插件 \"{plugin_id}\" 已注册管理员权限")
                except PermissionError as e:
                    print(f"[Engine] [!!] 插件 \"{plugin_id}\" 管理员权限注册失败: {e}", file=sys.stderr)
                    engine_error("admin_registration_failed", plugin=plugin_id, error=str(e))

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

    def _register_isolated_extensions(self, plugin_id, run):
        plugin = self._registry.extensions.plugin(plugin_id)
        if plugin is None:
            return
        for command_id in plugin["contributions"]["commands"]:
            def handler(context, payload, _command_id=command_id):
                return run.extension_command(context, payload, _command_id)

            self._registry.extensions.register_command(plugin_id, command_id, handler, None)

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
                try:
                    command_module = self._registry.importers[plugin_id].load_file(command_path)
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
