"""加载插件并完成元数据校验、安全检查、导入、注册和 setup，加载器只依赖调用方提供的运行时协作者且不持有 AutomationEngine。"""

import inspect
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
    inspect_signature,
    is_plugin_platform_compatible,
    plugin_directories,
    resolve_plugin_entrypoint,
    validate_plugin_signature,
)
from notmyfault.security.plugins import (
    load_plugin_manifest,
    plugin_signature_kind_from_payload,
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
        # 已发现但还没导入的动作插件，第一次执行时才导入。
        self.pending: Dict[str, Dict[str, Any]] = {}
        # 插件根目录，plugin_resource 靠它定位插件自带的二进制和资源。
        self.plugin_roots: Dict[str, str] = {}
        self.plugin_kinds: Dict[str, PluginKind] = {}
        self.extensions = ExtensionRegistry()
        self.importers: Dict[str, PluginImports] = {}
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

    def resource(self, plugin_id: str, *relative_parts: str) -> str:
        return plugin_resources.resolve_plugin_resource(self.plugin_roots, plugin_id, *relative_parts)

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
        self._materialize_lock = threading.Lock()
        registry.set_action_materializer(self.materialize_pending_action)
        registry.set_trigger_materializer(self.materialize_pending_trigger)

    def _restore_override(
        self,
        plugin_type: PluginKind,
        plugin_id: str,
        prev: Optional[Tuple[Any, Any, Any]],
    ) -> None:
        """覆盖加载失败时恢复旧插件，旧模块尚未执行 teardown 时可直接重新注册。"""
        if not prev:
            return
        old_module, old_meta, old_run = prev
        if old_module is None or old_meta is None or old_run is None:
            return
        self._registry.register(plugin_type, plugin_id, old_meta, old_run, old_module)

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
            if origin == "builtin" and not meta["enabled"]:
                print(f'[Engine] 插件 "{plugin_id}" 已禁用，跳过')
                continue
            disabled = self._config.get("disabled_plugins", {})
            if isinstance(disabled, dict) and plugin_id in disabled.get(kind + "s", []):
                print(f'[Engine] 插件 "{plugin_id}" 已被用户禁用，跳过')
                continue
            if not inspection.platform_compatible:
                engine_info(f"plugin_platform_skipped: {plugin_id}")
                continue
            if not inspection.entrypoint_exists and not meta.get("entrypoints"):
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
                "signature_source": inspection.signature.source,
                "signature_format": inspection.signature.format,
            }
            self._registry.plugin_roots[plugin_id] = folder_path
            self._registry.plugin_kinds[plugin_id] = kind
            self._registry.extensions.register_manifest(plugin_id, kind, meta_with_origin, folder_path)
            old_module = self._registry.get_module(plugin_id)
            entry = {
                "kind": kind, "plugin_id": plugin_id,
                "folder_path": folder_path, "entry_path": inspection.entrypoint,
                "meta": meta_with_origin, "origin": origin,
                "previous_root": previous_root,
                "file_snapshot": tree.file_snapshot,
                "signature_kind": inspection.signature.kind,
                "signature_format": inspection.signature.format,
                "prev": (old_module, meta_store.get(plugin_id), func_store.get(plugin_id)) if old_module is not None else None,
            }
            if old_module is not None:
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
        module_prefix = f"notmyfault.{plugin_type}_"
        store_name = plugin_type.title()
        meta = entry["meta"]
        origin = entry["origin"]
        meta_store, func_store = self._registry.stores(plugin_type)
        prev = entry.get("prev")
        previous_root = entry.get("previous_root")

        with self._materialize_lock:
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
                        plugin_type, plugin_id, prev
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
            # 物化阶段必须使用与发现阶段相同的 payload 格式
            signature_payload = (
                tree.legacy_payload
                if entry.get("signature_format") == "legacy"
                else tree.payload
            )
            if (
                plugin_signature_kind_from_payload(
                    folder_path, origin, signature_payload
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
                    _entry=py_file,
                    _entry_hash=entry_hash,
                    _info=meta,
                ):
                    ok, result = run_isolated_action(
                        _entry, _entry_hash, _info, params, {},
                        plugin_root=folder_path, file_snapshot=tree.file_snapshot,
                    )
                    if not ok:
                        raise RuntimeError(f"isolated worker 执行失败: {result}")
                    return result

                def isolated_run_with_context(
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

                setattr(isolated_run, "run_with_context", isolated_run_with_context)
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
                self._registry.register(plugin_type, plugin_id, meta, isolated_run, None)
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
                    self._sudo.authorize_plugin(
                        plugin_id,
                        self._engine_token,
                        module=module,
                        allowed_executables=admin_executables(meta),
                    )
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
