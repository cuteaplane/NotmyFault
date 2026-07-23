"""规则引擎：加载插件 -> 匹配规则 -> 执行动作。

本文件包含三个核心类：
- PluginRegistry   -- 已加载插件的唯一登记点（元数据 / 入口函数 / 模块对象）
- PluginLoader     -- 插件加载流水线（discover -> validate -> verify -> load -> setup）
- AutomationEngine -- 编排门面，组装上述协作者 + 线程管理 + 生命周期

规则匹配/校验的纯逻辑下沉到 rules.py，诊断数据在 diagnostics.py，
安全模式探测在 security.py，插件安全检查工具在 plugins.py。
这些原来被剁成 core/ 和 plugins/ 两个包十几个文件，现在各回各家了。

错误处理原则：
- 不石沉大海：所有异常分支均记录到日志与 Diagnostics（trigger_crashes /
  errors），Dashboard / API 可观测。
- 不说崩就崩：触发器线程与外部回调经 _run_trigger / _safe_on_event 隔离，
  单点异常不击穿主循环、不静默杀死线程。
"""
import importlib.util
import inspect
import json
import os
import secrets
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from notmyfault.config import CONFIG_FILE, ConfigValidationError, load_verified_config
from notmyfault.logging import engine_error, engine_info, engine_warn
from notmyfault.plugin_schema import validate_plugin_meta, check_permissions_conform, is_known_permission

# --- 协作者（原 core/ 和 plugins/ 包已平铺为单文件）---
from notmyfault.diagnostics import Diagnostics
from notmyfault.plugins import (
    check_sudo_import,
    scan_plugin_capabilities,
    verify_plugin_integrity,
    verify_plugin_sig,
)
from notmyfault.rules import (
    ConditionRuntime,
    aggregate_trigger_params,
    check_event_params,
    get_rule_events,
    match_rule,
    validate_rules,
)
from notmyfault.security import SecurityMode, detect_security_mode as _detect_security_mode, verify_core_integrity
from notmyfault.workflow import build_context, invoke_action, resolve_templates

# 向后兼容旧导入
_validate_plugin_meta = validate_plugin_meta
_check_sudo_import = check_sudo_import


# ============================================================================
# 插件注册表 -- 已加载插件的唯一登记点
# ============================================================================

