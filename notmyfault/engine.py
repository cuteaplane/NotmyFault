"""自动化引擎编排：匹配规则、执行动作并管理运行生命周期。

错误处理原则：
- 不石沉大海：所有异常分支均记录到日志与 Diagnostics（trigger_crashes /
  errors），Dashboard / API 可观测。
- 不说崩就崩：触发器线程与外部回调经 _run_trigger / _safe_on_event 隔离，
  单点异常不击穿主循环、不静默杀死线程。
插件注册与加载流水线已迁移到 ``plugin_loader.py``，本模块保留兼容导出。
"""
import copy
import os
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from notmyfault.config import CONFIG_FILE, ConfigValidationError, load_verified_config
from notmyfault.diagnostics import Diagnostics
from notmyfault.logging import engine_error, engine_info, engine_warn
from notmyfault.plugin_loader import (
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
from notmyfault.rules import (
    ConditionRuntime,
    aggregate_trigger_params,
    check_event_params,
    get_rule_events,
    match_rule,
    validate_rules,
)
from notmyfault.security import (
    SecurityMode,
    detect_security_mode as _detect_security_mode,
    verify_core_integrity,
)
from notmyfault.workflow import build_context, invoke_action, resolve_templates


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
        from notmyfault import sudo as _sudo
        self._sudo = _sudo
        self._engine_token: str = _sudo.begin_engine_session()
        self._privilege_session_closed = False
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
        """从 Dashboard 立即执行一条规则，不依赖其自动触发条件。

        列表上的运行箭头是“立即运行一次”，因此必须常驻；``manual``
        触发器仍可用于只由用户点击触发的规则建模。动作放入独立线程，HTTP
        请求只负责受理，不会被长动作卡住。
        """
        with self._rules_lock:
            if rule_index < 0 or rule_index >= len(self.rules):
                return False, "规则不存在或已被重新加载"
            rule = copy.deepcopy(self.rules[rule_index])

        return self.run_manual_rule_snapshot(rule, rule_index)

    def run_manual_rule_snapshot(
        self,
        rule: Dict[str, Any],
        rule_index: int = -1,
    ) -> tuple[bool, str]:
        """执行调用方已核对过的规则快照，不依赖热重载时序。"""
        rule = copy.deepcopy(rule)

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
        """运行引擎，并保证本代引擎的提权授权最终被撤销。"""
        if self._privilege_session_closed:
            raise RuntimeError("引擎实例已经关闭，不能再次启动")
        try:
            self._run(shutdown_event=shutdown_event)
        finally:
            self.close()

    def close(self) -> None:
        """撤销本代引擎权限会话；可安全重复调用。"""
        if self._privilege_session_closed:
            return
        self._sudo.end_engine_session(self._engine_token)
        self._privilege_session_closed = True

    def _run(
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
