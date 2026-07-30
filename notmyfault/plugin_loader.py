"""插件注册与加载流水线。

本模块负责插件发现、元数据校验、安全检查、动态加载、注册和 setup。
它只依赖显式传入的运行时协作者，不持有 AutomationEngine。
"""

import importlib.util
import inspect
import json
import os
import sys
import threading
import traceback
from typing import Any, Callable, Dict, Literal, Optional, Tuple

from notmyfault.logging import engine_error, engine_info, engine_warn
from notmyfault.plugin_schema import (
    check_permissions_conform,
    is_known_permission,
    validate_plugin_meta,
)
from notmyfault.plugins import (
    check_sudo_import,
    scan_plugin_capabilities,
    verify_plugin_integrity,
    verify_plugin_sig,
)
from notmyfault.security import SecurityMode
from notmyfault.signing import plugin_files

# 向后兼容早期内部导入。
_validate_plugin_meta = validate_plugin_meta
_check_sudo_import = check_sudo_import

PluginKind = Literal["trigger", "action"]


def _current_platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform


def is_plugin_platform_compatible(meta: Dict[str, Any]) -> bool:
    """清单未声明 platforms 时保持向后兼容，视为支持所有平台。"""
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
    """解析当前平台入口，并再次防御目录逃逸。"""
    entrypoints = meta.get("entrypoints") or {}
    relative_path = entrypoints.get(_current_platform_name(), default_filename)
    plugin_root = os.path.realpath(folder_path)
    entrypoint = os.path.realpath(os.path.join(plugin_root, relative_path))
    if os.path.commonpath((plugin_root, entrypoint)) != plugin_root:
        raise ValueError(f"插件入口逃逸插件目录: {relative_path}")
    return entrypoint


class PluginRegistry:
    """已加载插件的唯一登记点。

    集中保存已加载插件的元数据、入口函数与模块对象。引擎仍可通过旧的
    ``triggers_meta`` / ``actions_funcs`` 属性访问这些字典，以便平滑迁移。
    """

    def __init__(self) -> None:
        # 这几本账是同一份真相；engine 上那些同名属性只是兼容用的窗口。
        self.triggers_meta: Dict[str, Dict[str, Any]] = {}
        self.triggers_funcs: Dict[str, Callable[..., Any]] = {}
        self.actions_meta: Dict[str, Dict[str, Any]] = {}
        self.actions_funcs: Dict[str, Callable[..., Any]] = {}
        # 插件 id 目前在 trigger/action 间全局唯一；保留这一既有约束。
        self.modules: Dict[str, Any] = {}

    def stores(
        self, kind: PluginKind
    ) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Callable[..., Any]]]:
        # 小分流，别让 loader 到处写 if/else，后面加第三种插件时也好找地方。
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
        # 一次登记三样东西：说明书、可调用入口、模块本体；少一个都不算真装上。
        meta_store, func_store = self.stores(kind)
        func_store[plugin_id] = run
        meta_store[plugin_id] = meta
        self.modules[plugin_id] = module

    def unregister(self, kind: PluginKind, plugin_id: str) -> None:
        # 回滚和卸载都走这里，避免函数删了模块还在这种"幽灵插件"。
        meta_store, func_store = self.stores(kind)
        func_store.pop(plugin_id, None)
        meta_store.pop(plugin_id, None)
        self.modules.pop(plugin_id, None)


# ============================================================================
# 插件加载器 -- 流水线
# ============================================================================

