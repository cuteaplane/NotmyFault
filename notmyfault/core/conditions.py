from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List

from notmyfault.core.value_codec import encode_value


def get_rule_condition(rule: Dict[str, Any]) -> Dict[str, Any] | None:
    condition = rule.get("condition")
    return condition if isinstance(condition, dict) else None

def _is_event_leaf(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("type"), str) and \
        "children" not in node and "events" not in node

def _condition_children(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    children = node.get("children", [])
    return [child for child in children if isinstance(child, dict)]

def iter_condition_events(node: Dict[str, Any] | None) -> Iterable[Dict[str, Any]]:
    """深度优先枚举条件树中的事件叶子"""
    if not isinstance(node, dict):
        return
    if _is_event_leaf(node):
        yield node
        return
    for child in _condition_children(node):
        yield from iter_condition_events(child)

def get_rule_events(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从规则的任意条件树中提取所有事件条件"""
    return list(iter_condition_events(get_rule_condition(rule)))

def validate_condition_tree(node: Dict[str, Any] | None) -> List[str]:
    """校验可序列化的条件树结构和运算符"""
    errors: List[str] = []

    def visit(current: Any, path: str) -> None:
        if not isinstance(current, dict):
            errors.append(f"{path} 必须是对象")
            return
        if _is_event_leaf(current):
            if not current.get("type"):
                errors.append(f"{path}.type 不能为空")
            if "params" in current and not isinstance(current["params"], dict):
                errors.append(f"{path}.params 必须是对象")
            return
        op = _condition_op(current)
        if op not in ("any", "all", "not"):
            errors.append(f"{path} 的 op 必须为 any、all 或 not")
        children = current.get("children", current.get("events", []))
        if not isinstance(children, list):
            errors.append(f"{path}.children 必须是数组")
            children = []
        if not children:
            errors.append(f"{path} 至少需要一个子条件")
        for index, child in enumerate(children):
            visit(child, f"{path}.children[{index}]")
        if op == "not":
            if len(children) != 1 or not _is_event_leaf(children[0]):
                errors.append(f"{path} 的 NOT 必须包含一个事件条件")
            if "within_seconds" not in current:
                errors.append(f"{path} 的 NOT 必须设置等待时长 within_seconds")
        if "within_seconds" in current:
            try:
                seconds = float(current["within_seconds"])
                if isinstance(current["within_seconds"], bool) or not math.isfinite(seconds) or seconds <= 0:
                    errors.append(f"{path}.within_seconds 必须大于 0")
            except (TypeError, ValueError):
                errors.append(f"{path}.within_seconds 必须是数字")

    visit(node, "condition")
    return errors

def check_event_params(event_def: Dict[str, Any], event_payload: Dict[str, Any]) -> bool:
    """检查事件参数是否匹配并允许 payload 含额外字段"""
    expected_params = event_def.get("params", {})
    for key, expected_val in expected_params.items():
        # 缺少字段时不算命中
        if key not in event_payload or event_payload[key] != expected_val:
            return False
    return True

def _canonical_config_value(value: Any) -> Any:
    """递归规范化配置值，等价写法使用相同指纹"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return int(value) if float(value).is_integer() else value
        except (OverflowError, ValueError):
            return value
    if isinstance(value, dict):
        return {str(key): _canonical_config_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_config_value(item) for item in value]
    return value

def config_fingerprint(params: Any) -> str:
    """生成 event-v2 触发器实例配置的稳定指纹"""
    if not isinstance(params, dict):
        params = {}
    return json.dumps(
        encode_value(_canonical_config_value(params)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

def _condition_op(node: Dict[str, Any]) -> str:
    """读取条件运算符，兼容 ``type: and/or`` 的早期格式"""
    op = node.get("op", node.get("type", "any"))
    return {"or": "any", "and": "all"}.get(str(op).lower(), str(op).lower())

def _event_key(event_def: Dict[str, Any]) -> str:
    """生成事件叶子的稳定键，优先使用 binding_id"""
    binding_id = event_def.get("binding_id")
    if isinstance(binding_id, str) and binding_id:
        return f"id:{binding_id}"
    return json.dumps(
        encode_value({"type": event_def.get("type", ""), "params": event_def.get("params", {})}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

def _absence_nodes(node):
    if not isinstance(node, dict) or _is_event_leaf(node):
        return
    if _condition_op(node) == "not":
        yield node
        return
    for child in _condition_children(node):
        yield from _absence_nodes(child)

def _positive_events(node):
    if not isinstance(node, dict):
        return
    if _is_event_leaf(node):
        yield node
    elif _condition_op(node) != "not":
        for child in _condition_children(node):
            yield from _positive_events(child)
