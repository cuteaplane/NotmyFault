"""动作流水线的执行上下文与参数解析。

这里刻意不认识任何云盘、Excel 或具体插件。核心只负责把事件、条件组合和
前序步骤结果交给插件；第三方服务的协议与凭据留在用户插件里。
"""
import copy
from typing import Any, Callable, Dict

from notmyfault.bindings import resolve_value


def build_context(
    rule_name: str,
    event_type: str,
    event_payload: Dict[str, Any],
    condition_events: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """创建一次规则运行独享的上下文。"""
    triggers: Dict[str, Dict[str, Any]] = {}
    for item in condition_events:
        binding_id = item.get("binding_id")
        event = item.get("event", {})
        if not isinstance(binding_id, str) or not binding_id:
            continue
        triggers[binding_id] = {
            "type": event.get("type", ""),
            "payload": copy.deepcopy(item.get("payload", {})),
        }
    return {
        "context_version": 2,
        "rule": {"name": rule_name},
        "event": {
            "type": event_type,
            "payload": copy.deepcopy(event_payload),
        },
        "triggers": triggers,
        "condition_events": copy.deepcopy(condition_events),
        "steps": {},
    }


def resolve_templates(value: Any, context: Dict[str, Any]) -> Any:
    """兼容旧调用点；新规则使用结构化 ``$ref``。"""
    return resolve_value(value, context)


def invoke_action(
    action_func: Callable[..., Any],
    module: Any,
    action_meta: Dict[str, Any],
    params: Dict[str, Any],
    context: Dict[str, Any],
) -> Any:
    """调用新旧两种动作 API。

    旧插件永远保持 ``run(meta, params)``。需要工作流上下文的新插件显式提供
    ``run_with_context(meta, params, context)``，避免靠反射猜参数个数而破坏既有
    插件或 mock。
    """
    # 协议由已校验的插件元数据显式声明，而非靠反射猜参数个数。这样第三方
    # 插件的升级路径明确，旧两参数插件也不会被误调用。
    if action_meta.get("execution_api") == "context-v1":
        context_runner = getattr(module, "run_with_context", None)
        if not callable(context_runner):
            raise TypeError("execution_api=context-v1 的插件必须定义 run_with_context()")
        return context_runner(action_meta, params, context)
    return action_func(action_meta, params)
