import json
import math


def _reject_constant(value):
    raise ValueError("JSON 不能包含 NaN 或 Infinity")


def _check_json_value(value, ancestors=None):
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON 数字必须是有限值")
        return
    if not isinstance(value, (dict, list)):
        raise ValueError("输入只能包含 JSON 支持的数据类型")
    ancestors = set() if ancestors is None else ancestors
    identity = id(value)
    if identity in ancestors:
        raise ValueError("JSON 数据不能循环引用")
    ancestors.add(identity)
    try:
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("JSON 对象的键必须是字符串")
            items = value.values()
        else:
            items = value
        for item in items:
            _check_json_value(item, ancestors)
    finally:
        ancestors.remove(identity)


def _extract(value, pointer):
    if not isinstance(pointer, str):
        raise ValueError("JSON 路径必须是字符串")
    if not pointer:
        return value
    if not pointer.startswith("/"):
        raise ValueError("JSON 路径须以 / 开头，例如 /items/0/name")
    for index, raw in enumerate(pointer[1:].split("/"), start=1):
        cursor = 0
        while cursor < len(raw):
            if raw[cursor] == "~":
                if cursor + 1 >= len(raw) or raw[cursor + 1] not in "01":
                    raise ValueError("JSON 路径仅支持 ~0 和 ~1 转义")
                cursor += 1
            cursor += 1
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isascii() and key.isdecimal():
            if (len(key) > 1 and key.startswith("0")) or len(key) > 18:
                raise ValueError("JSON 数组下标必须是从 0 开始的整数，不能有前导零")
            position = int(key)
            if position >= len(value):
                raise ValueError(f"JSON 路径第 {index} 段不存在")
            value = value[position]
        else:
            raise ValueError(f"JSON 路径第 {index} 段不存在")
    return value


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    operation = params.get("operation", "parse")
    value = params.get("value", "")
    if operation not in ("parse", "extract", "stringify"):
        raise ValueError("未知的 JSON 操作")
    if operation == "parse" and not isinstance(value, str):
        raise ValueError("解析 JSON 时输入必须是文本")
    if operation == "parse":
        try:
            value = json.loads(value, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 格式错误，第 {exc.lineno} 行第 {exc.colno} 列") from None
    _check_json_value(value)
    if operation == "extract":
        value = _extract(value, params.get("pointer", ""))
    pretty = bool(params.get("pretty", False))
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )
    value_type = (
        "null" if value is None else
        "bool" if isinstance(value, bool) else
        "number" if isinstance(value, (int, float)) else
        "string" if isinstance(value, str) else
        "array" if isinstance(value, list) else "object"
    )
    return {"value": value, "json": text, "type": value_type}
