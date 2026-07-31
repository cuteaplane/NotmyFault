"""规则工作流、前置条件与动作流水线执行。"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from notmyfault.bindings import BindingResolutionError, resolve_value
from notmyfault.diagnostics import Diagnostics
from notmyfault.logging import engine_error
from notmyfault.workflow import build_context, invoke_action


MappingProvider = Callable[[], Dict[str, Any]]
ShutdownProvider = Callable[[], "threading.Event | None"]
EventSink = Callable[[str, Dict[str, Any]], None]
WorkflowCallback = Callable[..., Any]


class WorkflowExecutor:
    """执行工作流并独占延迟任务和活跃动作计数。"""

    def __init__(
        self,
        *,
        actions_meta: MappingProvider,
        actions_funcs: MappingProvider,
        plugin_modules: MappingProvider,
        diagnostics: Diagnostics,
        shutdown_event: ShutdownProvider,
        on_event: EventSink,
        defer_workflow: WorkflowCallback,
        resume_workflow: WorkflowCallback,
        execute_workflow: WorkflowCallback,
        execute_actions: WorkflowCallback,
        run_action: WorkflowCallback,
    ) -> None:
        self._actions_meta = actions_meta
        self._actions_funcs = actions_funcs
        self._plugin_modules = plugin_modules
        self._diagnostics = diagnostics
        self._shutdown_event = shutdown_event
        self._on_event = on_event
        self._defer_workflow = defer_workflow
        self._resume_workflow = resume_workflow
        self._execute_workflow = execute_workflow
        self._execute_actions = execute_actions
        self._run_action = run_action

        self._active_actions = 0
        self.action_lock = threading.Lock()
        self.action_done = threading.Condition()
        self.deferred_workflows: Dict[str, threading.Timer] = {}
        self.deferred_workflows_lock = threading.RLock()

    @property
    def active_actions(self) -> int:
        with self.action_lock:
            return self._active_actions

    def execute_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        """执行规则工作流；前置条件不安全时延后，而不是冒险运行动作。"""
        try:
            ready, reason, retry_after = self.check_preconditions(
                rule.get("preconditions", []), context,
            )
        except BindingResolutionError as exc:
            self._on_event(
                "workflow_failed",
                {
                    "rule_name": rule_name,
                    "error": exc.as_dict(),
                },
            )
            print(
                f"[Engine] 工作流 <{rule_name}> 数据绑定失败: {exc}",
                file=sys.stderr,
            )
            return
        if not ready:
            delay = min(max(retry_after or 60, 5), 3600)
            self._on_event(
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
        self._execute_actions(rule.get("actions", []), rule_name, context)

    def check_preconditions(
        self,
        preconditions: Any,
        context: Dict[str, Any],
    ) -> Tuple[bool, str, float | None]:
        """调用动作插件声明的 check_precondition()；未知/异常一律不放行。"""
        if not preconditions:
            return True, "", None
        if not isinstance(preconditions, list):
            return False, "preconditions 必须是数组", None

        actions_funcs = self._actions_funcs()
        actions_meta = self._actions_meta()
        plugin_modules = self._plugin_modules()
        for index, spec in enumerate(preconditions):
            if not isinstance(spec, dict):
                return False, f"preconditions[{index}] 必须是对象", None
            action_type = spec.get("type")
            module = plugin_modules.get(action_type)
            action_meta = actions_meta.get(action_type, {})
            check = getattr(module, "check_precondition", None)
            if (
                action_type not in actions_funcs
                or not callable(check)
                or action_meta.get("precondition_api") != "context-v1"
            ):
                return (
                    False,
                    f"前置条件插件不可用或未声明 context-v1: {action_type}",
                    None,
                )
            try:
                params = resolve_value(
                    spec.get("params", {}),
                    context,
                    location=f"preconditions[{index}].params",
                )
                if not isinstance(params, dict):
                    return False, f"前置条件 {action_type} 的 params 必须是对象", None
                verdict = check(action_meta, params, context)
            except BindingResolutionError:
                raise
            except Exception:
                return (
                    False,
                    f"前置条件 {action_type} 检查异常: "
                    f"{traceback.format_exc()[-200:]}",
                    None,
                )
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

    def defer_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
        delay: float,
    ) -> None:
        with self.deferred_workflows_lock:
            existing = self.deferred_workflows.get(workflow_key)
            if existing is not None and existing.is_alive():
                return
            timer = threading.Timer(
                delay,
                self._resume_workflow,
                args=(workflow_key, rule, rule_name, context),
            )
            timer.daemon = True
            self.deferred_workflows[workflow_key] = timer
            timer.start()

    def resume_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        with self.deferred_workflows_lock:
            self.deferred_workflows.pop(workflow_key, None)
        shutdown_event = self._shutdown_event()
        if shutdown_event and shutdown_event.is_set():
            return
        self._execute_workflow(workflow_key, rule, rule_name, context)

    def cancel_deferred_workflows(self) -> None:
        with self.deferred_workflows_lock:
            timers = list(self.deferred_workflows.values())
            self.deferred_workflows.clear()
        for timer in timers:
            timer.cancel()

    def execute_actions(
        self,
        actions: List[Dict[str, Any]],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        """顺序执行动作流水线，并把每一步产物写入 context。"""
        for index, action in enumerate(actions):
            legacy_step_id = f"{action.get('type', 'action')}_{index + 1}"
            step_id = action.get("binding_id") or legacy_step_id
            ok, result = self._run_action(action, rule_name, context)
            record = {
                "type": action.get("type", "action"),
                "status": "ok" if ok else "failed",
                "result": result if ok else None,
                "error": None if ok else str(result),
            }
            context["steps"][step_id] = record
            if legacy_step_id != step_id:
                context["steps"][legacy_step_id] = record
            if not ok and action.get("on_error", "stop") != "continue":
                print(f"[Engine] 动作流水线在步骤 {step_id} 停止", file=sys.stderr)
                break

    def execute_action(
        self,
        action: Dict[str, Any],
        rule_name: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """兼容入口：执行一个动作并返回插件的结构化结果。"""
        if context is None:
            context = build_context(rule_name, "", {}, [])
        ok, result = self._run_action(action, rule_name, context)
        return result if ok else None

    def run_action(
        self,
        action: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> Tuple[bool, Any]:
        """执行单一步骤；新插件可拿上下文并返回结果，旧插件保持两参数 API。"""
        shutdown_event = self._shutdown_event()
        if shutdown_event and shutdown_event.is_set():
            print(f"[Engine] 正在关闭，跳过动作: {action.get('type', '?')}")
            return False, "引擎正在关闭"

        action_type = action.get("type")
        try:
            params = resolve_value(
                action.get("params", {}),
                context,
                location=f"actions.{action.get('binding_id') or action_type}.params",
            )
        except BindingResolutionError as exc:
            self._diagnostics.inc_action_fail()
            self._on_event(
                "workflow_failed",
                {
                    "action_type": action_type,
                    "rule_name": rule_name,
                    "error": exc.as_dict(),
                },
            )
            print(
                f"[Engine] action \"{action_type}\" 数据绑定失败: {exc}",
                file=sys.stderr,
            )
            return False, str(exc)
        if not isinstance(params, dict):
            return False, "action.params 必须是对象"

        actions_funcs = self._actions_funcs()
        if action_type not in actions_funcs:
            print(
                f"[Engine] [?] 未知 action 类型或未装载模块: {action_type}",
                file=sys.stderr,
            )
            return False, f"未知 action 类型: {action_type}"

        actions_meta = self._actions_meta()
        module = self._plugin_modules().get(action_type)
        if module is not None and hasattr(module, "validate_params"):
            try:
                validation_errors = module.validate_params(
                    actions_meta.get(action_type, {}), params
                )
                if validation_errors:
                    print(
                        f"[Engine] [!!] action \"{action_type}\" validate_params 警告:",
                        file=sys.stderr,
                    )
                    for validation_error in validation_errors:
                        print(f"         - {validation_error}", file=sys.stderr)
            except Exception:
                print(
                    f"[Engine] action \"{action_type}\" validate_params() 执行异常:",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)

        with self.action_lock:
            self._active_actions += 1

        try:
            action_meta = actions_meta.get(action_type, {})
            action_func = actions_funcs[action_type]
            retries = min(max(int(action.get("retry", 0) or 0), 0), 3)
            delay = max(float(action.get("retry_delay_seconds", 0) or 0), 0)
            for attempt in range(retries + 1):
                try:
                    result = invoke_action(
                        action_func, module, action_meta, params, context
                    )
                    self._diagnostics.inc_action_ok()
                    self._on_event(
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
                            f"[Engine] action \"{action_type}\" 第 "
                            f"{attempt + 1} 次失败，准备重试",
                            file=sys.stderr,
                        )
                        if delay:
                            time.sleep(delay)
                        continue
                    raise
        except Exception:
            self._diagnostics.inc_action_fail()
            error_message = traceback.format_exc()
            print(
                f"[Engine] [ERR] 执行 action \"{action_type}\" 失败:",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            engine_error(
                "action_failed",
                action_type=action_type,
                rule_name=rule_name,
                error=str(error_message[-500:]),
            )
            self._on_event(
                "error",
                {
                    "action_type": action_type,
                    "rule_name": rule_name,
                    "error": error_message,
                },
            )
            return False, error_message[-500:]
        finally:
            with self.action_lock:
                self._active_actions -= 1
            with self.action_done:
                self.action_done.notify_all()

    def wait_active_actions(self, timeout: float = 60.0) -> None:
        print("[Engine] 正在关闭，等待活跃动作完成...")
        deadline = time.time() + timeout
        while True:
            remaining = self.active_actions
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
            with self.action_done:
                self.action_done.wait(timeout=3)
        print("[Engine] 所有动作已完成，引擎安全关闭")
