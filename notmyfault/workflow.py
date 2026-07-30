"""动作流水线的执行上下文与参数解析。

这里刻意不认识任何云盘、Excel 或具体插件。核心只负责把事件、条件组合和
前序步骤结果交给插件；第三方服务的协议与凭据留在用户插件里。
"""
import re
from typing import Any, Callable, Dict


_TEMPLATE = re.compile(r"{{\s*([a-zA-Z_][\w.]*)\s*}}")
_MISSING = object()


def build_context(
    rule_name: str,
    event_type: str,
    event_payload: Dict[str, Any],
    condition_events: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """创建一次规则运行独享的上下文。"""
    return {
        "rule": {"name": rule_name},
        "event": {"type": event_type, "payload": dict(event_payload)},
        "condition_events": condition_events,
        "steps": {},
    }


def _lookup(context: Dict[str, Any], dotted_path: str) -> Any:
    current: Any = context
    for segment in dotted_path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def resolve_templates(value: Any, context: Dict[str, Any]) -> Any:
    """递归解析 ``{{ event.payload.path }}`` / ``{{ steps.sync.result }}``。

    完整占位符保留原始类型（列表、数字都能直接传给下一步）；嵌在文本中的
    占位符按字符串替换。找不到变量保持原文，避免静默把路径替成空字符串。
    """
    if isinstance(value, dict):
        return {key: resolve_templates(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_templates(item, context) for item in value]
    if not isinstance(value, str):
        return value

    full = _TEMPLATE.fullmatch(value)
    if full:
        resolved = _lookup(context, full.group(1))
        return value if resolved is _MISSING else resolved

    def replace(match: re.Match[str]) -> str:
        resolved = _lookup(context, match.group(1))
        return match.group(0) if resolved is _MISSING else str(resolved)

    return _TEMPLATE.sub(replace, value)


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
