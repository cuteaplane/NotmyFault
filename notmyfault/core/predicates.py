from __future__ import annotations

from decimal import Decimal
from typing import Any

from notmyfault.core.bindings import BindingResolutionError, references_available, resolve_value


COMPARISON_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "is_true", "is_false"}


def values_equal(left: Any, right: Any) -> bool:
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
    if op not in ("is_true", "is_false") and "right" not in node:
        errors.append(f"{path}.right 不能为空")
    return errors


def evaluate_predicate(node: dict, context: dict, path: str = "condition") -> bool:
    errors = validate_predicate(node, path)
    if errors:
        raise ValueError("；".join(errors))
    op = node["op"]
    if op == "not":
        return not evaluate_predicate(node["children"][0], context, f"{path}.children[0]")
    if op in ("all", "any"):
        values = (
            evaluate_predicate(child, context, f"{path}.children[{index}]")
            for index, child in enumerate(node["children"])
        )
        return all(values) if op == "all" else any(values)
    def operand(key):
        value = node[key]
        if not references_available(value, context):
            raise BindingResolutionError("missing_binding_value", f"{path}.{key}", value, "判断条件引用的数据未参与本次运行")
        return resolve_value(value, context, location=f"{path}.{key}")

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
