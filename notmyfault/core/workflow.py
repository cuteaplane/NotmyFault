"""整理事件、条件组合和步骤结果供插件使用"""
import threading
from typing import Any, Callable, Dict

from notmyfault.core.data_types import DataTypeError, normalize_fields
from notmyfault.core.clock import Clock, SystemClock
from notmyfault.core.run_context import RunState, run_view
from notmyfault.values import allow_secrets, contains_secret, hold_value, protect


class ActionCancelled(RuntimeError):
    """动作在执行前或协作等待期间收到取消请求"""

    def __init__(self, reason: str = "cancelled") -> None:
        self.reason = reason
        message = "动作运行超时" if reason == "timeout" else "动作已取消"
        super().__init__(message)


class ActionCancellation:
    """提供给 context-v1 动作的协作式取消对象"""

    def __init__(
        self,
        run_event: threading.Event | None = None,
        shutdown_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._run_event = run_event
        self._shutdown_event = shutdown_event
        self._deadline = (
            self._clock.monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        )

    @property
    def reason(self) -> str:
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            return "shutdown"
        if self._run_event is not None and self._run_event.is_set():
            return "cancelled"
        if self._deadline is not None and self._clock.monotonic() >= self._deadline:
            return "timeout"
        return ""

    def is_cancelled(self) -> bool:
        return bool(self.reason)

    def raise_if_cancelled(self) -> None:
        reason = self.reason
        if reason:
            raise ActionCancelled(reason)

    def wait(self, timeout: float) -> bool:
        deadline = self._clock.monotonic() + max(float(timeout), 0.0)
        event = self._run_event or self._shutdown_event or threading.Event()
        while True:
            if self.is_cancelled():
                return True
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                return False
            self._clock.wait(event, min(remaining, 0.1))


def build_context(
    rule_name: str,
    event_type: str,
    event_payload: Dict[str, Any],
    condition_events: list[Dict[str, Any]],
    rule_id: str = "",
    run_id: str = "",
) -> RunState:
    """创建一次规则运行独享的上下文"""
    rule_context = {"name": rule_name}
    if rule_id:
        rule_context["id"] = rule_id
    triggers: Dict[str, Dict[str, Any]] = {}
    for item in condition_events:
        if item.get('absence'):
            continue
        binding_id = item.get("binding_id")
        event = item.get("event", {})
        if not isinstance(binding_id, str) or not binding_id:
            continue
        triggers[binding_id] = {
            "type": event.get("type", ""),
            "payload": hold_value(item.get("payload", {})),
            # v2 事件叶子的 params 是触发器配置，供 $ref scope=trigger_config 使用
            "config": hold_value(event.get("params", {})),
        }
    context = {
        "rule": rule_context,
        "event": {
            "type": event_type,
            "payload": hold_value(event_payload),
        },
        "triggers": triggers,
        "condition_events": hold_value(condition_events),
        "_event_occurrences": frozenset(triggers),
        "steps": {},
    }
    if run_id:
        context["run"] = {"id": run_id}
    return RunState(context)


def invoke_action(
    action_func: Callable[..., Any],
    module: Any,
    action_meta: Dict[str, Any],
    params: Dict[str, Any],
    context: Dict[str, Any],
) -> Any:
    """调用动作插件的 run_with_context(action_info, params, context)"""
    registry = context.get("_type_registry")
    params = normalize_fields(params, action_meta.get("params"), registry, parameters=True, location="params", wrap_sensitive=True)
    declared = {field.get("name") if isinstance(field, dict) else field for field in action_meta.get("params", [])}
    with allow_secrets({name: value for name, value in params.items() if name in declared}):
        runner = getattr(module, "run_with_context", None)
        if not callable(runner):
            runner = action_func
        if not callable(runner):
            raise TypeError("动作插件必须定义 run_with_context()")
        view = context if getattr(runner, "_isolated_proxy", False) else run_view(context, context.get("runtime", {}).get("cancellation"))
        result = runner(action_meta, params, view)
    result = normalize_fields(result, action_meta.get("outputs"), registry, location="outputs", wrap_sensitive=True)
    result = protect(result) if contains_secret(params) else hold_value(result)
    if registry is not None:
        try:
            result = registry.binding_value(result)
        except DataTypeError:
            # 动作结果可含插件私有类型，跨步骤引用时再检查共享声明
            pass
    return result
