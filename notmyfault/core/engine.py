import copy
import os
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from notmyfault.core.diagnostics import Diagnostics
from notmyfault.config import ConfigValidationError
from notmyfault.core.event_bus import EventBus
from notmyfault.core.rule_watcher import RuleWatcher
from notmyfault.core.logging import engine_error, engine_info, engine_warn
from notmyfault.core import plugin_worker
from notmyfault.security.plugin_loader import (
    PluginLoader,
    PluginRegistry,
)
from notmyfault.core.plugin_runtime import PluginRuntime
from notmyfault.core.generation_manager import GenerationManager
from notmyfault.core.run_manager import RunManager
from notmyfault.core.event_stream import Event, EventStream
from notmyfault.core.engine_events import use_stream
from notmyfault.core.rules import (
    get_rule_events,
    validate_rules,
)
from notmyfault.core.rule_model import normalize_rules
from notmyfault.core.runtime_rules import PreparedRules, rule_key
from notmyfault.security.plugin_schema import check_payload_contract
from notmyfault.security.security import (
    SecurityMode,
    detect_security_mode as _detect_security_mode,
    verify_core_integrity,
)
from notmyfault.core.trigger_host import TriggerHost
from notmyfault.core.state_store import StateStore
from pathlib import Path
from notmyfault.platform.boot_identity import boot_id
from notmyfault.core.workflow import build_context
from notmyfault.core.binding_schema import prepare_binding_context
from notmyfault.core.bindings import BindingResolutionError
from notmyfault.core.data_types import DataTypeError, normalize_fields
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.workflow_executor import WorkflowExecutor


class RulesStorePort(Protocol):
    @property
    def rules_path(self) -> str: ...

    @property
    def plugin_manifest_path(self) -> str: ...

    def load_verified_rules(self) -> List[Dict[str, Any]]: ...


class _PluginErrorReporter:
    """插件错误记进诊断的同时发给 Dashboard，页面不用等下次轮询"""

    def __init__(self, diagnostics: Any, emit: Callable[..., None]) -> None:
        self._diagnostics = diagnostics
        self._emit = emit

    def record_plugin_error(self, store: str, plugin_id: str, reason: str) -> None:
        self._diagnostics.record_plugin_error(store, plugin_id, reason)
        self._emit(
            "plugin.load_failed",
            {"store": store, "plugin_id": plugin_id, "reason": reason, "error": {"code": "plugin_load_failed", "message": reason, "location": plugin_id}},
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._diagnostics, name)