PluginKind = Literal["trigger", "action"]


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
            if not os.path.exists(py_file):
                print(
                    f"[Engine] 插件目录缺少 {py_filename}，跳过: {folder_path}",
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

            if origin == "builtin":
                disabled_cfg = self._config.get("disabled_plugins", {})
                ptype_key = "triggers" if store_name == "Trigger" else "actions"
                disabled_list = disabled_cfg.get(ptype_key, []) if isinstance(disabled_cfg, dict) else []
                if plugin_id in disabled_list:
                    print(
                        f'[Engine] 插件 "{plugin_id}" ({meta["name"]}) 已被用户禁用，跳过'
                    )
                    continue

            # --- 安全能力扫描（exec 前，AST 级，避免执行未声明危险代码）---
            # 先看源码再 import，不能让“我只是看看”顺手把危险代码跑起来。
            caps = scan_plugin_capabilities(py_file)

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

            # --- 权限一致性检查 ---
            if check_sudo_import(py_file):
                declared_perms = meta.get("permissions") or []
                if "admin" not in declared_perms:
                    print(
                        f"[Engine] [!!] 插件 \"{plugin_id}\" import 了 notmyfault.sudo "
                        f"但未在元数据中声明 'admin' 权限",
                        file=sys.stderr,
                    )

            # --- 插件完整性校验 ---
            # 内置插件由构建时 Ed25519 签名覆盖；再拿用户目录里的可变 hash
            # 缓存比对只会在源码升级后制造“被修改”假警报。用户/第三方插件
            # 才使用本地清单记录首次见到的文件内容。
            if origin != "builtin":
                integrity_files = [
                    (json_filename, json_file),
                    (py_filename, py_file),
                ]
                integrity_ok, integrity_msg = verify_plugin_integrity(plugin_id, integrity_files)
                if not integrity_ok:
                    warning = (f"[Engine] [安全] 插件 \"{plugin_id}\" 完整性校验失败：" + integrity_msg)
                    print(warning, file=sys.stderr)
                    engine_warn(f"integrity_check: {integrity_msg}")
                    self._integrity_errors.append(warning)

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

            # --- 签名校验（builtin + user 均需签名）---
            # 用户插件通过 /api/plugins/install 安装时会用项目私钥签名；
            # 直接放入用户插件目录的插件（无签名）在 strict 模式下拒载。
            if not verify_plugin_sig(folder_path, origin):
                if self._security_mode == SecurityMode.STRICT:
                    print(f"[Engine] [!!] {store_name} \"{plugin_id}\" 签名无效，不加载", file=sys.stderr)
                    continue
                elif self._security_mode == SecurityMode.NORMAL:
                    print(f"[Engine] [!!] {store_name} \"{plugin_id}\" 签名无效，降级加载", file=sys.stderr)
                # PERMISSIVE: 放行，允许直接放入文件夹安装（开发/测试用）

            # --- 权限合规校验（strict 模式）---
            # 允许安装时未做权限合规检查的插件（permissive 安装的），
            # 切换到 strict 后拒载。
            if self._security_mode == SecurityMode.STRICT:
                perms = meta.get("permissions") or []
                perm_conform, _ = check_permissions_conform(perms)
                if not perm_conform:
                    unknown = [p for p in perms if not is_known_permission(p)]
                    print(
                        f"[Engine] [!!] {store_name} \"{plugin_id}\" "
                        f"包含未知权限: {', '.join(unknown)}，strict 模式不加载",
                        file=sys.stderr,
                    )
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


# ============================================================================
# 规则引擎
# ============================================================================

class AutomationEngine:
    """规则引擎：加载插件 -> 匹配规则 -> 执行动作。

    公共属性与方法签名保持不变，保证插件、Dashboard 与测试零迁移。
    """

    def __init__(
        self,
        config: Dict[str, Any],
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        # config 是运行期间的“当前快照”；热重载只换 rules，别悄悄改调用方的字典。
        self.config = config
        self.rules: List[Dict[str, Any]] = config.get("rules", [])
        self.on_event = on_event

        self._plugin_registry = PluginRegistry()
        # 兼容旧插件、Dashboard 与测试：这些仍是可直接访问的原字典对象。
        # 这层马甲先别脱，外面还有人直接往里面塞假插件做测试。
        self.triggers_meta = self._plugin_registry.triggers_meta
        self.triggers_funcs = self._plugin_registry.triggers_funcs
        self.actions_meta = self._plugin_registry.actions_meta
        self.actions_funcs = self._plugin_registry.actions_funcs
        self._plugin_modules = self._plugin_registry.modules

        # 安全系统
        self._engine_token: str = secrets.token_hex(32)
        from notmyfault import sudo as _sudo
        _sudo.set_engine_token(self._engine_token)
        self._sudo = _sudo
        self._security_mode = _detect_security_mode()
        engine_info(f"Security mode: {self._security_mode.value}")
        self._plugin_integrity_errors: list[str] = []

        # 诊断（线程安全 + 快照 + 错误上报通道）
        self._diag_obj = Diagnostics()
        # 旧代码还会摸 _diag / _diag_lock；让它们继续活着，别把兼容性吓跑。
        self._diag: Dict[str, Any] = self._diag_obj.data      # 兼容：外部仍可读 engine._diag
        self._diag_lock = self._diag_obj.lock                 # 兼容：旧加锁路径

        # 关闭
        self._active_actions = 0
        self._action_lock = threading.Lock()
        self._action_done = threading.Condition()
        self._shutdown_flag: "threading.Event | None" = None
        # 前置条件未满足时的延后重试任务；以规则为粒度去重，避免定时器堆积。
        self._deferred_workflows: Dict[str, threading.Timer] = {}
        self._deferred_workflows_lock = threading.RLock()

        # 规则热重载线程安全
        self._rules_lock = threading.RLock()
        # 条件树需要保存 AND 分支最近一次命中；规则热重载时整体换代。
        self._condition_runtime = ConditionRuntime()

        # 触发器线程管理（单一所有权 + 锁，避免热加载与 shutdown 竞态）
        self._trigger_threads: Dict[str, threading.Thread] = {}
        self._trigger_events: Dict[str, threading.Event] = {}
        self._trigger_lock = threading.RLock()

        # 诊断时间
        self._start_time: float = 0.0

        # 协作者
        self._plugin_loader = PluginLoader(
            registry=self._plugin_registry,
            config=self.config,
            diagnostics=self._diag_obj,
            security_mode=self._security_mode,
            sudo=self._sudo,
            engine_token=self._engine_token,
            integrity_errors=self._plugin_integrity_errors,
        )

    # ------------------------------------------------------------------
    # 告警与安全回调
    # ------------------------------------------------------------------

    @staticmethod
    def _alert_user(title: str, message: str, open_dashboard: bool = False) -> None:
        """向用户发送告警；失败时记录到日志而非静默吞掉。"""
        try:
            from notmyfault.alert import alert_user
            alert_user(title, message, open_dashboard=open_dashboard)
        except Exception as e:
            engine_warn(f"alert_user 调用失败 ({title}): {e}")

    def _safe_on_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """调用外部事件回调，异常隔离：不外泄、不中断分发，但记录可观测。

        避免回调（如 API 推送）抛错击穿到触发器线程导致线程静默死亡。
        """
        if not self.on_event:
            return
        # UI / SSE 挂了不该连坐规则引擎，事件推送失败就记一笔然后放过主流程。
        try:
            self.on_event(event_type, payload)
        except Exception:
            err = traceback.format_exc()
            print(f"[Engine] [!!] on_event 回调异常 ({event_type}):", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            engine_error("event_callback_error", event_type=event_type, error=err[-500:])
            self._diag_obj.record_error("event_callback", f"{event_type}: {err[-300:]}")

    def _run_trigger(
        self,
        trigger_id: str,
        trigger_func: Callable[..., Any],
        trigger_meta: Dict[str, Any],
        config_list: List[Dict[str, Any]],
        stop_event: threading.Event,
    ) -> None:
        """触发器线程入口：隔离插件异常，崩溃即上报而非静默退出。"""
        # 触发器是插件代码，包一层安全气囊：炸了也不能把整台引擎带走。
        try:
            trigger_func(trigger_meta, config_list, self.emit_event, stop_event)
        except Exception:
            err = traceback.format_exc()
            print(f"[Engine] [!!] 触发器线程 {trigger_id} 崩溃:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            engine_error("trigger_crashed", trigger=trigger_id, error=err[-500:])
            self._diag_obj.record_trigger_crash(trigger_id, err[-300:])
            self._safe_on_event(
                "trigger_crashed",
                {"trigger_id": trigger_id, "error": err[-500:]},
            )
            try:
                from notmyfault.alert import alert_user
                alert_user(
                    f"触发器 {trigger_id} 崩溃",
                    err[-200:],
                    open_dashboard=False,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def get_diagnostics(self) -> Dict[str, Any]:
        """返回引擎当前诊断快照（供 Dashboard 展示）。"""
        uptime = time.time() - self._start_time if self._start_time > 0 else 0
        snap = self._diag_obj.snapshot()
        total_actions = snap["action_ok"] + snap["action_fail"]
        return {
            "uptime_seconds": round(uptime, 1),
            "plugins": {
                "actions_loaded": len(self.actions_funcs),
                "triggers_loaded": len(self.triggers_funcs),
                "errors": snap["plugin_errors"][-20:],
                "error_count": len(snap["plugin_errors"]),
                "admin_plugins": self._sudo.get_authorized_plugins(),
                "integrity_errors": self._plugin_integrity_errors[-10:],
            },
            "rules": {
                "total": len(self.rules),
                "issues": snap["rule_issues"],
                "issue_count": len(snap["rule_issues"]),
            },
            "actions": {
                "ok": snap["action_ok"],
                "fail": snap["action_fail"],
                "total": total_actions,
            },
            "hot_reload_errors": snap["hot_reload_errors"],
            "trigger_crashes": snap["trigger_crashes"],
            "trigger_crash_details": snap["trigger_crash_details"],
            "errors": snap["errors"][-20:],
        }

    # ------------------------------------------------------------------
    # 条件匹配助手（委托纯函数）
    # ------------------------------------------------------------------

    @staticmethod
    def _get_rule_events(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从规则中提取所有事件条件。"""
        return get_rule_events(rule)

    @staticmethod
    def _check_event_params(event_def: Dict[str, Any], event_payload: Dict[str, Any]) -> bool:
        """检查事件payload是否匹配事件定义的参数。"""
        return check_event_params(event_def, event_payload)

    # ------------------------------------------------------------------
    # 插件加载
    # ------------------------------------------------------------------

    def auto_load(self, load_paths) -> None:
        """扫描并加载所有插件。支持 [(base_dir, origin), ...] 或兼容单字符串。"""
        if isinstance(load_paths, str):
            # 老调用只给一个目录，给它补上 builtin 标签，别让历史用户白升级。
            load_paths = [(load_paths, "builtin")]
        engine_info("=== SESSION_START ===")
        t_loaded = t_failed = a_loaded = a_failed = 0
        for base_dir, origin in load_paths:
            _t, _tf = self._load_plugins(
                base_dir=base_dir,
                plugins_dir="triggers",
                json_filename="trigger.json",
                py_filename="trigger.py",
                module_prefix="notmyfault.trigger_",
                meta_store=self.triggers_meta,
                func_store=self.triggers_funcs,
                store_name="Trigger",
                origin=origin,
            )
            _a, _af = self._load_plugins(
                base_dir=base_dir,
                plugins_dir="actions",
                json_filename="action.json",
                py_filename="action.py",
                module_prefix="notmyfault.action_",
                meta_store=self.actions_meta,
                func_store=self.actions_funcs,
                store_name="Actioner",
                origin=origin,
            )
            t_loaded += _t; t_failed += _tf
            a_loaded += _a; a_failed += _af

        # 启动摘要
        print(
            f"\n[Engine] 已加载 {a_loaded} 个动作插件, {t_loaded} 个触发器插件"
        )

        # 插件加载失败 -> 告警
        total_failed = t_failed + a_failed
        if total_failed > 0:
            parts = []
            if t_failed > 0:
                parts.append(f"{t_failed} 个触发器")
            if a_failed > 0:
                parts.append(f"{a_failed} 个动作")
            fail_msg = "、".join(parts) + " 插件加载失败，请检查引擎日志"
            print(f"[Engine] [!!] {fail_msg}", file=sys.stderr)
            self._alert_user("插件加载异常", fail_msg)

        # admin 权限提示
        admin_plugins = [
            pid
            for pid, meta in {**self.triggers_meta, **self.actions_meta}.items()
            if "admin" in (meta.get("permissions") or [])
        ]
        if admin_plugins:
            print(
                f"[Engine] [!!] 以下插件声明了 admin 权限: {', '.join(admin_plugins)}"
            )
        print()

    def _load_plugins(
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
        """通用插件加载器（转发至 PluginLoader，保持签名与返回值兼容）。

        Returns:
            (loaded_count, failed_count)
        """
        return self._plugin_loader.load(
            base_dir=base_dir,
            plugins_dir=plugins_dir,
            json_filename=json_filename,
            py_filename=py_filename,
            module_prefix=module_prefix,
            meta_store=meta_store,
            func_store=func_store,
            store_name=store_name,
            origin=origin,
        )

    # ------------------------------------------------------------------
    # 规则 & 参数校验
    # ------------------------------------------------------------------

    def _validate_all_rules(self) -> Tuple[int, int]:
        """校验所有规则的 event/action 引用和参数是否与已加载插件匹配。

        非致命：只打印警告，不拒绝任何规则。
        校验逻辑下沉到 rules.validate_rules 纯函数，这里只管打印和记诊断。
        """
        # 校验时拿副本：热重载可以在另一边准备下一份规则，别互相抢纸笔。
        with self._rules_lock:
            rules = list(self.rules)

        self._diag_obj.reset_rule_issues()
        valid, total, issues, warnings = validate_rules(
            rules, self.triggers_meta, self.actions_meta
        )

        # 引用类问题：进诊断 + 日志，Dashboard 能看到
        for rule_name, msg in issues:
            print(f"[Engine] [!!] 规则 \"{rule_name}\" {msg}", file=sys.stderr)
            self._diag_obj.add_rule_issue(rule_name, msg)
            engine_error("rule_issue", rule=rule_name, issue=msg)

        # 参数类型不匹配：只打印提醒，不进诊断（保持原行为）
        for rule_name, msg in warnings:
            print(f"[Engine] [!!] 规则 \"{rule_name}\" {msg}", file=sys.stderr)

        print(f"[Engine] 规则校验完成: {valid}/{total} 条有效规则")

        if valid < total:
            self._alert_user(
                "规则配置异常",
                f"{total - valid} 条规则引用了未加载的插件或参数不匹配，请检查引擎日志",
            )

        return valid, total

    # ------------------------------------------------------------------
    # 事件分发
    # ------------------------------------------------------------------

    def emit_event(self, event_type: str, event_payload: Dict[str, Any]) -> None:
        """分发事件给匹配的规则。"""
        if self._shutdown_flag and self._shutdown_flag.is_set():
            print(f"[EventBus] 引擎正在关闭，忽略事件: [{event_type}]")
            return

        semantic = self.triggers_meta.get(event_type, {}).get("semantic", "oneshot")
        print(
            f"[EventBus] 收到广播事件: [{event_type}] ({semantic}) -> {event_payload}"
        )

        with self._rules_lock:
            # 事件分发沿用同步语义；这里刻意不持锁执行 action，慢动作不能卡住热重载。
            rules_snapshot = list(self.rules)

        for rule_index, rule in enumerate(rules_snapshot):
            rule_key = f"{rule_index}:{rule.get('name', '')}"
            if not self._condition_runtime.match(rule_key, rule, event_type, event_payload):
                continue

            rule_name = rule.get("name", "未命名规则")
            print(f"[EventBus] [OK] 匹配到规则: <{rule_name}>, 准备分发动作！")
            self._safe_on_event(
                "rule_triggered",
                {
                    "rule_name": rule_name,
                    "event_type": event_type,
                    "event_payload": event_payload,
                },
            )
            context = build_context(
                rule_name,
                event_type,
                event_payload,
                self._condition_runtime.last_match(rule_key),
            )
            self.execute_workflow(rule_key, rule, rule_name, context)

    def call_notmyfault(self, event_data: Dict[str, Any]) -> None:
        """接收外部事件（触发器线程通过此方法推送事件）。"""
        event_type = event_data.get("trigger_id")
        event_payload = event_data.get("triggered_params", {})
        self.emit_event(event_type, event_payload)

    def run_manual_rule(self, rule_index: int) -> tuple[bool, str]:
        """从 Dashboard 手动执行一条显式配置了 ``manual`` 触发器的规则。

        手动执行不能成为绕过条件的万能后门：只有规则条件树中声明了
        manual 触发器，才允许从列表直接启动。动作放入独立线程，HTTP 请求
        只负责受理，不会被长动作卡住。
        """
        with self._rules_lock:
            if rule_index < 0 or rule_index >= len(self.rules):
                return False, "规则不存在或已被重新加载"
            rule = self.rules[rule_index]

        if not any(event.get("type") == "manual" for event in get_rule_events(rule)):
            return False, "该规则未配置手动触发器"

        rule_name = rule.get("name", f"规则 #{rule_index + 1}")
        context = build_context(
            rule_name,
            "manual",
            {"source": "dashboard", "rule_index": rule_index},
            [],
        )
        self._safe_on_event("rule_triggered", {
            "rule_name": rule_name,
            "event_type": "manual",
            "event_payload": {"source": "dashboard"},
        })
        threading.Thread(
            target=self.execute_workflow,
            args=(f"manual:{rule_index}", rule, rule_name, context),
            name=f"ManualRule-{rule_index}",
            daemon=True,
        ).start()
        return True, "已开始执行"

    # ------------------------------------------------------------------
    # 动作执行
    # ------------------------------------------------------------------

    def execute_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        """执行规则工作流；前置条件不安全时延后，而不是冒险运行动作。"""
        ready, reason, retry_after = self._check_preconditions(
            rule.get("preconditions", []), context,
        )
        if not ready:
            delay = min(max(retry_after or 60, 5), 3600)
            self._safe_on_event(
                "workflow_deferred",
                {
                    "rule_name": rule_name,
                    "reason": reason,
                    "retry_after_seconds": delay,
                },
            )
            print(
                f"[Engine] 工作流 <{rule_name}> 前置条件未满足，{delay}s 后重试: {reason}",
                file=sys.stderr,
            )
            self._defer_workflow(workflow_key, rule, rule_name, context, delay)
            return
        self.execute_actions(rule.get("actions", []), rule_name, context)

    def _check_preconditions(
        self, preconditions: Any, context: Dict[str, Any],
    ) -> Tuple[bool, str, float | None]:
        """调用动作插件声明的 check_precondition()；未知/异常一律不放行。"""
        if not preconditions:
            return True, "", None
        if not isinstance(preconditions, list):
            return False, "preconditions 必须是数组", None
        for index, spec in enumerate(preconditions):
            if not isinstance(spec, dict):
                return False, f"preconditions[{index}] 必须是对象", None
            action_type = spec.get("type")
            module = self._plugin_modules.get(action_type)
            action_meta = self.actions_meta.get(action_type, {})
            check = getattr(module, "check_precondition", None)
            if action_type not in self.actions_funcs or not callable(check) \
                    or action_meta.get("precondition_api") != "context-v1":
                return False, f"前置条件插件不可用或未声明 context-v1: {action_type}", None
            try:
                params = resolve_templates(spec.get("params", {}), context)
                if not isinstance(params, dict):
                    return False, f"前置条件 {action_type} 的 params 必须是对象", None
                verdict = check(action_meta, params, context)
            except Exception:
                return False, f"前置条件 {action_type} 检查异常: {traceback.format_exc()[-200:]}", None
            if verdict is True:
                continue
            if isinstance(verdict, dict):
                if verdict.get("ok") is True:
                    continue
                return (
                    False,
                    str(verdict.get("reason") or f"前置条件 {action_type} 未满足"),
                    verdict.get("retry_after_seconds"),
                )
            return False, f"前置条件 {action_type} 未满足", None
        return True, "", None

    def _defer_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
        delay: float,
    ) -> None:
        with self._deferred_workflows_lock:
            existing = self._deferred_workflows.get(workflow_key)
            if existing is not None and existing.is_alive():
                return
            timer = threading.Timer(
                delay,
                self._resume_workflow,
                args=(workflow_key, rule, rule_name, context),
            )
            timer.daemon = True
            self._deferred_workflows[workflow_key] = timer
            timer.start()

    def _resume_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        with self._deferred_workflows_lock:
            self._deferred_workflows.pop(workflow_key, None)
        if self._shutdown_flag and self._shutdown_flag.is_set():
            return
        self.execute_workflow(workflow_key, rule, rule_name, context)

    def _cancel_deferred_workflows(self) -> None:
        with self._deferred_workflows_lock:
            timers = list(self._deferred_workflows.values())
            self._deferred_workflows.clear()
        for timer in timers:
            timer.cancel()

    def execute_actions(
        self,
        actions: List[Dict[str, Any]],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        """顺序执行一条规则的动作流水线，并把每一步产物写入 context。"""
        for index, action in enumerate(actions):
            # 步骤名由动作类型和位置自动生成，配置文件不保存也不接受用户自定义 ID。
            step_id = f"{action.get('type', 'action')}_{index + 1}"
            ok, result = self._run_action(action, rule_name, context)
            context["steps"][step_id] = {
                "status": "ok" if ok else "failed",
                "result": result if ok else None,
                "error": None if ok else str(result),
            }
            if not ok and action.get("on_error", "stop") != "continue":
                print(f"[Engine] 动作流水线在步骤 {step_id} 停止", file=sys.stderr)
                break

    def execute_action(
        self,
        action: Dict[str, Any],
        rule_name: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """兼容入口：执行一个动作并返回插件的结构化结果（旧调用可忽略返回值）。"""
        if context is None:
            context = build_context(rule_name, "", {}, [])
        _ok, result = self._run_action(action, rule_name, context)
        return result if _ok else None

    def _run_action(
        self,
        action: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> Tuple[bool, Any]:
        """执行单一步骤；新插件可拿上下文并返回结果，旧插件保持两参数 API。"""
        if self._shutdown_flag and self._shutdown_flag.is_set():
            print(f"[Engine] 正在关闭，跳过动作: {action.get('type', '?')}")
            return False, "引擎正在关闭"

        action_type = action.get("type")
        raw_params = action.get("params", {})
        params = resolve_templates(raw_params, context)
        if not isinstance(params, dict):
            return False, "action.params 必须是对象"

        if action_type not in self.actions_funcs:
            print(
                f"[Engine] [?] 未知 action 类型或未装载模块: {action_type}",
                file=sys.stderr,
            )
            return False, f"未知 action 类型: {action_type}"

        # --- 插件自定义参数校验 ---
        module = self._plugin_modules.get(action_type)
        if module is not None and hasattr(module, "validate_params"):
            try:
                validation_errors = module.validate_params(
                    self.actions_meta.get(action_type, {}), params
                )
                if validation_errors:
                    print(
                        f"[Engine] [!!] action \"{action_type}\" validate_params 警告:",
                        file=sys.stderr,
                    )
                    for ve in validation_errors:
                        print(f"         - {ve}", file=sys.stderr)
            except Exception:
                print(
                    f"[Engine] action \"{action_type}\" validate_params() 执行异常:",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)

        # 计数只管“正在跑几个”，真正的插件调用放在锁外，不然一个慢动作全员罚站。
        with self._action_lock:
            self._active_actions += 1

        try:
            action_meta = self.actions_meta.get(action_type, {})
            action_func = self.actions_funcs[action_type]
            retries = min(max(int(action.get("retry", 0) or 0), 0), 3)
            delay = max(float(action.get("retry_delay_seconds", 0) or 0), 0)
            for attempt in range(retries + 1):
                try:
                    result = invoke_action(action_func, module, action_meta, params, context)
                    self._diag_obj.inc_action_ok()
                    self._safe_on_event(
                        "action_executed",
                        {
                            "action_type": action_type,
                            "params": params,
                            "rule_name": rule_name,
                            "status": "ok",
                            "result": result,
                            "attempt": attempt + 1,
                        },
                    )
                    return True, result
                except Exception:
                    if attempt < retries:
                        print(
                            f"[Engine] action \"{action_type}\" 第 {attempt + 1} 次失败，准备重试",
                            file=sys.stderr,
                        )
                        if delay:
                            time.sleep(delay)
                        continue
                    raise
        except Exception:
            self._diag_obj.inc_action_fail()
            err_msg = traceback.format_exc()
            print(
                f"[Engine] [ERR] 执行 action \"{action_type}\" 失败:",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            engine_error("action_failed", action_type=action_type, rule_name=rule_name, error=str(err_msg[-500:]))
            self._safe_on_event(
                "error",
                {
                    "action_type": action_type,
                    "rule_name": rule_name,
                    "error": err_msg,
                },
            )
            return False, err_msg[-500:]
        finally:
            with self._action_lock:
                self._active_actions -= 1
            with self._action_done:
                self._action_done.notify_all()

    # ------------------------------------------------------------------
    # 触发器线程管理
    # ------------------------------------------------------------------

    def _start_trigger_threads(self, rules: List[Dict[str, Any]] | None = None) -> int:
        if rules is None:
            with self._rules_lock:
                rules = list(self.rules)

        # 同一个 trigger 只开一条线程，把所有规则参数打包给它，省得大家重复蹲点。
        aggregated = aggregate_trigger_params(rules)

        missing = [et for et in aggregated if et not in self.triggers_funcs]
        if missing:
            print(
                f"[Engine] [!!] 规则引用了未加载的触发器: {', '.join(missing)}",
                file=sys.stderr,
            )
            self._alert_user(
                "触发器缺失",
                f"以下触发器未装载，相关规则不会生效: {', '.join(missing)}",
            )

        count = 0
        for event_type, config_list in aggregated.items():
            if event_type not in self.triggers_funcs:
                continue

            trigger_meta = self.triggers_meta.get(event_type, {})
            trigger_func = self.triggers_funcs[event_type]
            trigger_event = threading.Event()
            thread = threading.Thread(
                target=self._run_trigger,
                args=(event_type, trigger_func, trigger_meta, config_list, trigger_event),
                daemon=True,
            )
            # 先登记再启动：避免 shutdown 与启动竞态漏掉事件信号
            with self._trigger_lock:
                self._trigger_events[event_type] = trigger_event
                self._trigger_threads[event_type] = thread
            thread.start()
            count += 1
            print(
                f"[Engine] 已启动触发器线程: {event_type}"
                f"（共监听 {len(config_list)} 条规则）"
            )

        return count

    def _stop_trigger_threads(self, timeout: float = 30.0) -> bool:
        """请求停止所有触发器，并报告它们是否全部退出。

        Python 线程不能被安全地强制终止；仍在运行的插件必须保留登记，
        这样热重载不会在同一触发器上再启动一代线程而造成重复执行。
        """
        with self._trigger_lock:
            if not self._trigger_threads:
                return True
            events = list(self._trigger_events.values())
            threads = list(self._trigger_threads.items())

        # 先广播“收工”，再 join；反过来等会儿基本就是和自己较劲。
        for evt in events:
            evt.set()

        deadline = time.time() + timeout
        for event_type, thread in threads:
            remaining = deadline - time.time()
            if remaining > 0:
                thread.join(timeout=remaining)
            if thread.is_alive():
                print(
                    f"[Engine] [!!] 触发器线程 {event_type} 未在 {timeout}s 内退出，强制终止",
                    file=sys.stderr,
                )

        alive = {event_type for event_type, thread in threads if thread.is_alive()}
        with self._trigger_lock:
            for event_type, _thread in threads:
                if event_type not in alive:
                    self._trigger_threads.pop(event_type, None)
                    self._trigger_events.pop(event_type, None)
        return not alive

    # ------------------------------------------------------------------
    # 引擎生命周期
    # ------------------------------------------------------------------

    def start(
        self, shutdown_event: "threading.Event | None" = None
    ) -> None:
        self._start_time = time.time()
        self._shutdown_flag = shutdown_event or threading.Event()

        # strict 模式（源码运行）：校验引擎核心源码完整性（engine.py/config.py 等），
        # 任何文件被改过就拒绝启动，免得有人改掉签名校验调用让插件签名形同虚设。
        # 冻结构建（PyInstaller）里没有 .py 源文件，跳过——靠编译产物 + 插件签名 +
        # build.json 签名防护。
        if self._security_mode == SecurityMode.STRICT and not getattr(sys, "frozen", False):
            ok, bad = verify_core_integrity()
            if not ok:
                detail = "；".join(bad[:5])
                print(f"[Engine] [!!] 核心文件完整性校验失败：{detail}", file=sys.stderr)
                engine_error("integrity_check_failed", files=",".join(bad))
                self._alert_user(
                    "NotmyFault 完整性校验失败",
                    f"核心文件可能被篡改（{detail}）。请重新运行 python build.py 生成完整性清单。",
                    open_dashboard=True,
                )
                return

        # 启动前先体检：问题规则会被诊断出来，但不因为一条坏规则饿死整台引擎。
        self._validate_all_rules()

        from Win_toaster.show_notification import show_notification
        from Win_toaster.AUMID_Register import register_toaster

        register_toaster()
        show_notification("NotmyFault 已加载", "")

        thread_count = self._start_trigger_threads()

        if thread_count == 0:
            print("[Engine] 没有找到可用触发器，程序将退出。")
            hint = "没有可用的触发器，请检查规则配置"
            if self._security_mode == SecurityMode.STRICT:
                # fresh clone 未跑 build.py：内置插件因无 signature.sig 被拒载。
                hint += (
                    "。当前为 strict 安全模式：若是源码运行，内置插件因缺少签名被拒载，"
                    "请先运行 `python build.py` 生成插件签名与 build.json。"
                )
            self._alert_user(
                "NotmyFault 启动失败",
                hint,
                open_dashboard=True,
            )
            return

        se = self._shutdown_flag
        config_mtime = (
            os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0
        )
        self._hot_reload_error_reported = False

        try:
            while not se.is_set():
                se.wait(1)

                try:
                    new_mtime = (
                        os.path.getmtime(CONFIG_FILE)
                        if os.path.exists(CONFIG_FILE)
                        else 0
                    )
                    if new_mtime > config_mtime:
                        # 文件保存可能正好写到一半，JSON 失败走下面的兜底，下个 tick 再来。
                        new_config = load_verified_config()
                        new_rules = new_config["rules"]

                        # 旧触发器还活着时绝不启动新一代，否则同一事件会被处理两次。
                        if not self._stop_trigger_threads(timeout=30):
                            message = "旧触发器未能在 30 秒内退出，已拒绝应用新配置"
                            if not self._hot_reload_error_reported:
                                self._diag_obj.inc_hot_reload_error()
                                engine_error("hot_reload_error", error=message)
                                print(f"[Engine] [!!] {message}", file=sys.stderr)
                                self._alert_user("热重载被拒绝", message)
                                self._hot_reload_error_reported = True
                            continue

                        with self._rules_lock:
                            old_rule_count = len(self.rules)
                            self.rules = new_rules
                            self._condition_runtime.reset()
                        self._cancel_deferred_workflows()

                        print(
                            f"[Engine] 配置已热加载（{old_rule_count} -> {len(new_rules)} 条规则）"
                        )
                        self._hot_reload_error_reported = False
                        self._validate_all_rules()

                        started = self._start_trigger_threads(new_rules)
                        if started == 0:
                            print("[Engine] 热加载后无可用触发器，保持引擎运行")
                        config_mtime = new_mtime
                except ConfigValidationError as e:
                    self._diag_obj.inc_hot_reload_error()
                    engine_error("hot_reload_error", error=str(e))
                    print(
                        f"[Engine] 热加载配置校验失败: {e}",
                        file=sys.stderr,
                    )
                    if not self._hot_reload_error_reported:
                        self._hot_reload_error_reported = True
                        self._alert_user(
                            "配置校验失败",
                            "config.json 未通过格式、签名或安全校验，热加载已拒绝；请修正后重新保存",
                        )
                    # 配置校验失败需要用户重新保存，避免每秒重复报同一个错误。
                    config_mtime = new_mtime
                except OSError as e:
                    print(
                        f"[Engine] 读取配置文件失败: {e}",
                        file=sys.stderr,
                    )
                except Exception:
                    print(
                        "[Engine] 热加载配置失败:",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
        except KeyboardInterrupt:
            print("[Engine] 主程序收到中断，退出中...")

        self._cancel_deferred_workflows()
        self._stop_trigger_threads(timeout=30)
        self._shutdown_plugins()
        self._wait_active_actions()

    def _shutdown_plugins(self) -> None:
        for plugin_id, module in self._plugin_modules.items():
            if hasattr(module, "teardown"):
                try:
                    print(f"[Engine] 调用 teardown: {plugin_id}")
                    module.teardown()
                except Exception:
                    print(
                        f"[Engine] teardown \"{plugin_id}\" 异常:",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                    self._diag_obj.record_plugin_error(
                        "teardown", plugin_id, f"teardown 异常: {traceback.format_exc()[-200:]}"
                    )
                    engine_error(
                        "teardown_failed", plugin=plugin_id, error=traceback.format_exc()[-500:]
                    )

    def _wait_active_actions(self, timeout: float = 60.0) -> None:
        print("[Engine] 正在关闭，等待活跃动作完成...")
        deadline = time.time() + timeout
        while True:
            with self._action_lock:
                remaining = self._active_actions
            if remaining == 0:
                break
            if time.time() >= deadline:
                print(
                    f"[Engine] [!!] shutdown: {remaining} active action(s) still "
                    f"running after {timeout}s, forcing exit",
                    file=sys.stderr,
                )
                break
            print(f"[Engine] 等待 {remaining} 个活跃动作完成...")
            with self._action_done:
                self._action_done.wait(timeout=3)
        print("[Engine] 所有动作已完成，引擎安全关闭")

    def shutdown(self) -> None:
        
        # shutdown 可能被 API、信号和 finally 同时喊到；每一步都尽量可重复。
        if self._shutdown_flag:
            self._shutdown_flag.set()
        with self._trigger_lock:
            events = list(self._trigger_events.values())
        for evt in events:
            evt.set()
        self._cancel_deferred_workflows()
        self._stop_trigger_threads(timeout=30)
        self._shutdown_plugins()
        self._wait_active_actions()