class PluginLoader:
    """通用插件加载器（触发器与动作共用）。

    流水线阶段：discover -> schema -> signature -> integrity -> load -> register -> setup。
    每阶段异常隔离，故障插件计入诊断而不污染引擎状态。对外行为保持不变。
    """

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
        # Loader 只拿它干活真正需要的零件，不再抱着整个 engine 不撒手。
        self._registry = registry
        self._config = config
        self._diagnostics = diagnostics
        self._security_mode = security_mode
        self._sudo = sudo
        self._engine_token = engine_token
        self._integrity_errors = integrity_errors

    def _restore_override(
        self,
        plugin_type: str,
        plugin_id: str,
        prev: Optional[Tuple[Any, Any, Any]],
        func_store: Dict[str, Any],
        meta_store: Dict[str, Dict[str, Any]],
    ) -> None:
        """覆盖装载失败时把旧插件原样装回去，别让一个装失败的用户插件把好用的内置插件带走。

        旧插件在被覆盖时只被 unregister，没被 teardown，状态完好，直接 register 即可。
        """
        if not prev:
            return
        old_module, old_meta, old_run = prev
        if old_module is None or old_meta is None or old_run is None:
            return
        self._registry.register(plugin_type, plugin_id, old_meta, old_run, old_module)
        # 与 register 路径一致：同步显式写入传入的存储字典（兼容独立存储场景）。
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
        """加载插件目录。

        Returns:
            (loaded_count, failed_count) - loaded 是成功加载数，failed 是出错数。
            故意跳过的（如 disabled、缺少文件）不计入 failed。
        """
        plugin_type = "trigger" if store_name == "Trigger" else "action"
        root_dir = os.path.join(base_dir, plugins_dir)
        loaded_count = 0
        failed_count = 0

        if not os.path.isdir(root_dir):
            print(f"[Engine] 插件目录不存在，跳过: {root_dir}", file=sys.stderr)
            return 0, 0

        for folder_name in sorted(os.listdir(root_dir)):
            folder_path = os.path.join(root_dir, folder_name)
            if not os.path.isdir(folder_path):
                continue

            json_file = os.path.join(folder_path, json_filename)
            py_file = os.path.join(folder_path, py_filename)

            # --- 文件存在检查（非错误：可能不是插件目录）---
            # 插件目录里偶尔有 README 或缓存，别把它们当成案发现场。
            if not os.path.exists(json_file):
                print(
                    f"[Engine] 插件目录缺少 {json_filename}，跳过: {folder_path}",
                    file=sys.stderr,
                )
                continue
            # --- JSON 解析 ---
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

            # --- Schema 校验 ---
            is_valid, errors = validate_plugin_meta(meta, plugin_type)
            plugin_id = meta.get("id", folder_name)
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

            # --- 签名校验（必须在 exec_module 前）---
            # strict 模式不能先执行模块级代码再决定是否信任插件；normal /
            # permissive 仍保持原有的降级加载语义。
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

            # --- 权限合规校验（必须在 exec_module 前）---
            # permissive/normal 继续兼容未知权限；strict 在执行任何插件代码前拒载。
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

            # --- 安全能力扫描（exec 前，AST 级，避免执行未声明危险代码）---
            # 先看源码再 import，不能让“我只是看看”顺手把危险代码跑起来。
            python_files = [
                str(path)
                for path in plugin_files(folder_path)
                if path.suffix == ".py"
            ]
            caps = set().union(
                *(scan_plugin_capabilities(path) for path in python_files)
            )

            # self_elevation（自行提权）一律禁止：插件要提权必须走
            # notmyfault.sudo.run_as_admin 并声明 admin，禁止自己 ShellExecute("runas") 等。
            # 不分安全模式--这是硬规则，permissive 也拒载。
            if "self_elevation" in caps:
                cap_msg = "self_elevation（自行提权：必须改走 notmyfault.sudo.run_as_admin 并声明 admin）"
                print(f'[Engine] [安全] 插件 "{plugin_id}" 触发禁止能力: {cap_msg}', file=sys.stderr)
                engine_warn(f"forbidden_capability: {plugin_id} {cap_msg}")
                failed_count += 1
                self._diagnostics.record_plugin_error(store_name, plugin_id, f"禁止能力: {cap_msg}")
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=f"禁止能力: {cap_msg}")
                continue

            # dynamic_exec（exec/eval/compile/__import__/importlib.import_module）一律禁止：
            # 可绕过所有 AST 能力检测，声明了也不安全。不分安全模式--硬规则。
            if "dynamic_exec" in caps:
                cap_msg = "dynamic_exec（exec/eval/compile/__import__ 动态执行：可绕过所有能力检测）"
                print(f'[Engine] [安全] 插件 "{plugin_id}" 触发禁止能力: {cap_msg}', file=sys.stderr)
                engine_warn(f"forbidden_capability: {plugin_id} {cap_msg}")
                failed_count += 1
                self._diagnostics.record_plugin_error(store_name, plugin_id, f"禁止能力: {cap_msg}")
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=f"禁止能力: {cap_msg}")
                continue

            declared_perms = set(meta.get("permissions") or [])
            # self_elevation / dynamic_exec 不可声明，从“未声明”判定里剔除。
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

            # --- 权限声明与源码引用一致性（exec 前）---
            if any(check_sudo_import(path) for path in python_files):
                declared_perms = meta.get("permissions") or []
                if "admin" not in declared_perms:
                    print(
                        f"[Engine] [!!] 插件 \"{plugin_id}\" import 了 notmyfault.sudo "
                        f"但未在元数据中声明 'admin' 权限",
                        file=sys.stderr,
                    )

            # --- 用户插件完整性校验（exec 前）---
            # 内置插件由构建时 Ed25519 签名覆盖；用户/第三方插件使用本地清单
            # 记录首次见到的文件内容。现有策略仅告警，不改变 normal/permissive
            # 模式的加载行为。
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

            # --- Python 模块加载 ---
            # 这里是真正执行插件 import 的临界点，前面的检查都是在给它铺软垫。
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

            # --- 提取 run 入口 ---
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

            # 内置触发器统一采用 event-v1：run(meta, config_list, emit_event,
            # shutdown_event)。在启动线程前做签名绑定检查，接口写错时直接标出
            # 插件问题，而不是让后台线程启动后才留下一段 traceback。
            if plugin_type == "trigger" and meta.get("trigger_api") == "event-v1":
                try:
                    inspect.signature(module.run).bind({}, [], lambda *_args: None, threading.Event())
                except (TypeError, ValueError) as exc:
                    message = f"event-v1 入口不兼容: {exc}"
                    print(f'[Engine] 触发器 "{plugin_id}" {message}', file=sys.stderr)
                    failed_count += 1
                    self._diagnostics.record_plugin_error(store_name, plugin_id, message)
                    engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=message)
                    continue

            # --- 同名覆盖 ---
            # 用户插件覆盖内置插件时，先拍下旧插件状态再卸下，但 teardown 推迟到
            # 新插件 setup 成功之后：万一新 setup 失败，能把旧插件原样装回去，
            # 别让一个装失败的用户插件把好用的内置插件一起带走。
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
                # 兼容直接调用 _load_plugins() 时传入的独立存储字典。
                func_store.pop(plugin_id, None)
                meta_store.pop(plugin_id, None)
            self._registry.register(
                plugin_type, plugin_id, {**meta, "origin": origin}, getattr(module, "run"), module
            )
            # 正常情况下这两个对象就是 registry 的兼容别名；保留显式写入
            # 以维持 _load_plugins() 的历史调用契约。
            func_store[plugin_id] = getattr(module, "run")
            meta_store[plugin_id] = {**meta, "origin": origin}

            # --- Admin 权限注册 ---
            # 声明 admin 不等于自动放行，仍需用本次引擎的令牌完成登记。
            if "admin" in (meta.get("permissions") or []):
                try:
                    self._sudo.authorize_plugin(plugin_id, self._engine_token)
                    print(f"[Engine] [安全] 插件 \"{plugin_id}\" 已注册管理员权限")
                except PermissionError as e:
                    print(f"[Engine] [!!] 插件 \"{plugin_id}\" 管理员权限注册失败: {e}", file=sys.stderr)
                    engine_error("admin_registration_failed", plugin=plugin_id, error=str(e))

            loaded_count += 1

            # --- 生命周期: setup (触发器) ---
            # setup 说"不行"就当本次装载没发生过，注册表回滚；若是覆盖装载，
            # 顺手把旧插件装回去（旧插件没 teardown 过，状态完好）。
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

            # 新插件已就位（触发器 setup 成功，或动作/无 setup 触发器直接就绪）：
            # 现在才安全地拆掉被覆盖的旧插件。之前只是卸下，没 teardown。
            if prev is not None and prev[0] is not None and hasattr(prev[0], "teardown"):
                try:
                    prev[0].teardown()
                except Exception:
                    engine_warn(f"override teardown \"{plugin_id}\" 异常: {traceback.format_exc()[-200:]}")

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