class AutomationEngine:
    """加载插件、匹配规则并执行动作"""

    def __init__(
        self,
        config: Dict[str, Any],
        on_event: Optional[Callable[[Event], None]] = None,
        *,
        rules_store: RulesStorePort,
    ) -> None:
        # 热重载只替换 rules，构造函数传入的 config 保持原对象
        self.config = config
        self._rules_store = rules_store
        self._rules_lock = threading.RLock()
        self.rules: List[Dict[str, Any]] = config.get("rules", [])
        self.events = EventStream()
        if on_event is not None:
            self.events.subscribe(on_event)

        self._plugin_registry = PluginRegistry()
        # 旧插件、Dashboard 和测试仍直接读取这些注册表对象
        self.triggers_meta = self._plugin_registry.triggers_meta
        self.triggers_funcs = self._plugin_registry.triggers_funcs
        self.actions_meta = self._plugin_registry.actions_meta
        self.actions_funcs = self._plugin_registry.actions_funcs
        self._plugin_modules = self._plugin_registry.modules
        self._plugin_shutdown_lock = threading.RLock()
        self._plugins_shutdown = False

        from notmyfault.security import sudo as _sudo
        self._sudo = _sudo
        self._engine_token: str = _sudo.begin_engine_session()
        self._privilege_session_closed = False
        self._close_lock = threading.Lock()
        self._shutdown_clean = True
        self._runtime_cleanup_lock = threading.RLock()
        self._runtime_cleaned = False
        self._security_mode = _detect_security_mode()
        engine_info(f"Security mode: {self._security_mode.value}")
        self._plugin_integrity_errors: list[str] = []

        self._diag_obj = Diagnostics()

        self._shutdown_flag: "threading.Event | None" = None

        self._state_store = None
        self._state_store_lock = threading.Lock()
        self._trigger_host = TriggerHost(
            self.emit_event, self.events, self._get_state_store,
            alert=lambda title, message: self._alert_user(title, message),
            boot_id=boot_id(), engine_started_at=time.time(),
            on_failure=self._diag_obj.record_trigger_crash,
        )
        self._condition_stop = threading.Event()
        self._condition_wake = threading.Event()
        self._condition_thread = None

        self._start_time: float = 0.0

        self._plugin_loader = PluginLoader(
            registry=self._plugin_registry,
            config=self.config,
            diagnostics=_PluginErrorReporter(self._diag_obj, self._safe_on_event),
            security_mode=self._security_mode,
            sudo=self._sudo,
            engine_token=self._engine_token,
            integrity_errors=self._plugin_integrity_errors,
            plugin_manifest_path=rules_store.plugin_manifest_path,
        )
        self._plugins = PluginRuntime(self._plugin_registry, self._plugin_loader)
        # rules 和 meta 走 lambda 运行时取值，锁和条件运行时传同一个对象
        self._event_bus = EventBus(
            snapshot_fn=self._get_runtime_snapshot,
            rules_lock=self._rules_lock,
            triggers_meta_fn=lambda: self.triggers_meta,
            actions_meta_fn=lambda: self.actions_meta,
            is_shutdown_fn=lambda: bool(
                self._shutdown_flag and self._shutdown_flag.is_set()
            ),
            safe_on_event=self._safe_on_event,
            execute_workflow_cb=self.execute_workflow,
            scheduler_submit_fn=lambda *args: self._run_manager.submit(*args),
            alert_fn=lambda title, message: self._alert_user(title, message),
        )
        self._workflow_executor = WorkflowExecutor(
            actions_meta=lambda: self.actions_meta,
            triggers_meta=lambda: self.triggers_meta,
            actions_funcs=lambda: self.actions_funcs,
            plugin_modules=lambda: self._plugin_modules,
            diagnostics=self._diag_obj,
            shutdown_event=lambda: self._shutdown_flag,
            on_event=self._safe_on_event,
            action_resolver=self._plugin_registry.resolve_action,
            sensitive_values=self._collect_sensitive_values,
        )
        # 同一规则反复触发时先到这里决定丢弃、排队还是直接跑
        self._run_manager = RunManager(
            execute_fn=self._workflow_executor.execute_workflow,
            on_event=self._safe_on_event,
        )
        self._generations = GenerationManager(self._plugins, self._run_manager, self.events, lock=self._rules_lock,
                                              reconcile=self._trigger_host.reconcile, on_change=self._condition_wake.set)
        # isolated 动作崩了通过这个回调推 plugin_worker_crashed 事件
        plugin_worker.set_crash_callback(self._safe_on_event)
        # 旧扩展和测试仍直接读取这些同步对象
        self._action_lock = self._workflow_executor.action_lock
        self._action_done = self._workflow_executor.action_done
        self._rule_watcher = RuleWatcher(
            lambda: self._rules_store.rules_path, self._rules_store.load_verified_rules,
            self.apply_rules, on_error=self._watcher_error,
        )

    def _get_state_store(self):
        with self._state_store_lock:
            if self._state_store is None:
                self._state_store = StateStore(Path(self._rules_store.rules_path).parent / "runtime.sqlite3")
            return self._state_store

    def _watcher_error(self, error, stage):
        self._diag_obj.add_rule_issue("", str(error))
        if stage == "read":
            self.events.publish("rules.rejected", {"source": "file", "error": {"code": "rules_read_failed", "message": str(error)}})
        self._alert_user("规则更新失败", str(error))

    @property
    def rules(self):
        manager = getattr(self, "_generations", None)
        generation = manager.current if manager is not None else None
        return generation.snapshot.rules if generation is not None else self._rules

    @rules.setter
    def rules(self, rules):
        normalized = normalize_rules(rules)
        manager = getattr(self, "_generations", None)
        if manager is not None and manager.current is not None:
            manager.apply(normalized, "assignment", activate=False, validate=False)
        else:
            self._rules = normalized

    @property
    def _condition_runtime(self):
        return self._get_runtime_snapshot().runtime

    def _get_runtime_snapshot(self):
        generation = self._generations.current
        if generation is None:
            generation = self._generations.ensure_initial(self._rules)
        return generation.snapshot

    def _prepare_rules(self, rules):
        try:
            return self._generations.prepare(rules)
        except (ValueError, DataTypeError, BindingResolutionError) as error:
            raise ConfigValidationError(str(error)) from error

    def apply_rules(self, document, source="api"):
        generation = self._generations.apply(document, source)
        self._report_rule_issues(generation.snapshot)
        if source == "api":
            self._rule_watcher.acknowledge()
        return generation

    def _report_rule_issues(self, prepared: PreparedRules) -> None:
        self._diag_obj.reset_rule_issues()
        for name, message in prepared.issues:
            self._diag_obj.add_rule_issue(name, message)
            engine_error("rule_issue", rule=name, issue=message)
            print(f'[Engine] 已跳过规则 "{name}": {message}', file=sys.stderr)
        for name, message in prepared.warnings:
            engine_warn(f'规则 "{name}": {message}')
        if prepared.issues:
            self._alert_user(
                "部分规则不可用",
                f"已跳过 {prepared.total - len(prepared)} 条规则，引擎继续运行。请在 Dashboard 查看规则错误并修正。",
            )

    @property
    def _active_actions(self) -> int:
        return self._workflow_executor.active_actions

    @staticmethod
    def _alert_user(title: str, message: str, open_dashboard: bool = False) -> None:
        """向用户发送告警，失败时记录日志"""
        try:
            from notmyfault.host.alert import alert_user
            alert_user(title, message, open_dashboard=open_dashboard)
        except Exception as e:
            engine_warn(f"alert_user 调用失败 ({title}): {e}")

    def _safe_on_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        self.events.publish(event_type, payload)

    def scheduler_stats(self) -> Dict[str, Dict[str, int]]:
        """每个规则当前的运行数和排队数，状态接口展示用"""
        return self._run_manager.stats()

    def get_diagnostics(self) -> Dict[str, Any]:
        """返回当前诊断数据供 Dashboard 展示"""
        uptime = time.time() - self._start_time if self._start_time > 0 else 0
        snap = self._diag_obj.snapshot()
        total_actions = snap["action_ok"] + snap["action_fail"]
        return {
            "uptime_seconds": round(uptime, 1),
            "plugins": {
                "actions_loaded": len(self.actions_funcs),
                "triggers_loaded": len(self.triggers_funcs),
                "errors": snap["plugin_errors"],
                "error_count": len(snap["plugin_errors"]),
                "admin_plugins": self._sudo.get_authorized_plugins(),
                "integrity_errors": self._plugin_integrity_errors[-10:],
            },
            "rules": {
                "total": self._get_runtime_snapshot().total,
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
            "triggers": {
                "health": self._trigger_host.health(),
            },
            "errors": snap["errors"][-20:],
        }

    def auto_load(self, load_paths) -> None:
        """扫描并加载插件，支持多个目录或单个目录参数"""
        if isinstance(load_paths, str):
            # 旧调用只传一个目录时标记为 builtin
            load_paths = [(load_paths, "builtin")]
        engine_info("=== SESSION_START ===")
        t_loaded = t_failed = a_loaded = a_failed = 0
        for base_dir, origin in load_paths:
            _t, _tf = self._plugin_loader.load(
                base_dir=os.path.join(base_dir, "triggers"),
                kind="trigger",
                origin=origin,
            )
            _a, _af = self._plugin_loader.load(
                base_dir=os.path.join(base_dir, "actions"),
                kind="action",
                origin=origin,
            )
            t_loaded += _t; t_failed += _tf
            a_loaded += _a; a_failed += _af

        print(
            f"\n[Engine] 已加载 {a_loaded} 个动作插件, {t_loaded} 个触发器插件"
        )

        total_failed = t_failed + a_failed
        if total_failed > 0:
            parts = []
            if t_failed > 0:
                parts.append(f"{t_failed} 个触发器")
            if a_failed > 0:
                parts.append(f"{a_failed} 个动作")
            fail_msg = "、".join(parts) + " 插件加载失败，请检查Engine日志"
            print(f"[Engine] [!!] {fail_msg}", file=sys.stderr)
            self._alert_user("插件加载异常", fail_msg)

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

    @property
    def extensions(self):
        return self._plugin_registry.extensions

    def extension_handler(self, plugin_id: str, command_id: str):
        handler = self.extensions.handler(plugin_id, command_id)
        if handler is not None:
            return handler
        if plugin_id in self.actions_meta:
            self._plugin_registry.resolve_action(plugin_id)
        return self.extensions.handler(plugin_id, command_id)

    def check_plugin_load(
        self,
        plugin_kind: str,
        plugin_id: str,
        folder_path: str,
    ) -> bool:
        """导入指定插件并运行 setup，入口或附加模块加载失败时返回 False。"""
        loaded_root = self._plugin_registry.plugin_roots.get(plugin_id)
        if not loaded_root or os.path.realpath(loaded_root) != os.path.realpath(
            folder_path
        ):
            return False
        try:
            if plugin_kind == "trigger":
                entry = self._plugin_registry.resolve_trigger(plugin_id)
            elif plugin_kind == "action":
                entry = self._plugin_registry.resolve_action(plugin_id)
                if entry is not None and self.actions_meta.get(plugin_id, {}).get("execution_mode") == "isolated":
                    entry.check_load()
            else:
                raise ValueError(f"未知插件类型: {plugin_kind}")
        except Exception as error:
            if plugin_kind == "action" and self.actions_meta.get(plugin_id, {}).get("execution_mode") == "isolated":
                self._plugin_registry.unregister("action", plugin_id)
            _PluginErrorReporter(self._diag_obj, self._safe_on_event).record_plugin_error(
                plugin_kind.title(),
                plugin_id,
                f"插件导入异常: {error}",
            )
            return False
        if entry is None:
            return False
        meta_store, _func_store = self._plugin_registry.stores(plugin_kind)
        contributes = meta_store.get(plugin_id, {}).get("contributes")
        if not isinstance(contributes, dict):
            return True
        return all(
            callable(self.extensions.handler(plugin_id, command["id"]))
            for command in contributes.get("commands", [])
        )

    def _validate_all_rules(self) -> Tuple[int, int]:
        """校验规则引用和参数并记录问题"""
        # 复制规则列表后再校验，热重载线程可以同时准备下一份配置
        with self._rules_lock:
            rules = list(self.rules)

        self._diag_obj.reset_rule_issues()
        valid, total, issues, warnings = validate_rules(
            rules, self.triggers_meta, self.actions_meta
        )

        # 引用问题写入诊断和日志，Dashboard 可以查看
        for rule_name, msg in issues:
            print(f"[Engine] [!!] 规则 \"{rule_name}\" {msg}", file=sys.stderr)
            self._diag_obj.add_rule_issue(rule_name, msg)
            engine_error("rule_issue", rule=rule_name, issue=msg)

        # 参数类型不匹配只打印提醒，保持原有行为
        for rule_name, msg in warnings:
            print(f"[Engine] [!!] 规则 \"{rule_name}\" {msg}", file=sys.stderr)

        print(f"[Engine] 规则校验完成: {valid}/{total} 条有效规则")

        if valid < total:
            self._alert_user(
                "规则配置异常",
                f"{total - valid} 条规则引用了未加载的插件或参数不匹配，请检查Engine日志",
            )

        return valid, total

    # 事件分发逻辑在 EventBus 里，保留老名字，触发器线程和测试直接调用
    def emit_event(
        self,
        event_type: str,
        event_payload: Dict[str, Any],
        instance: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._event_bus.emit_event(event_type, event_payload, instance=instance)

    def call_notmyfault(self, event_data: Dict[str, Any]) -> None:
        self._event_bus.call_notmyfault(event_data)

    def _mask_event_payload(
        self, event_type: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self._event_bus._mask_event_payload(event_type, payload)

    def _collect_sensitive_values(self, context: Dict[str, Any]) -> set:
        return self._event_bus._collect_sensitive_values(context)

    def run_manual_rule(self, rule_index: int) -> tuple[bool, str] | tuple[bool, str, str]:
        """从 Dashboard 立即执行指定规则"""
        with self._rules_lock:
            if rule_index < 0 or rule_index >= len(self.rules):
                return False, "规则不存在或已被重新加载"
            rule = copy.deepcopy(self.rules[rule_index])
            generation = self._get_runtime_snapshot().generation
            generation.retain()
        try:
            return self.run_manual_rule_snapshot(rule, rule_index, _generation=generation)
        finally:
            generation.release()

    def run_manual_rule_snapshot(
        self,
        rule: Dict[str, Any],
        rule_index: int = -1,
        trigger_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
        event_payload: Optional[Dict[str, Any]] = None,
        step_outputs: Optional[Dict[str, Any]] = None,
        start_step_id: str = "",
        end_step_id: str = "",
        test_assertions: Optional[List[Dict[str, Any]]] = None,
        variable_values: Optional[Dict[str, Any]] = None,
        _generation=None,
    ) -> tuple[bool, str, str]:
        """执行调用方传入的规则快照"""
        rule = normalize_rules([rule])[0]

        rule_name = rule.get("name", f"规则 #{rule_index + 1}")
        rule_id = rule.get("rule_id", "")
        with self._rules_lock:
            generation = _generation or self._generations.current
            if generation is not None and rule_id in generation.snapshot.compiled:
                generation.retain()
            else:
                if generation is not None and rule_index >= 0:
                    return False, "规则不存在或已被重新加载", f"run_{uuid.uuid4().hex}"
                generation = None
        try:
            run_id = f"run_{uuid.uuid4().hex}"
            manual_payload = (
                dict(event_payload)
                if isinstance(event_payload, dict)
                else {"source": "dashboard", "rule_index": rule_index}
            )
            context = build_context(
                rule_name,
                "manual",
                manual_payload,
                [],
                rule_id,
                run_id,
            )
            actions = rule.get("actions", [])
            if not isinstance(actions, list):
                actions = []
            try:
                prepared = self._generations.prepare([rule], validate=False)
            except (DataTypeError, BindingResolutionError, ValueError) as error:
                return False, str(error), run_id
            plugins = prepared.plugins
            compiled = prepared.compiled[rule_id]
            rule = compiled.source
            start_index = compiled.top_level_index.get(start_step_id, 0)
            end_index = compiled.top_level_index.get(end_step_id, len(actions) - 1)
            context["_plugins"] = plugins
            context["_compiled_rule"] = compiled
            context["_compiled_refs"] = compiled.references
            selected_count = max(0, end_index - start_index + 1) if actions else 0
            context["manual_test"] = {
                "start_step_id": start_step_id,
                "end_step_id": end_step_id,
                "start_index": start_index,
                "end_index": end_index,
                "assertions": copy.deepcopy(test_assertions or []),
            }
            if isinstance(step_outputs, dict):
                for action_index, action in enumerate(actions[:start_index]):
                    if not isinstance(action, dict):
                        continue
                    step_id = action.get("binding_id")
                    if not isinstance(step_id, str) or step_id not in step_outputs:
                        continue
                    record = {
                        "type": action.get("type", "action"),
                        "status": "ok",
                        "result": copy.deepcopy(step_outputs[step_id]),
                        "error": None,
                        "provided": True,
                    }
                    context["steps"][step_id] = record
            supplied_trigger_payloads = trigger_payloads if isinstance(trigger_payloads, dict) else {}
            context["triggers"] = {
                event.get("binding_id"): {
                    "type": event.get("type", ""),
                    "payload": copy.deepcopy(
                        supplied_trigger_payloads.get(event.get("binding_id"), {})
                    ),
                    "config": copy.deepcopy(event.get("params", {})),
                }
                for event in get_rule_events(rule)
                if isinstance(event.get("binding_id"), str)
            }
            context["manual_test"]["variable_values"] = variable_values
            try:
                prepare_binding_context(rule, context, plugins.triggers_meta, plugins.actions_meta)
            except (DataTypeError, BindingResolutionError) as error:
                return False, str(error), run_id
            if generation is not None:
                if generation.snapshot.compiled[rule_id].digest != compiled.digest:
                    return False, "规则运行版本已变化，请刷新后重试", run_id
                context["_generation"] = generation
            else:
                context["_standalone_snapshot"] = True
            self._safe_on_event("run.triggered", {
                "rule_id": rule_id,
                "run_id": run_id,
                "rule_name": rule_name,
                "event_type": "manual",
                "action_count": selected_count,
                "start_step_id": start_step_id,
                "end_step_id": end_step_id,
                "assertion_count": len(test_assertions or []),
                "event_payload": manual_payload,
            })
            decision = self._run_manager.submit(rule_id, rule, rule_name, context)
            if decision == "queued":
                return True, "已加入队列", run_id
            if decision == "dropped":
                return False, "当前规则不允许新的运行", run_id
            return True, "已开始执行", run_id
        finally:
            if generation is not None:
                generation.release()

    def execute_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        context["_plugins"] = self._plugins.snapshot(normalize_rules([rule]))
        return self._run_manager.execute_sync(
            workflow_key, rule, rule_name, context
        )

    def cancel_run(self, run_id: str) -> bool:
        return self._run_manager.cancel_run(run_id)

    def execute_actions(
        self,
        actions: List[Dict[str, Any]],
        rule_name: str,
        context: Dict[str, Any],
    ) -> bool:
        return self._workflow_executor.execute_actions(actions, rule_name, context)

    def execute_action(
        self,
        action: Dict[str, Any],
        rule_name: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return self._workflow_executor.execute_action(action, rule_name, context)

    def _run_action(
        self,
        action: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> Tuple[bool, Any]:
        return self._workflow_executor.run_action(action, rule_name, context)

    def _start_trigger_threads(self, rules: PreparedRules | None = None) -> int:
        snapshot = rules if rules is not None else self._get_runtime_snapshot()
        generation = self._generations.apply_prepared(snapshot, "initial")
        return len(generation.snapshot.subscription_counts)

    def _stop_trigger_threads(self, timeout: float = 30.0) -> bool:
        return self._trigger_host.stop(timeout)

    def start(
        self, shutdown_event: "threading.Event | None" = None
    ) -> None:
        """运行引擎并在结束时撤销本代授权"""
        if self._privilege_session_closed:
            raise RuntimeError("Engine实例已经关闭，不能再次启动")
        try:
            self._run(shutdown_event=shutdown_event)
        finally:
            if self._shutdown_clean:
                self.close()

    def close(self) -> None:
        """撤销本代引擎权限会话并允许重复调用"""
        with self._close_lock:
            if self._privilege_session_closed:
                return
            self._sudo.end_engine_session(self._engine_token)
            self._privilege_session_closed = True

    def _run(
        self, shutdown_event: "threading.Event | None" = None
    ) -> None:
        self._start_time = time.time()
        self._shutdown_flag = shutdown_event or threading.Event()
        try:
            if self._security_mode == SecurityMode.STRICT and not getattr(sys, "frozen", False):
                ok, bad = verify_core_integrity()
                if not ok:
                    detail = "；".join(bad[:5])
                    print(f"[Engine] [!!] 核心文件完整性校验失败：{detail}", file=sys.stderr)
                    engine_error("integrity_check_failed", files=",".join(bad))
                    self._alert_user(
                        "NotmyFault 完整性校验失败",
                        f"安装文件缺失、损坏或被修改（{detail}）。请从可信来源重新安装 NotmyFault。",
                        open_dashboard=True,
                    )
                    raise RuntimeError("核心文件完整性校验失败")

            try:
                self._rule_watcher.acknowledge()
                snapshot = self._prepare_rules(self._rules_store.load_verified_rules())
            except ConfigValidationError as error:
                self._diag_obj.add_rule_issue("", str(error))
                engine_error("rule_issue", issue=str(error))
                self._alert_user("NotmyFault 启动失败", str(error), open_dashboard=True)
                raise
            self._report_rule_issues(snapshot)

            from notmyfault.platform.platform_support import show_notification

            if os.name == "nt":
                try:
                    from Win_toaster.AUMID_Register import register_toaster
                    register_toaster()
                except Exception as error:
                    engine_error("notification_registration_failed", error_type=type(error).__name__)

            show_notification("NotmyFault 已加载", "")

            thread_count = self._start_trigger_threads(snapshot)

            if thread_count == 0:
                print("[Engine] 当前没有可用触发器，保持运行并等待规则更新")

            se = self._shutdown_flag
            self._rule_watcher.start()
            self._condition_stop.clear()
            self._condition_thread = threading.Thread(target=self._wait_conditions, name="ConditionDeadlines", daemon=True)
            self._condition_thread.start()
            try:
                se.wait()
            except KeyboardInterrupt:
                print("[Engine] 主程序收到中断，退出中...")
        finally:
            self._cleanup_runtime()

    def _wait_conditions(self):
        while not self._condition_stop.is_set():
            self._condition_wake.clear()
            self._event_bus.process_timers()
            with self._rules_lock:
                runtime = self._get_runtime_snapshot().runtime
                deadline = runtime.next_deadline()
                timeout = min(threading.TIMEOUT_MAX, max(0, deadline - runtime.clock.monotonic())) if deadline is not None else None
            if not self._condition_stop.is_set():
                runtime.clock.wait(self._condition_wake, timeout)

    def _cleanup_runtime(self, timeout: float = 90.0) -> None:
        with self._runtime_cleanup_lock:
            if self._runtime_cleaned:
                return
            from notmyfault.security.admin_prompt import cancel_pending_admin_requests

            cancel_pending_admin_requests()
            self._condition_stop.set()
            self._condition_wake.set()
            self._rule_watcher.stop()
            if self._condition_thread is not None and self._condition_thread is not threading.current_thread():
                self._condition_thread.join(2)
            self._run_manager.shutdown()
            self._trigger_host.request_stop_all()
            deadline = time.monotonic() + max(0.0, timeout)
            stopped = self._stop_trigger_threads(timeout=min(30.0, max(0.0, timeout)))
            scheduled_stopped, drained = self._wait_runtime_work(
                timeout=max(0.0, deadline - time.monotonic())
            )
            self._shutdown_clean = bool(stopped and scheduled_stopped and drained)
            plugin_worker.shutdown_all()
            self.close()
            if not self._shutdown_clean:
                return
            self._shutdown_plugins()
            if self._state_store is not None and not self._state_store.close():
                self._shutdown_clean = False
                return
            self._runtime_cleaned = True

    def _shutdown_plugins(self) -> None:
        # 先在锁内认领清理权，再在锁外调用插件 teardown()
        with self._plugin_shutdown_lock:
            if self._plugins_shutdown:
                return
            self._plugins_shutdown = True
            modules = list(self._plugin_modules.items())

        for plugin_id, module in modules:
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
        self.events.flush()
        self.events.close()
        self._plugin_registry.clear()

    def _wait_active_actions(self, timeout: float = 60.0) -> bool:
        return self._workflow_executor.wait_active_actions(timeout=timeout)

    def _wait_runtime_work(self, timeout: float = 60.0) -> tuple[bool, bool]:
        deadline = time.monotonic() + timeout
        scheduled_stopped = self._run_manager.wait_for_idle(timeout=timeout)
        remaining = max(deadline - time.monotonic(), 0.0)
        drained = self._wait_active_actions(timeout=remaining)
        return scheduled_stopped, drained

    def shutdown(self, timeout: float = 90.0) -> None:
        # API、信号和 finally 可能同时调用 shutdown()，每个清理步骤都支持重复执行
        if self._shutdown_flag is not None:
            self._shutdown_flag.set()
        self._cleanup_runtime(timeout=timeout)
        if self._shutdown_clean:
            self.close()
