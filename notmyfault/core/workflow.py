"""整理事件、条件组合和步骤结果供插件使用"""
import copy
import threading
import time
from typing import Any, Callable, Dict

from notmyfault.core.data_types import normalize_fields


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
    ) -> None:
        self._run_event = run_event
        self._shutdown_event = shutdown_event
        self._deadline = (
            time.monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        )

    @property
    def reason(self) -> str:
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            return "shutdown"
        if self._run_event is not None and self._run_event.is_set():
            return "cancelled"
        if self._deadline is not None and time.monotonic() >= self._deadline:
            return "timeout"
        return ""

    def is_cancelled(self) -> bool:
        return bool(self.reason)

    def raise_if_cancelled(self) -> None:
        reason = self.reason
        if reason:
            raise ActionCancelled(reason)

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            if self.is_cancelled():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._run_event is not None:
                self._run_event.wait(min(remaining, 0.1))
            elif self._shutdown_event is not None:
                self._shutdown_event.wait(min(remaining, 0.1))
            else:
                time.sleep(min(remaining, 0.1))


def build_context(
    rule_name: str,
    event_type: str,
    event_payload: Dict[str, Any],
    condition_events: list[Dict[str, Any]],
    rule_id: str = "",
    run_id: str = "",
) -> Dict[str, Any]:
    """创建一次规则运行独享的上下文"""
    rule_context = {"name": rule_name}
    if rule_id:
        rule_context["id"] = rule_id
    triggers: Dict[str, Dict[str, Any]] = {}
    for item in condition_events:
        binding_id = item.get("binding_id")
        event = item.get("event", {})
        if not isinstance(binding_id, str) or not binding_id:
            continue
        triggers[binding_id] = {
            "type": event.get("type", ""),
            "payload": copy.deepcopy(item.get("payload", {})),
            # v2 事件叶子的 params 是触发器配置，供 $ref scope=trigger_config 使用
            "config": copy.deepcopy(event.get("params", {})),
        }
    context = {
        "context_version": 2,
        "rule": rule_context,
        "event": {
            "type": event_type,
            "payload": copy.deepcopy(event_payload),
        },
        "triggers": triggers,
        "condition_events": copy.deepcopy(condition_events),
        "steps": {},
    }
    if run_id:
        context["run"] = {"id": run_id}
    return context


def invoke_action(
    action_func: Callable[..., Any],
    module: Any,
    action_meta: Dict[str, Any],
    params: Dict[str, Any],
    context: Dict[str, Any],
) -> Any:
    """按插件元数据调用新旧动作 API，兼容 run 和 run_with_context"""
    registry = context.get("_type_registry")
    params = normalize_fields(params, action_meta.get("params"), registry, parameters=True, location="params")
    if action_meta.get("execution_api") == "context-v1":
        context_runner = getattr(module, "run_with_context", None)
        if not callable(context_runner):
            context_runner = getattr(action_func, "run_with_context", None)
        if not callable(context_runner):
            raise TypeError("execution_api=context-v1 的插件必须定义 run_with_context()")
        result = context_runner(action_meta, params, context)
    else:
        result = action_func(action_meta, params)
    return normalize_fields(result, action_meta.get("outputs"), registry, location="outputs")
