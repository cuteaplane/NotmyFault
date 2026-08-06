"""加载插件并完成元数据校验、安全检查、导入、注册和 setup，加载器只依赖调用方提供的运行时协作者且不持有 AutomationEngine。"""

import importlib.util
import inspect
import json
import os
import posixpath
import sys
import threading
import traceback
from typing import Any, Callable, Dict, Literal, Optional, Tuple

from notmyfault.core.logging import engine_error, engine_info, engine_warn
from notmyfault.security.plugin_schema import (
    check_permissions_conform,
    is_known_permission,
    validate_plugin_meta,
)
from notmyfault.security.plugins import (
    check_sudo_import,
    scan_borrowed_privilege,
    scan_plugin_capabilities,
    verify_plugin_integrity,
    verify_plugin_sig,
)
from notmyfault.security.security import SecurityMode
from notmyfault.security.signing import plugin_files

# 旧调用仍通过这两个别名访问校验函数。
_validate_plugin_meta = validate_plugin_meta
_check_sudo_import = check_sudo_import

PluginKind = Literal["trigger", "action"]

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

    def unregister(self, kind: PluginKind, plugin_id: str) -> None:
        # 回滚和卸载都从三个表中删除同一 plugin id。
        meta_store, func_store = self.stores(kind)
        func_store.pop(plugin_id, None)
        meta_store.pop(plugin_id, None)
        self.modules.pop(plugin_id, None)


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

            if origin == "builtin":
                disabled_cfg = self._config.get("disabled_plugins", {})
                ptype_key = "triggers" if store_name == "Trigger" else "actions"
                disabled_list = disabled_cfg.get(ptype_key, []) if isinstance(disabled_cfg, dict) else []
                if plugin_id in disabled_list:
                    print(
                        f'[Engine] 插件 "{plugin_id}" ({meta["name"]}) 已被用户禁用，跳过'
                    )
                    continue

            # strict 模式在 exec_module 前检查签名，其他模式保留签名失败时的降级加载。
            signature_ok = verify_plugin_sig(folder_path, origin)
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

            # 先完成 AST 扫描，再执行模块导入。
            python_files = [
                str(path)
                for path in plugin_files(folder_path)
                if path.suffix == ".py"
            ]
            caps = set().union(
                *(scan_plugin_capabilities(path) for path in python_files)
            )

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
            uses_sudo = any(check_sudo_import(path) for path in python_files)
            has_admin = "admin" in (meta.get("permissions") or [])
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
                integrity_files = [
                    (json_filename, json_file),
                    (os.path.relpath(py_file, folder_path), py_file),
                ]
                integrity_ok, integrity_msg = verify_plugin_integrity(
                    plugin_id, integrity_files
                )
                if not integrity_ok:
                    warning = (
                        f"[Engine] [安全] 插件 \"{plugin_id}\" 完整性校验失败："
                        + integrity_msg
                    )
                    print(warning, file=sys.stderr)
                    engine_warn(f"integrity_check: {integrity_msg}")
                    self._integrity_errors.append(warning)

                # 扫描用户插件对已加载插件模块的导入和调用，命中时记录告警。
                borrowed_findings = []
                for path in python_files:
                    borrowed_findings.extend(
                        scan_borrowed_privilege(path)
                    )
                if borrowed_findings:
                    warning = (
                        f"[Engine] [安全] 插件 \"{plugin_id}\" 存在借壳提权嫌疑: "
                        + "；".join(sorted(set(borrowed_findings))[:3])
                    )
                    print(warning, file=sys.stderr)
                    engine_warn(f"borrowed_privilege: {plugin_id} {'; '.join(borrowed_findings)}")
                    self._integrity_errors.append(warning)

            # 模块导入从这里开始，签名和能力检查在调用前完成。
            module_name = f"{module_prefix}{plugin_id}"
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                print(
                    f"[Engine] 无法创建模块规格，跳过: {py_file}",
                    file=sys.stderr,
                )
                failed_count += 1
                self._diagnostics.record_plugin_error(
                    store_name, plugin_id, "无法创建模块规格"
                )
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason="无法创建模块规格")
                continue

            try:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                print(
                    f"[Engine] 插件 \"{plugin_id}\" Python 加载失败 ({py_file}):",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                failed_count += 1
                self._diagnostics.record_plugin_error(
                    store_name, plugin_id, f"Python 加载异常: {traceback.format_exc()[-200:]}"
                )
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason="Python 加载异常")
                continue

            if not hasattr(module, "run"):
                print(
                    f"[Engine] 插件 \"{plugin_id}\" ({meta['name']}) 缺少 run() 函数，跳过",
                    file=sys.stderr,
                )
                failed_count += 1
                self._diagnostics.record_plugin_error(
                    store_name, plugin_id, "缺少 run() 函数"
                )
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason="缺少 run() 函数")
                continue

            # event-v1 接收配置列表，event-v2 为每条规则使用独立配置，启动线程前先验证入口。
            if plugin_type == "trigger" and meta.get("trigger_api") in ("event-v1", "event-v2"):
                try:
                    config = [] if meta.get("trigger_api") == "event-v1" else {}
                    emit = lambda *_args: None
                    inspect.signature(module.run).bind({}, config, emit, threading.Event())
                except (TypeError, ValueError) as exc:
                    message = f"{meta['trigger_api']} 入口不兼容: {exc}"
                    print(f'[Engine] 触发器 "{plugin_id}" {message}', file=sys.stderr)
                    failed_count += 1
                    self._diagnostics.record_plugin_error(store_name, plugin_id, message)
                    engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=message)
                    continue

            # 覆盖插件时先保存旧状态并卸载注册项，teardown() 由新插件 setup() 成功后触发。
            prev: Optional[Tuple[Any, Any, Any]] = None
            if self._registry.get_module(plugin_id) is not None:
                old_origin = meta_store.get(plugin_id, {}).get("origin", "builtin")
                engine_info(f"{store_name} \"{plugin_id}\": {old_origin} -> {origin} override")
                prev = (
                    self._registry.get_module(plugin_id),
                    meta_store.get(plugin_id),
                    func_store.get(plugin_id),
                )
                self._registry.unregister(plugin_type, plugin_id)
                # 直接调用 _load_plugins() 时也要清理传入的存储字典。
                func_store.pop(plugin_id, None)
                meta_store.pop(plugin_id, None)
            self._registry.register(
                plugin_type, plugin_id, {**meta, "origin": origin}, getattr(module, "run"), module
            )
            # 显式写入维持 _load_plugins() 返回字典的历史行为。
            func_store[plugin_id] = getattr(module, "run")
            meta_store[plugin_id] = {**meta, "origin": origin}

            loaded_count += 1

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
                        self._restore_override(plugin_type, plugin_id, prev, func_store, meta_store)
                        loaded_count -= 1
                        failed_count += 1
                        self._diagnostics.record_plugin_error(
                            store_name, plugin_id, "setup() 返回 False"
                        )
                        engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason="setup() 返回 False")
                        continue
                except Exception:
                    print(
                        f"[Engine] 触发器 \"{plugin_id}\" setup() 执行异常:",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                    self._registry.unregister(plugin_type, plugin_id)
                    func_store.pop(plugin_id, None)
                    meta_store.pop(plugin_id, None)
                    self._restore_override(plugin_type, plugin_id, prev, func_store, meta_store)
                    loaded_count -= 1
                    failed_count += 1
                    self._diagnostics.record_plugin_error(
                        store_name, plugin_id, "setup() 执行异常"
                    )
                    engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason="setup() 执行异常")
                    continue

            # 新插件 setup() 成功后才调用旧插件 teardown()。
            if prev is not None and prev[0] is not None and hasattr(prev[0], "teardown"):
                try:
                    prev[0].teardown()
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

            version_info = (
                f" v{meta['version_code']}"
                if meta.get("version_code") is not None
                else ""
            )
            perm_info = ""
            if meta.get("permissions"):
                perm_info = f" [权限: {', '.join(meta['permissions'])}]"

            print(
                f"[Engine] 装载{store_name}: {meta['name']} ({plugin_id}){version_info}{perm_info}"
            )

        return loaded_count, failed_count
