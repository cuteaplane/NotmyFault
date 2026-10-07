"""执行规则工作流和动作分支"""

from __future__ import annotations

import sys
import threading
import time
from decimal import Decimal
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from notmyfault.core.bindings import (
    BindingResolutionError,
    contains_dynamic_value,
    references_available,
    resolve_value,
    unavailable_references,
)
from notmyfault.core.binding_schema import prepare_binding_context
from notmyfault.core.data_types import DataTypeError, normalize_fields
from notmyfault.core.variables import assign_variable
from notmyfault.core.diagnostics import Diagnostics
from notmyfault.core.clock import SystemClock
from notmyfault.core.predicates import evaluate_predicate, values_equal
from notmyfault.core.logging import engine_error
from notmyfault.core.run_manager import RunOutcome
from notmyfault.core.run_summary import summarize_fields
from notmyfault.core.workflow import (
    ActionCancellation,
    ActionCancelled,
    build_context,
    invoke_action,
)
from notmyfault.values import allow_secrets, action_values, contains_secret, hold_value, plain_value, secret_value
from notmyfault.core.run_context import RunState, referenced_context
from notmyfault.security.errors import AdminExecutionBlocked
from notmyfault.security.plugin_schema import literal_only_params


MappingProvider = Callable[[], Dict[str, Any]]
ActionResolver = Callable[[str], Optional[Callable[..., Any]]]
ShutdownProvider = Callable[[], "threading.Event | None"]
EventSink = Callable[[str, Dict[str, Any]], None]
_MISSING = object()
def _run_id(context: Dict[str, Any]) -> str:
    return str(context.get("run", {}).get("id", ""))


def _assertion_value(context: Dict[str, Any], assertion: Dict[str, Any]) -> Any:
    step = context.get("steps", {}).get(assertion.get("step_id"))
    if not isinstance(step, dict) or step.get("status") != "ok":
        return _MISSING
    current = step.get("result", _MISSING)
    for segment in assertion.get("path", []):
        current = secret_value(current)
        if isinstance(current, list) and isinstance(segment, int) and not isinstance(segment, bool) and 0 <= segment < len(current):
            current = current[segment]
            continue
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _assertion_matches(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "exists":
        return actual is not _MISSING
    if actual is _MISSING:
        return False
    actual, expected = plain_value(actual), plain_value(expected)
    if operator == "equals":
        return values_equal(actual, expected)
    if operator == "contains":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, list):
            return any(values_equal(item, expected) for item in actual)
        if isinstance(actual, dict):
            if isinstance(expected, dict):
                return all(key in actual and values_equal(actual[key], value) for key, value in expected.items())
            return isinstance(expected, str) and expected in actual
        return False
    if operator in {"gt", "gte", "lt", "lte"}:
        if isinstance(actual, bool) or isinstance(expected, bool):
            return False
        if not isinstance(actual, (int, float, Decimal)) or not isinstance(expected, (int, float, Decimal)):
            return False
        return {
            "gt": actual > expected,
            "gte": actual >= expected,
            "lt": actual < expected,
            "lte": actual <= expected,
        }[operator]
    return False


class ActionPreparationError(ValueError):
    pass


@dataclass
class PreparedAction:
    action_type: str
    action_func: Any
    meta: dict
    raw_params: dict
    params: dict
    module: Any
    timeout_seconds: float | None
    context_sources: tuple | None = None


