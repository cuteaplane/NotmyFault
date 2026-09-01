import copy
import os
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from notmyfault.core.diagnostics import Diagnostics
from notmyfault.core.event_bus import EventBus
from notmyfault.core.hot_reloader import RulesHotReloader
from notmyfault.core.logging import engine_error, engine_info, engine_warn
from notmyfault.core import plugin_worker
from notmyfault.security.plugin_loader import (
    PluginLoader,
    PluginRegistry,
)
from notmyfault.core.rule_scheduler import RuleScheduler
from notmyfault.core.rules import (
    ConditionRuntime,
    aggregate_trigger_params,
    get_rule_events,
    validate_rules,
)
from notmyfault.security.plugin_schema import check_payload_contract
from notmyfault.security.security import (
    SecurityMode,
    detect_security_mode as _detect_security_mode,
    verify_core_integrity,
)
from notmyfault.core.trigger_supervisor import TriggerSupervisor
from notmyfault.core.workflow import build_context
from notmyfault.core.workflow_executor import WorkflowExecutor


class RulesStorePort(Protocol):
    @property
    def rules_path(self) -> str: ...

    @property
    def plugin_manifest_path(self) -> str: ...

    def load_verified_rules(self) -> List[Dict[str, Any]]: ...


class AutomationEngine:
    """加载插件、匹配规则并执行动作"""

    def __init__(
        self,
        config: Dict[str, Any],
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        *,
        rules_store: RulesStorePort,
    ) -> None:
        # 热重载只替换 rules，构造函数传入的 config 保持原对象
        self.config = config
        self._rules_store = rules_store
        self.rules: List[Dict[str, Any]] = config.get("rules", [])
        self.on_event = on_event

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
        # 旧代码仍读取 _diag 和 _diag_lock，保留这两个兼容属性
        self._diag: Dict[str, Any] = self._diag_obj.data
        self._diag_lock = self._diag_obj.lock

        self._shutdown_flag: "threading.Event | None" = None

        self._rules_lock = threading.RLock()
        # 热重载时整体替换条件运行时，保留 AND 分支的最近命中
        self._condition_runtime = ConditionRuntime()

        self._trigger_supervisor = TriggerSupervisor(
            # 运行时查找 _alert_user，构造后替换的告警回调仍然生效
            alert_cb=lambda title, message: self._alert_user(title, message),
        )

        self._start_time: float = 0.0

        self._plugin_loader = PluginLoader(
            registry=self._plugin_registry,
            config=self.config,
            diagnostics=self._diag_obj,
            security_mode=self._security_mode,
            sudo=self._sudo,
            engine_token=self._engine_token,
            integrity_errors=self._plugin_integrity_errors,
            plugin_manifest_path=rules_store.plugin_manifest_path,
        )
        # rules 和 meta 走 lambda 运行时取值，锁和条件运行时传同一个对象
        self._event_bus = EventBus(
            rules_fn=lambda: self.rules,
            rules_lock=self._rules_lock,
            condition_runtime=self._condition_runtime,
            triggers_meta_fn=lambda: self.triggers_meta,
            actions_meta_fn=lambda: self.actions_meta,
            is_shutdown_fn=lambda: bool(
                self._shutdown_flag and self._shutdown_flag.is_set()
            ),
            safe_on_event=self._safe_on_event,
            execute_workflow_cb=self.execute_workflow,
            scheduler_submit_fn=lambda *args: self._rule_scheduler.submit(*args),
        )
        self._workflow_executor = WorkflowExecutor(
            actions_meta=lambda: self.actions_meta,
            actions_funcs=lambda: self.actions_funcs,
            plugin_modules=lambda: self._plugin_modules,
            diagnostics=self._diag_obj,
            shutdown_event=lambda: self._shutdown_flag,
            on_event=self._safe_on_event,
            defer_workflow=lambda *args: self._defer_workflow(*args),
            resume_workflow=lambda *args: self._resume_workflow(*args),
            execute_workflow=lambda *args: self.execute_workflow(*args),
            execute_actions=lambda *args: self.execute_actions(*args),
            run_action=lambda *args: self._run_action(*args),
            action_resolver=self._plugin_registry.resolve_action,
            sensitive_values=self._collect_sensitive_values,
        )
        # 同一规则反复触发时先到这里决定丢弃、排队还是直接跑
        self._rule_scheduler = RuleScheduler(
            execute_fn=self.execute_workflow,
            cancel_run_fn=self._workflow_executor.cancel_run,
            is_deferred_fn=self._workflow_executor.is_run_deferred,
            on_history_event=self._safe_on_event,
            prepare_run_fn=self._workflow_executor.prepare_run,
        )
        # isolated 动作崩了通过这个回调推 plugin_worker_crashed 事件
        plugin_worker.set_crash_callback(self._safe_on_event)
        # 旧扩展和测试仍直接读取这些同步对象
        self._action_lock = self._workflow_executor.action_lock
        self._action_done = self._workflow_executor.action_done
        self._deferred_workflows = self._workflow_executor.deferred_workflows
        self._deferred_workflows_lock = (
            self._workflow_executor.deferred_workflows_lock
        )
        self._hot_reloader = RulesHotReloader(
            rules_path_fn=lambda: self._rules_store.rules_path,
            load_rules_fn=self._rules_store.load_verified_rules,
            stop_triggers_fn=self._stop_trigger_threads,
            apply_rules_fn=self._apply_hot_reload_rules,
            cancel_deferred_fn=self._cancel_deferred_workflows,
            validate_rules_fn=self._validate_all_rules,
            start_triggers_fn=self._start_trigger_threads,
            diagnostics=self._diag_obj,
            alert_cb=lambda title, message: self._alert_user(title, message),
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
        """调用外部事件回调并记录回调异常"""
        if event_type in ("workflow_completed", "workflow_failed"):
            scheduler = getattr(self, "_rule_scheduler", None)
            if scheduler is not None:
                # deferred 的 run 靠终态事件退场，排队中的下一条也在这里补发
                scheduler.on_run_event(event_type, payload.get("run_id"))
        if not self.on_event:
            return
        # UI 或 SSE 推送失败时记录错误并继续分发
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
        instance_id: str,
        trigger_id: str,
        trigger_func: Callable[..., Any],
        trigger_meta: Dict[str, Any],
        config: Any,
        stop_event: threading.Event,
    ) -> None:
        """触发器线程入口，隔离插件异常并上报崩溃"""
        # 触发器代码来自插件，异常由这里捕获并上报
        try:
            def emit_checked(event_name: str, payload: Dict[str, Any]) -> None:
                if not isinstance(payload, dict):
                    raise TypeError("emit_event 的 payload 必须是对象")
                problems = check_payload_contract(
                    trigger_meta.get("outputs"), payload
                )
                if problems:
                    print(
                        f"[Engine] [!!] 触发器 {instance_id} 事件 payload "
                        "违反输出契约，已拦截:",
                        file=sys.stderr,
                    )
                    for problem in problems:
                        print(f"         - {problem}", file=sys.stderr)
                    engine_error(
                        "trigger_payload_invalid",
                        trigger=instance_id,
                        error="; ".join(problems),
                    )
                    self._safe_on_event(
                        "trigger_payload_invalid",
                        {
                            "trigger_id": trigger_id,
                            "instance_id": instance_id,
                            "problems": problems,
                        },
                    )
                    return
                self.emit_event(
                    event_name,
                    payload,
                    instance={"config": config},
                )

            if trigger_meta.get("trigger_api") == "event-v2":

                def emit_event(payload: Dict[str, Any]) -> None:
                    emit_checked(trigger_id, payload)

                trigger_func(trigger_meta, config, emit_event, stop_event)
            else:
                def emit_legacy(event_name: str, payload: Dict[str, Any]) -> None:
                    emit_checked(event_name, payload)

                trigger_func(trigger_meta, config, emit_legacy, stop_event)
        except Exception as error:
            error_type = type(error).__name__
            print(
                f"[Engine] [!!] 触发器线程 {instance_id} 崩溃 "
                f"({error_type})",
                file=sys.stderr,
            )
            engine_error(
                "trigger_crashed",
                trigger=instance_id,
                error_type=error_type,
            )
            self._diag_obj.record_trigger_crash(instance_id, error_type)
            self._trigger_supervisor.mark_crashed(instance_id, error_type)
            self._safe_on_event(
                "trigger_crashed",
                {
                    "trigger_id": trigger_id,
                    "instance_id": instance_id,
                    "error": {
                        "code": "trigger_crashed",
                        "message": "触发器异常退出",
                    },
                },
            )
            self._alert_user(
                f"触发器 {instance_id} 崩溃",
                "触发器异常退出，请查看诊断状态",
                open_dashboard=False,
            )

    def scheduler_stats(self) -> Dict[str, Dict[str, int]]:
        """每个规则当前的运行数和排队数，状态接口展示用"""
        return self._rule_scheduler.stats()

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
            "triggers": {
                "health": self._trigger_supervisor.health(),
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
        """转发到 PluginLoader 并保留旧加载接口的返回值"""
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
        before = len(self._diag_obj.snapshot()["plugin_errors"])
        try:
            if plugin_kind == "trigger":
                entry = self._plugin_registry.resolve_trigger(plugin_id)
            elif plugin_kind == "action":
                entry = self._plugin_registry.resolve_action(plugin_id)
            else:
                raise ValueError(f"未知插件类型: {plugin_kind}")
        except Exception as error:
            self._diag_obj.record_plugin_error(
                plugin_kind,
                plugin_id,
                f"插件导入异常: {error}",
            )
            return False
        errors = self._diag_obj.snapshot()["plugin_errors"][before:]
        return entry is not None and not any(
            len(item) >= 2 and item[1] == plugin_id for item in errors
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

        return self.run_manual_rule_snapshot(rule, rule_index)

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
    ) -> tuple[bool, str, str]:
        """执行调用方传入的规则快照"""
        rule = copy.deepcopy(rule)

        rule_name = rule.get("name", f"规则 #{rule_index + 1}")
        rule_id = rule.get("rule_id", "")
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
        action_ids = [
            action.get("binding_id", "") if isinstance(action, dict) else ""
            for action in actions
        ]
        start_index = action_ids.index(start_step_id) if start_step_id in action_ids else 0
        end_index = (
            action_ids.index(end_step_id)
            if end_step_id in action_ids
            else len(actions) - 1
        )
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
        self._safe_on_event("rule_triggered", {
            "rule_id": rule_id,
            "run_id": run_id,
            "rule_name": rule_name,
            "event_type": "manual",
            "action_count": selected_count,
            "precondition_count": len(rule.get("preconditions", [])),
            "start_step_id": start_step_id,
            "end_step_id": end_step_id,
            "assertion_count": len(test_assertions or []),
            "event_payload": manual_payload,
        })
        decision = self._rule_scheduler.submit(
            rule_id or rule_name,
            rule,
            rule_name,
            context,
        )
        if decision == "queued":
            return True, "已加入队列", run_id
        if decision == "dropped":
            return False, "当前规则不允许新的运行", run_id
        return True, "已开始执行", run_id

    def execute_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        return self._workflow_executor.execute_workflow(
            workflow_key, rule, rule_name, context
        )

    def _check_preconditions(
        self, preconditions: Any, context: Dict[str, Any],
    ) -> Tuple[bool, str, float | None]:
        return self._workflow_executor.check_preconditions(preconditions, context)

    def _defer_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
        delay: float,
    ) -> None:
        self._workflow_executor.defer_workflow(
            workflow_key, rule, rule_name, context, delay
        )

    def _resume_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        self._workflow_executor.resume_workflow(
            workflow_key, rule, rule_name, context
        )

    def _cancel_deferred_workflows(self) -> None:
        self._workflow_executor.cancel_deferred_workflows()

    def cancel_run(self, run_id: str) -> bool:
        return self._workflow_executor.cancel_run(run_id)

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

    def _start_trigger_threads(self, rules: List[Dict[str, Any]] | None = None) -> int:
        if rules is None:
            with self._rules_lock:
                rules = list(self.rules)

        # event-v1 每类触发器共用一条线程，event-v2 每个配置使用独立实例
        aggregated = aggregate_trigger_params(rules)

        # 规则驱动的懒加载：只物化被规则引用的触发器，未引用的保持 pending
        for trigger_id in aggregated:
            self._plugin_registry.resolve_trigger(trigger_id)

        return self._trigger_supervisor.start(
            aggregated=aggregated,
            triggers_funcs=self.triggers_funcs,
            triggers_meta=self.triggers_meta,
            run_trigger_cb=self._run_trigger,
        )

    def _stop_trigger_threads(self, timeout: float = 30.0) -> bool:
        """请求停止所有触发器并返回是否全部退出"""
        return self._trigger_supervisor.stop(timeout)

    def start(
        self, shutdown_event: "threading.Event | None" = None
    ) -> None:
        """运行引擎并在结束时撤销本代授权"""
        if self._privilege_session_closed:
            raise RuntimeError("Engine实例已经关闭，不能再次启动")
        try:
            self._run(shutdown_event=shutdown_event)
        finally:
            self.close()

    def close(self) -> None:
        """撤销本代引擎权限会话并允许重复调用"""
        with self._close_lock:
            if self._privilege_session_closed:
                return
            self._sudo.end_engine_session(self._engine_token)
            self._privilege_session_closed = True

    def _apply_hot_reload_rules(
        self, new_rules: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        # 锁内整体替换规则并重置条件运行时，返回换之前的规则
        with self._rules_lock:
            old_rules = list(self.rules)
            self.rules = new_rules
            self._condition_runtime.reset()
        # 被删掉的规则连排队中的 run 一起丢弃；还在的规则排队条目用入队时的快照
        def rule_key_of(rule: Dict[str, Any]) -> str:
            # 调度 key 与 EventBus 一致，先取 rule_id，没有时取规则名
            return str(rule.get("rule_id", "")) or str(rule.get("name", ""))

        new_keys = {
            rule_key_of(rule)
            for rule in new_rules
            if isinstance(rule, dict)
        }
        for rule in old_rules:
            if not isinstance(rule, dict):
                continue
            key = rule_key_of(rule)
            if key not in new_keys:
                self._rule_scheduler.drop_rule(key)
        return old_rules

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
                        f"核心文件可能被篡改（{detail}）请重新运行 python build.py 生成完整性清单",
                        open_dashboard=True,
                    )
                    raise RuntimeError("核心文件完整性校验失败")

            self._validate_all_rules()

            from notmyfault.platform.platform_support import show_notification

            if os.name == "nt":
                from Win_toaster.AUMID_Register import register_toaster
                register_toaster()

            show_notification("NotmyFault 已加载", "")

            thread_count = self._start_trigger_threads()

            if thread_count == 0:
                print("[Engine] 没有找到可用触发器，程序将退出")
                hint = "没有可用的触发器，请检查规则配置"
                if self._security_mode == SecurityMode.STRICT:
                    hint += (
                        "Tips: 当前安全模式为 strict 严格模式：若是源码运行，内置插件因缺少签名被拒载，"
                        "请先运行 `python build.py` 生成插件签名与 build.json"
                    )
                self._alert_user(
                    "NotmyFault 启动失败 😥",
                    hint,
                    open_dashboard=True,
                )
                return

            se = self._shutdown_flag
            self._hot_reloader.begin()

            try:
                while not se.is_set():
                    se.wait(1)
                    self._hot_reloader.check_once()
            except KeyboardInterrupt:
                print("[Engine] 主程序收到中断，退出中...")
        finally:
            self._cleanup_runtime()

    def _cleanup_runtime(self) -> None:
        with self._runtime_cleanup_lock:
            if self._runtime_cleaned:
                return
            from notmyfault.security.admin_prompt import cancel_pending_admin_requests

            cancel_pending_admin_requests()
            self._rule_scheduler.shutdown()
            self._trigger_supervisor.request_stop_all()
            self._cancel_deferred_workflows()
            stopped = self._stop_trigger_threads(timeout=30)
            scheduled_stopped, drained = self._wait_runtime_work()
            plugin_worker.shutdown_all()
            self._shutdown_plugins()
            self._shutdown_clean = bool(stopped and scheduled_stopped and drained)
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
        self._plugin_registry.clear()

    def _wait_active_actions(self, timeout: float = 60.0) -> bool:
        return self._workflow_executor.wait_active_actions(timeout=timeout)

    def _wait_runtime_work(self, timeout: float = 60.0) -> tuple[bool, bool]:
        deadline = time.monotonic() + timeout
        scheduled_stopped = self._rule_scheduler.wait_for_idle(timeout=timeout)
        remaining = max(deadline - time.monotonic(), 0.0)
        drained = self._wait_active_actions(timeout=remaining)
        return scheduled_stopped, drained

    def shutdown(self) -> None:
        # API、信号和 finally 可能同时调用 shutdown()，每个清理步骤都支持重复执行
        if self._shutdown_flag is not None:
            self._shutdown_flag.set()
        self._cleanup_runtime()
        self.close()
