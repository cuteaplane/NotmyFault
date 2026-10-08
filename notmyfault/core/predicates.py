from __future__ import annotations

from decimal import Decimal
from typing import Any

from notmyfault.core.bindings import BindingResolutionError, is_reference, reference_exists, references_available, resolve_value
from notmyfault.core.rule_model import _RULE_ID_RE, _BINDING_ID_RE
from notmyfault.values import plain_value


COMPARISON_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "is_true", "is_false", "exists", "not_exists"}
RUN_STATUSES = {"running", "queued", "succeeded", "failed", "cancelled", "timed_out", "dropped", "never_run"}


def values_equal(left: Any, right: Any) -> bool:
    left, right = plain_value(left), plain_value(right)
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list) and isinstance(right, list)
            and len(left) == len(right)
            and all(values_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict) and isinstance(right, dict)
            and left.keys() == right.keys()
            and all(values_equal(value, right[key]) for key, value in left.items())
        )
    return left == right


def validate_predicate(node: Any, path: str = "condition") -> list[str]:
    if not isinstance(node, dict):
        return [f"{path} 必须是对象"]
    op = node.get("op")
    if op == "event_occurred":
        identity = node.get("trigger_id")
        errors = []
        if not isinstance(identity, str) or not identity.startswith("t_") or not _BINDING_ID_RE.fullmatch(identity):
            errors.append(f"{path}.trigger_id 必须是本流程触发器的节点 ID")
        if not isinstance(node.get("expected", True), bool):
            errors.append(f"{path}.expected 必须是布尔值")
        return errors
    if op == "run_status":
        errors = []
        if not isinstance(node.get("rule_id"), str) or not _RULE_ID_RE.fullmatch(node["rule_id"]):
            errors.append(f"{path}.rule_id 必须是自动化的规则 ID")
        if not isinstance(node.get("status"), str) or node["status"] not in RUN_STATUSES:
            errors.append(f"{path}.status 不是支持的运行状态")
        if node.get("comparison", "eq") not in ("eq", "ne"):
            errors.append(f"{path}.comparison 必须是 eq 或 ne")
        return errors
    if op in ("all", "any", "not"):
        children = node.get("children")
        if not isinstance(children, list) or not children:
            return [f"{path}.children 至少需要一个条件"]
        errors = []
        if op == "not" and len(children) != 1:
            errors.append(f"{path} 的 NOT 必须有且仅有一个子条件")
        for index, child in enumerate(children):
            errors.extend(validate_predicate(child, f"{path}.children[{index}]"))
        return errors
    if not isinstance(op, str) or op not in COMPARISON_OPERATORS:
        return [f"{path}.op 不是支持的比较方式"]
    errors = []
    if "left" not in node:
        errors.append(f"{path}.left 不能为空")
    if op in ("exists", "not_exists") and not is_reference(node.get("left")):
        errors.append(f"{path}.left 的存在判断必须选择数据来源")
    if op not in ("is_true", "is_false", "exists", "not_exists") and "right" not in node:
        errors.append(f"{path}.right 不能为空")
    return errors


def event_occurrences(context):
    observation = context.get("_event_observation")
    manual = context.get("manual_test")
    return observation.snapshot() if observation is not None else frozenset(
        manual.get("provided_trigger_ids", ()) if isinstance(manual, dict) else context.get("_event_occurrences", ())
    )


def evaluate_predicate(node: dict, context: dict, path: str = "condition", *, status_lookup=None, _event_occurrences=None) -> bool:
    errors = validate_predicate(node, path)
    if errors:
        raise ValueError("；".join(errors))
    op = node["op"]
    if _event_occurrences is None:
        _event_occurrences = event_occurrences(context)
    if op == "event_occurred":
        return (node["trigger_id"] in _event_occurrences) == node.get("expected", True)
    if op == "run_status":
        if status_lookup is None:
            raise ValueError("当前执行环境不提供自动化运行状态")
        state = status_lookup(node["rule_id"])
        expected = node["status"]
        if expected in ("running", "queued"):
            matched = state[expected] > 0
        elif expected == "never_run":
            matched = not state["running"] and not state["queued"] and state["last_status"] is None
        else:
            matched = state["last_status"] == expected
        return matched if node.get("comparison", "eq") == "eq" else not matched
    if op == "not":
        return not evaluate_predicate(node["children"][0], context, f"{path}.children[0]", status_lookup=status_lookup, _event_occurrences=_event_occurrences)
    if op in ("all", "any"):
        values = (
            evaluate_predicate(child, context, f"{path}.children[{index}]", status_lookup=status_lookup, _event_occurrences=_event_occurrences)
            for index, child in enumerate(node["children"])
        )
        return all(values) if op == "all" else any(values)
    if op in ("exists", "not_exists"):
        present = reference_exists(node["left"]["$ref"], context, location=f"{path}.left")
        return present if op == "exists" else not present
    def operand(key):
        value = node[key]
        if not references_available(value, context):
            raise BindingResolutionError("missing_binding_value", f"{path}.{key}", value, "判断条件引用的数据未参与本次运行")
        return plain_value(resolve_value(value, context, location=f"{path}.{key}"))

    left = operand("left")
    if op in ("is_true", "is_false") and not isinstance(left, bool):
        raise ValueError(f"{path} 的布尔判断需要布尔值")
    if op == "is_true":
        return left is True
    if op == "is_false":
        return left is False
    right = operand("right")
    if op in ("eq", "ne"):
        equal = values_equal(left, right)
        return equal if op == "eq" else not equal
    if op == "contains":
        if isinstance(left, str) and isinstance(right, str):
            return right in left
        if isinstance(left, list):
            return any(values_equal(item, right) for item in left)
        raise ValueError(f"{path} 的包含比较需要文本或数组")
    if any(isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) for value in (left, right)):
        raise ValueError(f"{path} 的大小比较需要数字")
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]
