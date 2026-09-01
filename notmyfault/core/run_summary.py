"""把动作参数和结果缩成可持久化的安全摘要"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


SUMMARY_POLICIES = frozenset({"shape", "value", "hidden"})
_MAX_FIELDS = 8
_MAX_LABEL_LENGTH = 80
_MAX_VALUE_LENGTH = 120


def _text(value: Any, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(value)).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _shape(value: Any, field_type: str) -> str:
    if value is None:
        return "空值"
    if isinstance(value, bool):
        return "布尔值"
    if isinstance(value, (int, float)):
        return "数字"
    if isinstance(value, str):
        names = {
            "path": "路径",
            "textarea": "多行文本",
            "hotkey": "快捷键",
            "time": "时间",
            "select": "选项",
        }
        return f"{names.get(field_type, '文本')} · {len(value)} 字符"
    if isinstance(value, list):
        return f"列表 · {len(value)} 项"
    if isinstance(value, dict):
        return f"对象 · {len(value)} 个字段"
    return type(value).__name__


def _visible_value(value: Any, field_type: str) -> str:
    if value is None:
        return "空值"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return _text(value, _MAX_VALUE_LENGTH)
    if isinstance(value, str):
        return _text(value, _MAX_VALUE_LENGTH) or "空文本"
    return _shape(value, field_type)


def _definitions(raw: Any) -> Iterable[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [
        {"name": item, "label": item, "type": "any"}
        if isinstance(item, str)
        else item
        for item in raw
        if isinstance(item, (str, dict))
    ]


def summarize_fields(value: Any, definitions: Any) -> List[Dict[str, Any]]:
    """只按插件声明摘取字段，未声明的返回内容不进入摘要"""
    specs = list(_definitions(definitions))
    if not specs:
        return []
    if not isinstance(value, dict):
        value = {specs[0].get("name"): value} if len(specs) == 1 else {}

    result = []
    for spec in specs:
        if len(result) >= _MAX_FIELDS:
            break
        name = spec.get("name")
        if not isinstance(name, str) or name not in value:
            continue
        policy = spec.get("summary", "shape")
        if policy == "hidden":
            continue
        sensitive = spec.get("sensitive") is True
        field_type = str(spec.get("value_type") or spec.get("type") or "any")
        display = (
            "敏感值已隐藏"
            if sensitive
            else _visible_value(value[name], field_type)
            if policy == "value"
            else _shape(value[name], field_type)
        )
        result.append({
            "name": name,
            "label": _text(spec.get("label") or name, _MAX_LABEL_LENGTH),
            "type": field_type,
            "display": _text(display, _MAX_VALUE_LENGTH),
            "redacted": sensitive,
        })
    return result
