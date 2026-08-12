"""执行规则工作流、前置条件和动作"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from notmyfault.core.bindings import (
    BindingResolutionError,
    contains_legacy_template,
    is_reference,
    references_available,
    resolve_value,
)
from notmyfault.core.diagnostics import Diagnostics
from notmyfault.core.logging import engine_error
from notmyfault.core.run_summary import summarize_fields
from notmyfault.core.workflow import (
    ActionCancellation,
    ActionCancelled,
    build_context,
    invoke_action,
)
from notmyfault.security.errors import AdminExecutionBlocked


MappingProvider = Callable[[], Dict[str, Any]]
ActionResolver = Callable[[str], Optional[Callable[..., Any]]]
ShutdownProvider = Callable[[], "threading.Event | None"]
EventSink = Callable[[str, Dict[str, Any]], None]
WorkflowCallback = Callable[..., Any]
SensitiveProvider = Callable[[Dict[str, Any]], set]
_MISSING = object()


def _run_id(context: Dict[str, Any]) -> str:
    return str(context.get("run", {}).get("id", ""))


def _mask_sensitive(value: Any, sensitive_values: set) -> Any:
    """把结构里的敏感字符串换成掩码，只用于事件推送"""
    if isinstance(value, str):
        masked = value
        for secret in sorted(sensitive_values, key=len, reverse=True):
            if secret:
                masked = masked.replace(secret, "***")
        return masked
    if isinstance(value, dict):
        return {
            key: _mask_sensitive(item, sensitive_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_sensitive(item, sensitive_values) for item in value]
    return value


def _mask_declared_outputs(result: Any, outputs: Any) -> Any:
    """把插件声明为 sensitive 的顶层输出换成掩码"""
    if not isinstance(result, dict) or not isinstance(outputs, list):
        return result
    sensitive_names = {
        output.get("name")
        for output in outputs
        if isinstance(output, dict) and output.get("sensitive") is True
    }
    if not sensitive_names:
        return result
    masked = dict(result)
    for name in sensitive_names:
        if name in masked:
            masked[name] = "***"
    return masked


def _string_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, dict):
        result = set()
        for item in value.values():
            result.update(_string_values(item))
        return result
    if isinstance(value, list):
        result = set()
        for item in value:
            result.update(_string_values(item))
        return result
    return set()


def _declared_sensitive_values(value: Any, definitions: Any) -> set[str]:
    if not isinstance(value, dict) or not isinstance(definitions, list):
        return set()
    result = set()
    for spec in definitions:
        if not isinstance(spec, dict) or spec.get("sensitive") is not True:
            continue
        name = spec.get("name")
        if isinstance(name, str) and name in value:
            result.update(_string_values(value[name]))
    return result


def _assertion_value(context: Dict[str, Any], assertion: Dict[str, Any]) -> Any:
    step = context.get("steps", {}).get(assertion.get("step_id"))
    if not isinstance(step, dict) or step.get("status") != "ok":
        return _MISSING
    current = step.get("result", _MISSING)
    for segment in assertion.get("path", []):
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _assertion_matches(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "exists":
        return actual is not _MISSING
    if actual is _MISSING:
        return False
    if operator == "equals":
        return actual == expected
    if operator == "contains":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, list):
            if isinstance(expected, list):
                return all(item in actual for item in expected)
            return expected in actual
        if isinstance(actual, dict):
            if isinstance(expected, dict):
                return all(actual.get(key, _MISSING) == value for key, value in expected.items())
            return isinstance(expected, str) and expected in actual
        return False
    if operator in {"gt", "gte", "lt", "lte"}:
        if isinstance(actual, bool) or isinstance(expected, bool):
            return False
        if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
            return False
        return {
            "gt": actual > expected,
            "gte": actual >= expected,
            "lt": actual < expected,
            "lte": actual <= expected,
        }[operator]
    return False


class WorkflowExecutor:
    """执行工作流并独占延迟任务和活跃动作计数"""

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
        action_resolver: Optional[ActionResolver] = None,
        sensitive_values: Optional[SensitiveProvider] = None,
    ) -> None:
        self._actions_meta = actions_meta
        self._actions_funcs = actions_funcs
        self._action_resolver = action_resolver
        self._plugin_modules = plugin_modules
        self._diagnostics = diagnostics
        self._shutdown_event = shutdown_event
        self._on_event = on_event
        self._defer_workflow = defer_workflow
        self._resume_workflow = resume_workflow
        self._execute_workflow = execute_workflow
        self._execute_actions = execute_actions
        self._run_action = run_action
        self._sensitive_values = sensitive_values

        self._active_actions = 0
        self.action_lock = threading.Lock()
        self.action_done = threading.Condition()
        self.deferred_workflows: Dict[str, threading.Timer] = {}
        self.deferred_workflows_lock = threading.RLock()
        self._run_cancel_events: Dict[str, threading.Event] = {}
        self._deferred_run_keys: Dict[str, str] = {}
        self._deferred_run_contexts: Dict[
            str, Tuple[Dict[str, Any], str]
        ] = {}
        self._run_cancel_lock = threading.RLock()

    def _resolve_action(self, action_type: Any) -> Optional[Callable[..., Any]]:
        """先查已装载缓存，查不到再交给注册表物化懒加载插件"""
        run = self._actions_funcs().get(action_type)
        if run is not None:
            return run
        if self._action_resolver is None:
            return None
        return self._action_resolver(action_type)

    def _ensure_run_cancel_event(
        self, context: Dict[str, Any]
    ) -> threading.Event:
        event = context.get("_run_cancel_event")
        if not isinstance(event, type(threading.Event())):
            event = threading.Event()
            context["_run_cancel_event"] = event
        run_id = _run_id(context)
        if run_id:
            with self._run_cancel_lock:
                self._run_cancel_events[run_id] = event
        return event

    def _finish_run(self, context: Dict[str, Any]) -> bool:
        run_id = _run_id(context)
        with self._run_cancel_lock:
            if context.get("_run_finished") is True:
                return False
            context["_run_finished"] = True
            if run_id:
                self._run_cancel_events.pop(run_id, None)
                self._deferred_run_keys.pop(run_id, None)
                self._deferred_run_contexts.pop(run_id, None)
        return True

    def _complete_cancelled(
        self, context: Dict[str, Any], rule_name: str
    ) -> None:
        if not self._finish_run(context):
            return
        self._on_event(
            "workflow_completed",
            {
                "run_id": _run_id(context),
                "rule_id": context.get("rule", {}).get("id", ""),
                "rule_name": rule_name,
                "status": "cancelled",
                "assertions_passed": 0,
                "assertions_total": 0,
                "failure_kind": "cancelled",
            },
        )

    def cancel_run(self, run_id: str) -> bool:
        with self._run_cancel_lock:
            event = self._run_cancel_events.get(run_id)
            workflow_key = self._deferred_run_keys.pop(run_id, None)
            deferred = self._deferred_run_contexts.pop(run_id, None)
        if event is None:
            return False
        event.set()
        if workflow_key:
            with self.deferred_workflows_lock:
                timer = self.deferred_workflows.pop(workflow_key, None)
            if timer is not None:
                timer.cancel()
            if deferred is not None:
                self._complete_cancelled(deferred[0], deferred[1])
        return True

    @property
    def active_actions(self) -> int:
        with self.action_lock:
            return self._active_actions

    def _masked_event_value(self, value: Any, context: Dict[str, Any]) -> Any:
        if self._sensitive_values is None:
            return value
        hidden = self._sensitive_values(context)
        return _mask_sensitive(value, hidden) if hidden else value

    def execute_workflow(
        self,
        workflow_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> None:
        """执行规则工作流，前置条件未满足时安排延迟重试"""
        if context.get("_run_finished") is True:
            return
        run_cancel_event = self._ensure_run_cancel_event(context)
        if run_cancel_event.is_set():
            self._complete_cancelled(context, rule_name)
            return
        try:
            ready, reason, retry_after = self.check_preconditions(
                rule.get("preconditions", []), context,
            )
        except BindingResolutionError as exc:
            if not self._finish_run(context):
                return
            self._on_event(
                "workflow_failed",
                {
                    "rule_id": context.get("rule", {}).get("id", ""),
                    "run_id": _run_id(context),
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
            if run_cancel_event.is_set():
                self._complete_cancelled(context, rule_name)
                return
            # 插件可能返回非数字的 retry_after_seconds，转不了就用默认 60s
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = 60.0
            delay = min(max(delay, 5), 3600)
            event_reason = self._masked_event_value(str(reason), context)
            self._on_event(
                "workflow_deferred",
                {
                    "rule_id": context.get("rule", {}).get("id", ""),
                    "run_id": _run_id(context),
                    "rule_name": rule_name,
                    "reason": event_reason,
                    "retry_after_seconds": delay,
                },
            )
            print(
                f"[Engine] 工作流 <{rule_name}> 前置条件未满足，{delay}s 后重试: {event_reason}",
                file=sys.stderr,
            )
            self._defer_workflow(workflow_key, rule, rule_name, context, delay)
            return
        actions = rule.get("actions", [])
        if not isinstance(actions, list):
            actions = []
        manual_test = context.get("manual_test")
        if isinstance(manual_test, dict):
            start_index = int(manual_test.get("start_index", 0) or 0)
            end_index = int(manual_test.get("end_index", len(actions) - 1))
            selected_actions = actions[start_index : end_index + 1]
            manual_test["active_offset"] = start_index
        else:
            selected_actions = actions
        succeeded = self._execute_actions(selected_actions, rule_name, context)
        if run_cancel_event.is_set():
            self._complete_cancelled(context, rule_name)
            return
        assertion_results = self.evaluate_test_assertions(context, rule_name)
        if assertion_results is not None:
            succeeded = succeeded and assertion_results["passed"] == assertion_results["total"]
        if run_cancel_event.is_set():
            self._complete_cancelled(context, rule_name)
            return
        if not self._finish_run(context):
            return
        self._on_event(
            "workflow_completed",
            {
                "run_id": _run_id(context),
                "rule_id": context.get("rule", {}).get("id", ""),
                "rule_name": rule_name,
                "status": "succeeded" if succeeded else "failed",
                "assertions_passed": assertion_results["passed"] if assertion_results else 0,
                "assertions_total": assertion_results["total"] if assertion_results else 0,
                "failure_kind": (
                    "assertion" if assertion_results and assertion_results["passed"] < assertion_results["total"]
                    else ""
                ),
            },
        )

    def evaluate_test_assertions(
        self,
        context: Dict[str, Any],
        rule_name: str,
    ) -> Dict[str, Any] | None:
        manual_test = context.get("manual_test")
        assertions = manual_test.get("assertions") if isinstance(manual_test, dict) else None
        if not assertions:
            return None
        results = []
        for assertion in assertions:
            actual = _assertion_value(context, assertion)
            operator = assertion.get("operator", "")
            passed = _assertion_matches(actual, operator, assertion.get("expected"))
            path = assertion.get("path", [])
            results.append({
                "step_id": assertion.get("step_id", ""),
                "path": path,
                "operator": operator,
                "passed": passed,
                "message": "符合预期" if passed else (
                    "动作结果或字段不存在" if actual is _MISSING else "动作结果不符合预期"
                ),
            })
        summary = {
            "run_id": _run_id(context),
            "rule_id": context.get("rule", {}).get("id", ""),
            "rule_name": rule_name,
            "passed": sum(1 for result in results if result["passed"]),
            "total": len(results),
            "results": results,
        }
        self._on_event("test_assertions_completed", summary)
        return summary

    def check_preconditions(
        self,
        preconditions: Any,
        context: Dict[str, Any],
    ) -> Tuple[bool, str, float | None]:
        """调用动作插件的前置条件检查并返回结果"""
        if not preconditions:
            return True, "", None
        if not isinstance(preconditions, list):
            return False, "preconditions 必须是数组", None

        actions_funcs = self._actions_funcs()
        actions_meta = self._actions_meta()
        for index, spec in enumerate(preconditions):
            if not isinstance(spec, dict):
                return False, f"preconditions[{index}] 必须是对象", None
            action_type = spec.get("type")
            action_func = self._resolve_action(action_type)
            plugin_modules = self._plugin_modules()
            module = plugin_modules.get(action_type)
            action_meta = actions_meta.get(action_type, {})
            check = getattr(module, "check_precondition", None)
            if (
                action_func is None
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
        run_cancel_event = self._ensure_run_cancel_event(context)
        if run_cancel_event.is_set():
            self._complete_cancelled(context, rule_name)
            return
        with self.deferred_workflows_lock:
            existing = self.deferred_workflows.get(workflow_key)
            if existing is not None and existing.is_alive():
                self._complete_cancelled(context, rule_name)
                return
            timer = threading.Timer(
                delay,
                self._resume_workflow,
                args=(workflow_key, rule, rule_name, context),
            )
            timer.daemon = True
            self.deferred_workflows[workflow_key] = timer
            run_id = _run_id(context)
            if run_id:
                with self._run_cancel_lock:
                    self._deferred_run_keys[run_id] = workflow_key
                    self._deferred_run_contexts[run_id] = (context, rule_name)
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
        run_id = _run_id(context)
        if run_id:
            with self._run_cancel_lock:
                self._deferred_run_keys.pop(run_id, None)
                self._deferred_run_contexts.pop(run_id, None)
        shutdown_event = self._shutdown_event()
        if shutdown_event and shutdown_event.is_set():
            self._complete_cancelled(context, rule_name)
            return
        self._execute_workflow(workflow_key, rule, rule_name, context)

    def cancel_deferred_workflows(self) -> None:
        with self._run_cancel_lock:
            deferred = list(self._deferred_run_contexts.values())
            for context, _rule_name in deferred:
                event = context.get("_run_cancel_event")
                if isinstance(event, type(threading.Event())):
                    event.set()
        with self.deferred_workflows_lock:
            timers = list(self.deferred_workflows.values())
            self.deferred_workflows.clear()
        for timer in timers:
            timer.cancel()
        for context, rule_name in deferred:
            self._complete_cancelled(context, rule_name)

    def execute_actions(
        self,
        actions: List[Dict[str, Any]],
        rule_name: str,
        context: Dict[str, Any],
    ) -> bool:
        """按顺序执行动作并把每步结果写入 context"""
        manual_test = context.get("manual_test")
        offset = (
            int(manual_test.get("active_offset", 0) or 0)
            if isinstance(manual_test, dict)
            else 0
        )
        return self._execute_action_sequence(
            actions,
            rule_name,
            context,
            legacy_offset=offset,
            run_failure_actions=True,
        )

    def _execute_action_sequence(
        self,
        actions: List[Dict[str, Any]],
        rule_name: str,
        context: Dict[str, Any],
        *,
        legacy_offset: int = 0,
        legacy_prefix: str = "",
        run_failure_actions: bool,
    ) -> bool:
        all_ok = True
        run_cancel_event = self._ensure_run_cancel_event(context)
        for index, action in enumerate(actions):
            if run_cancel_event.is_set():
                all_ok = False
                break
            legacy_step_id = (
                f"{legacy_prefix}{action.get('type', 'action')}_"
                f"{legacy_offset + index + 1}"
            )
            step_id = action.get("binding_id") or legacy_step_id
            if not references_available(action.get("params", {}), context):
                result = None
                record = {
                    "type": action.get("type", "action"),
                    "status": "skipped",
                    "result": None,
                    "error": None,
                }
                self._on_event(
                    "action_skipped",
                    {
                        "action_type": action.get("type"),
                        "rule_id": context.get("rule", {}).get("id", ""),
                        "run_id": _run_id(context),
                        "rule_name": rule_name,
                        "step_id": step_id,
                        "reason": "数据来源未参与本次运行",
                        "duration_ms": 0,
                    },
                )
                ok = True
            else:
                ok, result = self._run_action(action, rule_name, context)
                status = "ok" if ok else (
                    "timed_out"
                    if isinstance(result, ActionCancelled)
                    and result.reason == "timeout"
                    else "cancelled"
                    if isinstance(result, ActionCancelled)
                    else "failed"
                )
                record = {
                    "type": action.get("type", "action"),
                    "status": status,
                    "result": result if ok else None,
                    "error": None if ok else str(result),
                }
                all_ok = all_ok and ok
                if (
                    isinstance(result, ActionCancelled)
                    and result.reason != "timeout"
                ):
                    run_cancel_event.set()
            context["steps"][step_id] = record
            if legacy_step_id != step_id:
                context["steps"][legacy_step_id] = record
            if (
                not ok
                and run_failure_actions
                and not isinstance(result, ActionCancelled)
            ):
                failure_actions = action.get("failure_actions", [])
                if isinstance(failure_actions, list) and failure_actions:
                    self._execute_action_sequence(
                        failure_actions,
                        rule_name,
                        context,
                        legacy_prefix=f"failure_{step_id}_",
                        run_failure_actions=False,
                    )
            if not ok and action.get("on_error", "stop") != "continue":
                print(f"[Engine] 动作流水线在步骤 {step_id} 停止", file=sys.stderr)
                break
        return all_ok

    def execute_action(
        self,
        action: Dict[str, Any],
        rule_name: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """执行一个动作并返回插件结果，供旧调用方使用"""
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
        """执行一个动作并兼容带上下文和旧版两参数插件"""
        started = time.perf_counter()
        shutdown_event = self._shutdown_event()
        if shutdown_event and shutdown_event.is_set():
            print(f"[Engine] 正在关闭，跳过动作: {action.get('type', '?')}")
            return False, "引擎正在关闭"

        action_type = action.get("type")
        dynamic_parameter_error = ""
        dynamic_parameter_name = ""
        if action_type in ("run_powershell", "launch_program"):
            raw_params = action.get("params", {})
            dynamic_parameter_name = (
                "command" if action_type == "run_powershell" else "path"
            )
            raw_value = (
                raw_params.get(dynamic_parameter_name)
                if isinstance(raw_params, dict)
                else None
            )
            if is_reference(raw_value) or contains_legacy_template(raw_value):
                dynamic_parameter_error = (
                    "PowerShell 命令不允许来自运行时数据"
                    if action_type == "run_powershell"
                    else "程序路径不允许来自运行时数据"
                )
        if dynamic_parameter_error:
            self._diagnostics.inc_action_fail()
            engine_error(
                "unsafe_dynamic_parameter",
                action_type=action_type,
                rule_name=rule_name,
                error=dynamic_parameter_error,
            )
            self._on_event(
                "workflow_failed",
                {
                    "action_type": action_type,
                    "rule_id": context.get("rule", {}).get("id", ""),
                    "run_id": _run_id(context),
                    "rule_name": rule_name,
                    "step_id": action.get("binding_id") or action_type,
                    "error": {
                        "code": "unsafe_dynamic_parameter",
                        "location": (
                            f"actions.{action_type}.params."
                            f"{dynamic_parameter_name}"
                        ),
                        "message": dynamic_parameter_error,
                    },
                },
            )
            print(
                f"[Engine] [!!] 拦截 {action_type} 动态参数: {rule_name}",
                file=sys.stderr,
            )
            return False, dynamic_parameter_error
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
                    "rule_id": context.get("rule", {}).get("id", ""),
                    "run_id": _run_id(context),
                    "rule_name": rule_name,
                    "step_id": action.get("binding_id") or action_type,
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

        action_func = self._resolve_action(action_type)
        if action_func is None:
            print(
                f"[Engine] [?] 未知 action 类型或未装载模块: {action_type}",
                file=sys.stderr,
            )
            return False, f"未知 action 类型: {action_type}"

        actions_meta = self._actions_meta()
        action_meta = actions_meta.get(action_type, {})
        timeout_seconds = None
        if action.get("timeout_seconds") is not None:
            if action_meta.get("cancellation_api") != "runtime-v1":
                return False, "这个动作不支持安全取消，不能设置运行超时"
            try:
                timeout_seconds = float(action["timeout_seconds"])
            except (TypeError, ValueError):
                return False, "timeout_seconds 必须是数字"
            if not 1 <= timeout_seconds <= 86400:
                return False, "timeout_seconds 必须在 1 到 86400 秒之间"
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
                validation_error = self._masked_event_value(
                    traceback.format_exc(), context
                )
                print(
                    f"[Engine] action \"{action_type}\" validate_params() 执行异常:",
                    file=sys.stderr,
                )
                print(validation_error, file=sys.stderr)

        with self.action_lock:
            # 在锁内再查一次 shutdown，堵住检查后、计数前被并发关闭的窗口
            shutdown_event = self._shutdown_event()
            if shutdown_event and shutdown_event.is_set():
                return False, "引擎正在关闭"
            self._active_actions += 1

        attempt = 0
        try:
            input_summary = summarize_fields(params, action_meta.get("params"))
            retries = min(max(int(action.get("retry", 0) or 0), 0), 3)
            delay = min(
                max(float(action.get("retry_delay_seconds", 0) or 0), 0),
                3600,
            )
            backoff = action.get("retry_backoff", "fixed")
            for attempt in range(retries + 1):
                try:
                    cancellation = ActionCancellation(
                        self._ensure_run_cancel_event(context),
                        self._shutdown_event(),
                        timeout_seconds,
                    )
                    action_context = {
                        key: value
                        for key, value in context.items()
                        if key != "_run_cancel_event"
                    }
                    runtime = dict(action_context.get("runtime", {}))
                    runtime["cancellation"] = cancellation
                    action_context["runtime"] = runtime
                    result = invoke_action(
                        action_func, module, action_meta, params, action_context
                    )
                    cancellation.raise_if_cancelled()
                    self._diagnostics.inc_action_ok()
                    if self._sensitive_values is not None:
                        hidden = self._sensitive_values(context)
                        hidden.update(
                            _declared_sensitive_values(
                                params, action_meta.get("params")
                            )
                        )
                        event_params = (
                            _mask_sensitive(params, hidden) if hidden else params
                        )
                    else:
                        event_params = params
                    self._on_event(
                        "action_executed",
                        {
                            "action_type": action_type,
                            "params": event_params,
                            "rule_id": context.get("rule", {}).get("id", ""),
                            "run_id": _run_id(context),
                            "rule_name": rule_name,
                            "step_id": action.get("binding_id") or action_type,
                            "status": "ok",
                            "input_summary": input_summary,
                            "output_summary": summarize_fields(
                                result, action_meta.get("outputs")
                            ),
                            "result": _mask_declared_outputs(
                                result, action_meta.get("outputs")
                            ),
                            "attempt": attempt + 1,
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000
                            ),
                        },
                    )
                    return True, result
                except AdminExecutionBlocked:
                    raise
                except ActionCancelled as exc:
                    if exc.reason == "timeout" and attempt < retries:
                        print(
                            f"[Engine] action \"{action_type}\" 运行超时，准备重试",
                            file=sys.stderr,
                        )
                    else:
                        raise
                except Exception:
                    if attempt < retries:
                        print(
                            f"[Engine] action \"{action_type}\" 第 "
                            f"{attempt + 1} 次失败，准备重试",
                            file=sys.stderr,
                        )
                    else:
                        raise
                if attempt < retries:
                    if delay:
                        wait_seconds = (
                            delay * (2 ** attempt)
                            if backoff == "exponential"
                            else delay
                        )
                        retry_wait = ActionCancellation(
                            self._ensure_run_cancel_event(context),
                            self._shutdown_event(),
                        )
                        if retry_wait.wait(min(wait_seconds, 3600)):
                            retry_wait.raise_if_cancelled()
                    continue
                raise
        except ActionCancelled as exc:
            timed_out = exc.reason == "timeout"
            if timed_out:
                self._diagnostics.inc_action_fail()
                engine_error(
                    "action_timed_out",
                    action_type=action_type,
                    rule_name=rule_name,
                    error=str(exc),
                )
            self._on_event(
                "action_timed_out" if timed_out else "action_cancelled",
                {
                    "action_type": action_type,
                    "rule_id": context.get("rule", {}).get("id", ""),
                    "run_id": _run_id(context),
                    "rule_name": rule_name,
                    "step_id": action.get("binding_id") or action_type,
                    "error": str(exc),
                    "input_summary": input_summary,
                    "attempt": attempt + 1,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                },
            )
            return False, exc
        except Exception:
            self._diagnostics.inc_action_fail()
            error_message = traceback.format_exc()
            hidden = (
                self._sensitive_values(context)
                if self._sensitive_values is not None
                else set()
            )
            hidden.update(
                _declared_sensitive_values(params, action_meta.get("params"))
            )
            event_error = _mask_sensitive(error_message, hidden) if hidden else error_message
            print(
                f"[Engine] [ERR] 执行 action \"{action_type}\" 失败:",
                file=sys.stderr,
            )
            print(event_error, file=sys.stderr)
            engine_error(
                "action_failed",
                action_type=action_type,
                rule_name=rule_name,
                error=str(event_error[-500:]),
            )
            self._on_event(
                "error",
                {
                    "action_type": action_type,
                    "rule_id": context.get("rule", {}).get("id", ""),
                    "run_id": _run_id(context),
                    "rule_name": rule_name,
                    "step_id": action.get("binding_id") or action_type,
                    "error": event_error,
                    "input_summary": input_summary,
                    "attempt": attempt + 1,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                },
            )
            return False, event_error[-500:]
        finally:
            with self.action_lock:
                self._active_actions -= 1
            with self.action_done:
                self.action_done.notify_all()

    def wait_active_actions(self, timeout: float = 60.0) -> bool:
        """等待活跃动作排空，返回是否在超时内全部完成"""
        print("[Engine] 正在关闭，等待活跃动作完成...")
        deadline = time.time() + timeout
        while True:
            remaining = self.active_actions
            if remaining == 0:
                print("[Engine] 所有动作已完成，引擎安全关闭")
                return True
            if time.time() >= deadline:
                print(
                    f"[Engine] [!!] shutdown: {remaining} active action(s) still "
                    f"running after {timeout}s, forcing exit",
                    file=sys.stderr,
                )
                return False
            print(f"[Engine] 等待 {remaining} 个活跃动作完成...")
            with self.action_done:
                self.action_done.wait(timeout=3)
