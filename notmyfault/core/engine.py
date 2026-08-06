"""NotmyFault 规则引擎负责加载插件、匹配规则和执行动作"""
import copy
import os
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from notmyfault.config import CONFIG_FILE, ConfigValidationError, load_verified_config
from notmyfault.core.diagnostics import Diagnostics
from notmyfault.core.logging import engine_error, engine_info, engine_warn
from notmyfault.security.plugin_loader import (
    PluginKind,
    PluginLoader,
    PluginRegistry,
    _check_sudo_import,
    _validate_plugin_meta,
    check_permissions_conform,
    check_sudo_import,
    is_known_permission,
    scan_plugin_capabilities,
    validate_plugin_meta,
    verify_plugin_integrity,
    verify_plugin_sig,
)
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


class AutomationEngine:
    """加载插件、匹配规则并执行动作"""

    def __init__(
        self,
        config: Dict[str, Any],
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        # 热重载只替换 rules，构造函数传入的 config 保持原对象
        self.config = config
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
        # 关闭时若还有线程没退干净，跳过撤销权限，免得截断仍在跑的动作
        self._shutdown_clean = True
        self._manual_threads: List[threading.Thread] = []
        self._manual_threads_lock = threading.Lock()
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

        # TriggerSupervisor 管理线程、停止事件和锁，engine 属性通过 property 转发
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
            sensitive_values=self._collect_sensitive_values,
        )
        # 旧扩展和测试仍直接读取这些同步对象
        self._action_lock = self._workflow_executor.action_lock
        self._action_done = self._workflow_executor.action_done
        self._deferred_workflows = self._workflow_executor.deferred_workflows
        self._deferred_workflows_lock = (
            self._workflow_executor.deferred_workflows_lock
        )

    @property
    def _active_actions(self) -> int:
        return self._workflow_executor.active_actions

    # 这些属性转发到 TriggerSupervisor，旧调用方仍可直接访问
    @property
    def _trigger_threads(self) -> Dict[str, threading.Thread]:
        return self._trigger_supervisor.threads

    @_trigger_threads.setter
    def _trigger_threads(self, value: Dict[str, threading.Thread]) -> None:
        self._trigger_supervisor.threads = value

    @property
    def _trigger_events(self) -> Dict[str, threading.Event]:
        return self._trigger_supervisor.events

    @_trigger_events.setter
    def _trigger_events(self, value: Dict[str, threading.Event]) -> None:
        self._trigger_supervisor.events = value

    @property
    def _trigger_lock(self) -> threading.RLock:
        return self._trigger_supervisor.lock

    @_trigger_lock.setter
    def _trigger_lock(self, value: threading.RLock) -> None:
        self._trigger_supervisor.lock = value

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
            if trigger_meta.get("trigger_api") == "event-v2":

                def emit_event(payload: Dict[str, Any]) -> None:
                    if not isinstance(payload, dict):
                        raise TypeError("event-v2 emit_event(payload) 的 payload 必须是对象")
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
                        trigger_id,
                        payload,
                        instance={"config": config},
                    )

                trigger_func(trigger_meta, config, emit_event, stop_event)
            else:
                trigger_func(trigger_meta, config, self.emit_event, stop_event)
        except Exception:
            err = traceback.format_exc()
            print(f"[Engine] [!!] 触发器线程 {instance_id} 崩溃:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            engine_error("trigger_crashed", trigger=instance_id, error=err[-500:])
            self._diag_obj.record_trigger_crash(instance_id, err[-300:])
            self._trigger_supervisor.mark_crashed(instance_id, err[-300:])
            self._safe_on_event(
                "trigger_crashed",
                {"trigger_id": trigger_id, "instance_id": instance_id, "error": err[-500:]},
            )
            # _alert_user 自己会记录告警失败，不再静默吞掉
            self._alert_user(
                f"触发器 {instance_id} 崩溃",
                err[-200:],
                open_dashboard=False,
            )

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

    @staticmethod
    def _get_rule_events(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从规则中提取所有事件条件"""
        return get_rule_events(rule)

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

    def emit_event(
        self,
        event_type: str,
        event_payload: Dict[str, Any],
        instance: Optional[Dict[str, Any]] = None,
    ) -> None:
        """按事件版本匹配规则并分发事件"""
        if self._shutdown_flag and self._shutdown_flag.is_set():
            print(f"[EventBus] Engine正在关闭，忽略事件: [{event_type}]")
            return

        semantic = self.triggers_meta.get(event_type, {}).get("semantic", "oneshot")
        # 声明 sensitive 的输出字段不写明文，日志和事件推送都用掉过掩码的副本
        masked_payload = self._mask_event_payload(event_type, event_payload)
        print(
            f"[EventBus] 收到广播事件: [{event_type}] ({semantic}) -> {masked_payload}"
        )

        with self._rules_lock:
            # 规则快照在锁内复制，动作执行在锁外进行
            rules_snapshot = list(self.rules)

        for rule_index, rule in enumerate(rules_snapshot):
            rule_key = f"{rule_index}:{rule.get('name', '')}"
            if not self._condition_runtime.match(
                rule_key,
                rule,
                event_type,
                event_payload,
                instance=instance,
            ):
                continue

            rule_name = rule.get("name", "未命名规则")
            print(f"[EventBus] [OK] 匹配到规则: <{rule_name}>, 准备分发动作！")
            self._safe_on_event(
                "rule_triggered",
                {
                    "rule_name": rule_name,
                    "event_type": event_type,
                    "event_payload": masked_payload,
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
        """接收触发器线程推送的外部事件"""
        event_type = event_data.get("trigger_id")
        event_payload = event_data.get("triggered_params", {})
        if not isinstance(event_type, str) or not isinstance(event_payload, dict):
            engine_warn("忽略格式无效的外部事件")
            return
        self.emit_event(event_type, event_payload)

    def _mask_event_payload(
        self, event_type: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """把触发器 outputs 声明 sensitive 的字段换成掩码再对外输出"""
        outputs = self.triggers_meta.get(event_type, {}).get("outputs") or []
        sensitive_names = {
            output.get("name")
            for output in outputs
            if isinstance(output, dict) and output.get("sensitive") is True
        }
        if not sensitive_names or not isinstance(payload, dict):
            return payload
        masked = dict(payload)
        for name in sensitive_names:
            if name in masked:
                masked[name] = "***"
        return masked

    def _collect_sensitive_values(self, context: Dict[str, Any]) -> set:
        """收集上下文里声明 sensitive 的字段当前值，用于掩码动作参数"""
        values: set = set()

        def collect(meta: Dict[str, Any], payload: Any) -> None:
            if not isinstance(payload, dict):
                return
            for output in meta.get("outputs") or []:
                if not isinstance(output, dict) or output.get("sensitive") is not True:
                    continue
                value = payload.get(output.get("name"))
                if isinstance(value, str) and value:
                    values.add(value)

        event = context.get("event") or {}
        collect(
            self.triggers_meta.get(event.get("type", ""), {}),
            event.get("payload"),
        )
        for trigger in (context.get("triggers") or {}).values():
            if isinstance(trigger, dict):
                collect(
                    self.triggers_meta.get(trigger.get("type", ""), {}),
                    trigger.get("payload"),
                )
        return values

    def run_manual_rule(self, rule_index: int) -> tuple[bool, str]:
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
    ) -> tuple[bool, str]:
        """执行调用方传入的规则快照"""
        rule = copy.deepcopy(rule)

        rule_name = rule.get("name", f"规则 #{rule_index + 1}")
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
        )
        if isinstance(trigger_payloads, dict):
            event_types = {
                event.get("binding_id"): event.get("type", "")
                for event in get_rule_events(rule)
            }
            leaf_configs = {
                event.get("binding_id"): event.get("params", {})
                for event in get_rule_events(rule)
            }
            context["triggers"] = {
                binding_id: {
                    "type": event_types.get(binding_id, ""),
                    "payload": copy.deepcopy(payload),
                    "config": copy.deepcopy(leaf_configs.get(binding_id, {})),
                }
                for binding_id, payload in trigger_payloads.items()
                if isinstance(binding_id, str) and isinstance(payload, dict)
            }
        self._safe_on_event("rule_triggered", {
            "rule_name": rule_name,
            "event_type": "manual",
            "event_payload": manual_payload,
        })
        thread = threading.Thread(
            target=self.execute_workflow,
            args=(f"manual:{rule_index}", rule, rule_name, context),
            name=f"ManualRule-{rule_index}",
            daemon=True,
        )
        # 登记手工规则线程，shutdown 时要等它们退出再排空动作
        with self._manual_threads_lock:
            self._manual_threads = [
                item for item in self._manual_threads if item.is_alive()
            ]
            self._manual_threads.append(thread)
        thread.start()
        return True, "已开始执行"

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

    def execute_actions(
        self,
        actions: List[Dict[str, Any]],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        self._workflow_executor.execute_actions(actions, rule_name, context)

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
            self._privilege_session_closed = True
        if not self._shutdown_clean:
            engine_warn("关闭时仍有线程未完全退出，保留本代权限会话不撤销")
            return
        self._sudo.end_engine_session(self._engine_token)

    def _run(
        self, shutdown_event: "threading.Event | None" = None
    ) -> None:
        self._start_time = time.time()
        self._shutdown_flag = shutdown_event or threading.Event()

        # strict 源码运行时校验引擎核心文件完整性
        # build.json 签名用于保护安全模式
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
                return

        # 启动前校验规则并记录问题，坏规则与其他规则分别处理
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
                # strict 源码运行需要插件签名文件
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
        config_mtime = (
            os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0
        )
        self._hot_reload_error_reported = False

        try:
            while not se.is_set():
                se.wait(1)
                new_mtime = config_mtime

                try:
                    new_mtime = (
                        os.path.getmtime(CONFIG_FILE)
                        if os.path.exists(CONFIG_FILE)
                        else 0
                    )
                    if new_mtime > config_mtime:
                        # 文件写入中被读取时可能出现临时 JSON 错误，下一轮继续尝试
                        new_config = load_verified_config()
                        new_rules = new_config["rules"]

                        # 旧触发器仍在运行时先等待退出，再决定是否加载新配置
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
                            print("[Engine] 热加载后无可用触发器，保持Engine运行")
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
                            "config.json 未通过格式、签名或安全校验，热加载失败；请修改后重新保存",
                        )
                    # 校验失败后记录当前修改时间，等文件再次保存再重试
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
        stopped = self._stop_trigger_threads(timeout=30)
        manual_stopped = self._join_manual_threads()
        self._shutdown_plugins()
        drained = self._wait_active_actions()
        self._shutdown_clean = bool(stopped and manual_stopped and drained)

    def _join_manual_threads(self, timeout: float = 10.0) -> bool:
        """等待手工规则线程退出，返回是否全部退出"""
        with self._manual_threads_lock:
            threads = list(self._manual_threads)
            self._manual_threads.clear()
        deadline = time.time() + timeout
        for thread in threads:
            remaining = deadline - time.time()
            if remaining > 0:
                thread.join(timeout=remaining)
        return all(not thread.is_alive() for thread in threads)

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

    def _wait_active_actions(self, timeout: float = 60.0) -> bool:
        return self._workflow_executor.wait_active_actions(timeout=timeout)

    def shutdown(self) -> None:
        # API、信号和 finally 可能同时调用 shutdown()，每个清理步骤都支持重复执行
        if self._shutdown_flag:
            self._shutdown_flag.set()
        self._trigger_supervisor.request_stop_all()
        self._cancel_deferred_workflows()
        stopped = self._stop_trigger_threads(timeout=30)
        manual_stopped = self._join_manual_threads()
        self._shutdown_plugins()
        drained = self._wait_active_actions()
        self._shutdown_clean = bool(stopped and manual_stopped and drained)
