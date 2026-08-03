"""整理事件、条件组合和步骤结果供插件使用"""
import copy
from typing import Any, Callable, Dict


def build_context(
    rule_name: str,
    event_type: str,
    event_payload: Dict[str, Any],
    condition_events: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """创建一次规则运行独享的上下文"""
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


def invoke_action(
    action_func: Callable[..., Any],
    module: Any,
    action_meta: Dict[str, Any],
    params: Dict[str, Any],
    context: Dict[str, Any],
) -> Any:
    """按插件元数据调用新旧动作 API，兼容 run 和 run_with_context"""
    # 插件元数据声明执行接口，调用方按声明传入参数
    if action_meta.get("execution_api") == "context-v1":
        context_runner = getattr(module, "run_with_context", None)
        if not callable(context_runner):
            raise TypeError("execution_api=context-v1 的插件必须定义 run_with_context()")
        return context_runner(action_meta, params, context)
    return action_func(action_meta, params)