class WorkflowExecutor:
    """执行工作流并维护取消请求和活跃动作计数"""

    def __init__(
        self,
        *,
        actions_meta: MappingProvider,
        actions_funcs: MappingProvider,
        plugin_modules: MappingProvider,
        diagnostics: Diagnostics,
        shutdown_event: ShutdownProvider,
        on_event: EventSink,
        action_resolver: Optional[ActionResolver] = None,
        triggers_meta: Optional[MappingProvider] = None,
        clock=None,
        status_lookup=None,
    ) -> None:
        self._actions_meta = actions_meta
        self._triggers_meta = triggers_meta or (lambda: {})
        self._actions_funcs = actions_funcs
        self._action_resolver = action_resolver
        self._plugin_modules = plugin_modules
        self._diagnostics = diagnostics
        self._shutdown_event = shutdown_event
        self._on_event = on_event
        self._clock = clock or SystemClock()
        self._status_lookup = status_lookup

        self._active_actions = 0
        self.action_lock = threading.Lock()
        self.action_done = threading.Condition(self.action_lock)

    @staticmethod
    def _plugins_for(context):
        plugins = context.get("_plugins")
        generation = context.get("_generation")
        return plugins if plugins is not None else generation.plugins if generation is not None else None

    def _action_metadata(self, context):
        plugins = self._plugins_for(context)
        return plugins.actions_meta if plugins is not None else self._actions_meta()

    def _trigger_metadata(self, context):
        plugins = self._plugins_for(context)
        return plugins.triggers_meta if plugins is not None else self._triggers_meta()

    def _resolve_action(self, action_type: Any, context) -> Optional[Callable[..., Any]]:
        """先查已装载缓存，查不到再交给注册表物化懒加载插件"""
        plugins = self._plugins_for(context)
        if plugins is not None:
            return plugins.actions_funcs.get(action_type)
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
        if not isinstance(event, threading.Event):
            event = threading.Event()
            context["_run_cancel_event"] = event
        return event

    @property
    def active_actions(self) -> int:
        with self.action_lock:
            return self._active_actions

    def execute_workflow(
        self, workflow_key: str, rule: Dict[str, Any], rule_name: str, context: Dict[str, Any],
    ) -> RunOutcome:
        if isinstance(context, RunState):
            context.prepare()
        if context.get("_preparation_error"):
            return RunOutcome("failed", {"failure_kind": "binding"}, context["_preparation_error"])
        run_cancel_event = self._ensure_run_cancel_event(context)
        if run_cancel_event.is_set():
            return RunOutcome("cancelled")
        if not context.get("_variables_initialized"):
            try:
                prepare_binding_context(rule, context, self._trigger_metadata(context), self._action_metadata(context))
            except (DataTypeError, BindingResolutionError) as error:
                return RunOutcome("failed", {"failure_kind": "binding"}, error.as_dict())
        actions = rule.get("actions", [])
        actions = actions if isinstance(actions, list) else []
        manual_test = context.get("manual_test")
        if isinstance(manual_test, dict):
            start_index = int(manual_test.get("start_index", 0) or 0)
            end_index = int(manual_test.get("end_index", len(actions) - 1))
            selected_actions = actions[start_index:end_index + 1]
            manual_test["active_offset"] = start_index
        else:
            selected_actions = actions
        succeeded = self.execute_actions(selected_actions, rule_name, context)
        if run_cancel_event.is_set():
            return RunOutcome("cancelled", {"failure_kind": "cancelled"})
        assertions = self.evaluate_test_assertions(context, rule_name)
        assertion_failed = assertions is not None and assertions["passed"] < assertions["total"]
        succeeded = succeeded and not assertion_failed
        if run_cancel_event.is_set():
            return RunOutcome("cancelled", {"failure_kind": "cancelled"})
        timed_out = not succeeded and any(step.get("status") == "timed_out" for step in context.get("steps", {}).values())
        status = "succeeded" if succeeded else "timed_out" if timed_out else "failed"
        error = None if succeeded else {"code": "assertion_failed" if assertion_failed else "action_timeout" if timed_out else "action_failed", "message": "检查结果不符合预期" if assertion_failed else "动作执行超时" if timed_out else "动作执行失败"}
        return RunOutcome(status, {
            "assertions_passed": assertions["passed"] if assertions else 0,
            "assertions_total": assertions["total"] if assertions else 0,
            "failure_kind": "assertion" if assertion_failed else "",
        }, error)

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
        self._on_event("run.assertions.completed", summary)
        return summary

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
            if action.get("type") == "set_variable":
                ok = self._execute_assignment(action, step_id, rule_name, context)
                all_ok = all_ok and ok
                if not ok and action.get("on_error", "stop") != "continue":
                    break
                continue
            if action.get("type") == "if":
                ok = self._execute_if(action, step_id, rule_name, context, run_failure_actions)
                all_ok = all_ok and ok
                if not ok and action.get("on_error", "stop") != "continue":
                    break
                continue
            if not references_available(action.get("params", {}), context):
                missing_sources = unavailable_references(
                    action.get("params", {}), context
                )
                source_labels = [
                    f"{usage.location} ← {usage.reference.get('scope')}:{usage.reference.get('node', '')}"
                    for usage in missing_sources
                ]
                skip_reason = "数据来源未参与本次运行"
                if source_labels:
                    skip_reason += "：" + "；".join(source_labels)
                result = None
                record = {
                    "type": action.get("type", "action"),
                    "status": "skipped",
                    "result": None,
                    "error": None,
                }
                self._on_event(
                    "run.step.skipped",
                    {
                        "action_type": action.get("type"),
                        "rule_id": context.get("rule", {}).get("id", ""),
                        "run_id": _run_id(context),
                        "rule_name": rule_name,
                        "step_id": step_id,
                        "reason": skip_reason,
                        "duration_ms": 0,
                    },
                )
                ok = True
            else:
                ok, result = self.run_action(action, rule_name, context)
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
            context["steps"][step_id] = hold_value(record)
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
                print(f"[Engine] 动作在步骤 {step_id} 停止", file=sys.stderr)
                break
        return all_ok

    def _execute_assignment(self, action, step_id, rule_name, context):
        event = {
            "action_type": "set_variable", "step_id": step_id, "rule_name": rule_name,
            "run_id": _run_id(context), "rule_id": context.get("rule", {}).get("id", ""),
            "duration_ms": 0,
        }
        self._on_event("run.step.started", event)
        if not references_available(action.get("value"), context):
            context["steps"][step_id] = {"type": "set_variable", "status": "skipped", "result": None, "error": None}
            self._on_event("run.step.skipped", {**event, "reason": "赋值来源未参与本次运行"})
            return True
        try:
            value = assign_variable(action.get("variable"), action.get("value"), context)
            result = {"value": value}
            context["steps"][step_id] = {"type": "set_variable", "status": "ok", "result": result, "error": None}
            self._on_event("run.step.succeeded", {**event, "status": "ok", "result": result})
            return True
        except (DataTypeError, BindingResolutionError) as error:
            message = str(error)
            context["steps"][step_id] = {"type": "set_variable", "status": "failed", "result": None, "error": message}
            self._on_event("run.step.failed", {**event, "error": {
                "code": error.code, "location": error.location, "message": message,
            }, "status": "failed"})
            return False

    def _execute_if(self, action, step_id, rule_name, context, run_failure_actions):
        started = time.perf_counter()
        event = {
            "action_type": "if", "step_id": step_id, "rule_name": rule_name,
            "rule_id": context.get("rule", {}).get("id", ""), "run_id": _run_id(context),
        }
        self._on_event("run.step.started", event)
        try:
            statuses = {}
            def status_lookup(rule_id):
                if self._status_lookup is None:
                    raise ValueError("当前执行环境不提供自动化运行状态")
                if rule_id not in statuses:
                    statuses[rule_id] = self._status_lookup(rule_id)
                return statuses[rule_id]

            matched = evaluate_predicate(action.get("condition"), context, status_lookup=status_lookup)
            branch = "then" if matched else "else"
            actions = action.get(branch, [])
            if not matched:
                for index, alternative in enumerate(action.get("elif", [])):
                    if evaluate_predicate(alternative.get("condition"), context, f"elif[{index}].condition", status_lookup=status_lookup):
                        matched = True
                        branch = f"elif[{index}]"
                        actions = alternative.get("then", [])
                        break
            succeeded = self._execute_action_sequence(
                actions, rule_name, context,
                legacy_prefix=f"{step_id}_{branch}_", run_failure_actions=run_failure_actions,
            )
            result = {"matched": matched, "branch": branch}
            cancelled = self._ensure_run_cancel_event(context).is_set()
            status = "ok" if succeeded else "cancelled" if cancelled else "failed"
            error = None if succeeded else "运行已取消" if cancelled else "分支动作执行失败"
            context["steps"][step_id] = {
                "type": "if", "status": status,
                "result": result if succeeded else None,
                "error": error,
            }
            self._on_event("run.step.succeeded" if succeeded else "run.step.cancelled" if cancelled else "run.step.failed", {
                **event, "status": status, "result": result, "branch": branch,
                "error": None if succeeded else {"code": "action_cancelled" if cancelled else "action_execution_failed", "message": error},
                "duration_ms": round((time.perf_counter() - started) * 1000),
            })
            return succeeded
        except (BindingResolutionError, ValueError, TypeError) as error:
            message = str(error)
            context["steps"][step_id] = {
                "type": "if", "status": "failed", "result": None, "error": message,
            }
            self._on_event("run.step.failed", {**event, "error": {
                "code": getattr(error, "code", "condition_evaluation_failed"), "message": message,
            }, "status": "failed", "duration_ms": round((time.perf_counter() - started) * 1000)})
            return False

    def execute_action(
        self,
        action: Dict[str, Any],
        rule_name: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """执行一个动作并返回插件结果，供旧调用方使用"""
        if context is None:
            context = build_context(rule_name, "", {}, [])
        ok, result = self.run_action(action, rule_name, context)
        return result if ok else None

    def _authorize_action_parameters(self, action, rule_name, context):
        action_type = action.get("type")
        dynamic_parameter_error = ""
        dynamic_parameter_name = ""
        action_meta = self._action_metadata(context).get(action_type, {})
        raw_params = action.get("params", {})
        if isinstance(raw_params, dict):
            for param_name in sorted(literal_only_params(action_meta)):
                raw_value = raw_params.get(param_name)
                if contains_dynamic_value(raw_value):
                    dynamic_parameter_name = param_name
                    dynamic_parameter_error = (
                        f"参数 {param_name!r} 只允许使用固定值"
                    )
                    break
        if dynamic_parameter_error:
            self._diagnostics.inc_action_fail()
            engine_error(
                "unsafe_dynamic_parameter",
                action_type=action_type,
                rule_name=rule_name,
                error=dynamic_parameter_error,
            )
            self._on_event(
                "run.step.failed",
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
            raise ActionPreparationError(dynamic_parameter_error)

    def _prepare_action(self, action, rule_name, context):
        self._authorize_action_parameters(action, rule_name, context)
        action_type = action.get("type")
        raw_params = action.get("params", {})
        try:
            params = resolve_value(
                action.get("params", {}),
                context,
                location=f"actions.{action.get('binding_id') or action_type}.params",
            )
        except BindingResolutionError as exc:
            self._diagnostics.inc_action_fail()
            self._on_event(
                "run.step.failed",
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
            raise ActionPreparationError(str(exc)) from exc
        if not isinstance(params, dict):
            raise ActionPreparationError("action.params 必须是对象")

        action_func = self._resolve_action(action_type, context)
        if action_func is None:
            print(
                f"[Engine] [?] 未知 action 类型或未装载模块: {action_type}",
                file=sys.stderr,
            )
            raise ActionPreparationError(f"未知 action 类型: {action_type}")

        actions_meta = self._action_metadata(context)
        action_meta = actions_meta.get(action_type, {})
        try:
            params = hold_value(normalize_fields(params, action_meta.get("params"), context.get("_type_registry"), parameters=True, wrap_sensitive=True))
        except DataTypeError as error:
            raise ActionPreparationError(str(error)) from error
        timeout_seconds = None
        if action.get("timeout_seconds") is not None:
            if action_meta.get("cancellation_api") != "runtime-v1":
                raise ActionPreparationError("这个动作不支持安全取消，不能设置运行超时")
            try:
                timeout_seconds = float(action["timeout_seconds"])
            except (TypeError, ValueError):
                raise ActionPreparationError("timeout_seconds 必须是数字")
            if not 1 <= timeout_seconds <= 86400:
                raise ActionPreparationError("timeout_seconds 必须在 1 到 86400 秒之间")
        plugins = self._plugins_for(context)
        module = (plugins.modules if plugins is not None else self._plugin_modules()).get(action_type)
        if module is not None and hasattr(module, "validate_params"):
            try:
                declared = {field.get("name") if isinstance(field, dict) else field for field in action_meta.get("params", [])}
                with allow_secrets({name: value for name, value in params.items() if name in declared}):
                    validation_errors = module.validate_params(
                        actions_meta.get(action_type, {}), action_values(params)
                    )
                if validation_errors:
                    message = "插件参数校验失败" if contains_secret(params) else "; ".join(map(str, validation_errors))
                else:
                    message = ""
            except Exception:
                message = "插件参数校验失败"
            if message:
                self._diagnostics.inc_action_fail()
                self._on_event("run.step.failed", {
                    "action_type": action_type,
                    "rule_id": context.get("rule", {}).get("id", ""),
                    "run_id": _run_id(context),
                    "rule_name": rule_name,
                    "step_id": action.get("binding_id") or action_type,
                    "error": {"code": "invalid_action_params", "message": message},
                })
                raise ActionPreparationError(message)

        compiled = context.get("_compiled_rule")
        index = compiled.step_index.get(action.get("binding_id")) if compiled is not None else None
        sources = compiled.plan[index].context_sources if index is not None else None
        return PreparedAction(
            action_type, action_func, action_meta, raw_params, params, module, timeout_seconds,
            sources,
        )

    def _invoke_action_once(self, prepared, context, cancellation):
        cancellation.raise_if_cancelled()
        context["runtime"] = {"cancellation": cancellation}
        action_context = referenced_context(context, prepared.raw_params, sources=prepared.context_sources) if prepared.meta.get("execution_mode") == "isolated" else context
        result = invoke_action(
            prepared.action_func, prepared.module, prepared.meta, prepared.params, action_context
        )
        cancellation.raise_if_cancelled()
        return result

    def _record_action_success(
        self, prepared, action, rule_name, context, result,
        input_summary, attempt, started,
    ):
        self._diagnostics.inc_action_ok()
        self._on_event("run.step.succeeded", {
            "action_type": prepared.action_type, "params": prepared.params,
            "rule_id": context.get("rule", {}).get("id", ""),
            "run_id": _run_id(context), "rule_name": rule_name,
            "step_id": action.get("binding_id") or prepared.action_type,
            "status": "ok", "input_summary": input_summary,
            "output_summary": summarize_fields(result, prepared.meta.get("outputs")),
            "result": result, "attempt": attempt + 1,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        })

    def _record_action_cancelled(self, action, rule_name, context, exc, input_summary, attempt, started):
        action_type = action.get("type")
        timed_out = exc.reason == "timeout"
        if timed_out:
            self._diagnostics.inc_action_fail()
            engine_error(
                "run.step.timed_out",
                action_type=action_type,
                rule_name=rule_name,
                error=str(exc),
            )
        self._on_event(
            "run.step.timed_out" if timed_out else "run.step.cancelled",
            {
                "action_type": action_type,
                "rule_id": context.get("rule", {}).get("id", ""),
                "run_id": _run_id(context),
                "rule_name": rule_name,
                "step_id": action.get("binding_id") or action_type,
                "error": {"code": "action_timeout" if timed_out else "action_cancelled", "message": str(exc)},
                "input_summary": input_summary,
                "attempt": attempt + 1,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            },
        )
        return False, exc

    def _record_action_failure(self, action, rule_name, context, error, input_summary, attempt, started):
        action_type = action.get("type")
        self._diagnostics.inc_action_fail()
        error_code = "action_execution_failed"
        error_message = f"动作 {action_type} 执行失败"
        print(
            f"[Engine] [ERR] 执行 action \"{action_type}\" 失败 "
            f"({type(error).__name__})",
            file=sys.stderr,
        )
        engine_error(
            "action_failed",
            plugin=action_type,
            action_type=action_type,
            rule_name=rule_name,
            error_type=type(error).__name__,
            reason=type(error).__name__,
        )
        self._on_event(
            "run.step.failed",
            {
                "action_type": action_type,
                "rule_id": context.get("rule", {}).get("id", ""),
                "run_id": _run_id(context),
                "rule_name": rule_name,
                "step_id": action.get("binding_id") or action_type,
                "error": {
                    "code": error_code,
                    "message": error_message,
                },
                "input_summary": input_summary,
                "attempt": attempt + 1,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            },
        )
        return False, error_message

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

        self._on_event("run.step.started", {"run_id": _run_id(context), "rule_id": context.get("rule", {}).get("id", ""), "step_id": action.get("binding_id") or action.get("type"), "action_type": action.get("type"), "rule_name": rule_name})
        try:
            prepared = self._prepare_action(action, rule_name, context)
        except ActionPreparationError as error:
            return False, str(error)
        action_type = prepared.action_type
        action_meta = prepared.meta
        raw_params = prepared.raw_params
        params = prepared.params

        with self.action_lock:
            # 在锁内再查一次 shutdown，两次检查之间引擎可能已经开始关闭
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
            cancellation = ActionCancellation(self._ensure_run_cancel_event(context), self._shutdown_event(), prepared.timeout_seconds, clock=self._clock)
            for attempt in range(retries + 1):
                try:
                    result = self._invoke_action_once(prepared, context, cancellation)
                    self._record_action_success(
                        prepared, action, rule_name, context, result,
                        input_summary, attempt, started,
                    )
                    return True, result
                except AdminExecutionBlocked:
                    raise
                except ActionCancelled:
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
                        retry_wait = cancellation
                        if retry_wait.wait(min(wait_seconds, 3600)):
                            retry_wait.raise_if_cancelled()
                    continue
        except ActionCancelled as error:
            return self._record_action_cancelled(action, rule_name, context, error, input_summary, attempt, started)
        except Exception as error:
            return self._record_action_failure(action, rule_name, context, error, input_summary, attempt, started)
        finally:
            with self.action_done:
                self._active_actions -= 1
                self.action_done.notify_all()

    def wait_active_actions(self, timeout: float = 60.0) -> bool:
        """等待活跃动作排空，返回是否在超时内全部完成"""
        deadline = time.monotonic() + timeout
        with self.action_done:
            while self._active_actions:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.action_done.wait(timeout=remaining)
        return True
