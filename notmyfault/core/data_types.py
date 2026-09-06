"""数据类型声明、值校验和端口类型关系。"""

from __future__ import annotations

import copy
import math
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID


TYPE_ALIASES = {"string": "text", "integer": "int", "boolean": "bool", "list": "array"}
BUILTIN_TYPES = frozenset({
    "any", "null", "text", "int", "float", "number", "decimal", "bool",
    "date", "time", "datetime", "timestamp", "duration", "path", "url", "uuid",
    "bytes", "array", "object", "union",
})
TYPE_LABELS = {
    "any": "任意数据", "null": "空值", "text": "文本", "int": "整数",
    "float": "浮点数", "number": "数字", "decimal": "精确小数", "bool": "布尔值",
    "date": "日期", "time": "时间", "datetime": "日期时间", "timestamp": "时间戳",
    "duration": "时长", "path": "路径", "url": "网址", "uuid": "UUID",
    "bytes": "二进制", "array": "数组", "object": "对象", "union": "多种类型",
}
TIME_UNITS = {"seconds": 1, "milliseconds": 0.001}
_QUALIFIED_TYPE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+/[a-zA-Z][a-zA-Z0-9_-]*@[1-9][0-9]*$")
_COMMON_KEYS = {"type", "nullable", "enum"}
_TYPE_KEYS = {
    "int": {"min", "max"}, "float": {"min", "max"}, "number": {"min", "max"},
    "decimal": {"min", "max"}, "array": {"items"},
    "object": {"properties", "required", "additional_properties"},
    "union": {"variants"}, "timestamp": {"unit"}, "duration": {"unit"},
    "datetime": {"timezone"}, "time": {"timezone"}, "path": {"flavor", "allow_empty"}, "url": {"schemes"},
}


class DataTypeError(ValueError):
    def __init__(self, message: str, *, location: str = "$", code: str = "invalid_value"):
        self.location = location
        self.code = code
        super().__init__(f"{location}: {message}")

    def as_dict(self):
        return {"code": self.code, "location": self.location, "message": str(self)}


def is_custom_type(name: Any) -> bool:
    return isinstance(name, str) and _QUALIFIED_TYPE.fullmatch(name) is not None


def normalize_type(declaration: Any, *, location: str = "$", _depth: int = 0) -> dict:
    if _depth > 64:
        raise DataTypeError("类型嵌套超过 64 层", location=location, code="invalid_type")
    if isinstance(declaration, str):
        declaration = {"type": declaration}
    if not isinstance(declaration, dict):
        raise DataTypeError("类型必须是名称或对象", location=location, code="invalid_type")
    raw = declaration.get("type")
    kind = TYPE_ALIASES.get(raw, raw) if isinstance(raw, str) else None
    if kind not in BUILTIN_TYPES and not is_custom_type(kind):
        raise DataTypeError("未知的数据类型", location=location, code="invalid_type")
    unknown = set(declaration) - _COMMON_KEYS - _TYPE_KEYS.get(kind, set())
    if unknown:
        raise DataTypeError(f"类型包含不适用的字段: {', '.join(sorted(unknown))}", location=location, code="invalid_type")
    result = {"type": kind}

    def invalid(message):
        raise DataTypeError(message, location=location, code="invalid_type")

    def child(value, suffix):
        return normalize_type(value, location=f"{location}.{suffix}", _depth=_depth + 1)

    if "nullable" in declaration:
        if not isinstance(declaration["nullable"], bool):
            invalid("nullable 必须是布尔值")
        if declaration["nullable"]:
            result["nullable"] = True
    if "enum" in declaration:
        values = declaration["enum"]
        if not isinstance(values, list) or not values:
            invalid("enum 必须是非空数组")
        if any(not isinstance(value, (str, int, float, bool, type(None))) for value in values):
            invalid("enum 只能包含文本、数字、布尔值和 null")
        if any(isinstance(value, float) and not math.isfinite(value) for value in values):
            invalid("enum 数字必须有限")
        result["enum"] = copy.deepcopy(values)
    for bound in ("min", "max"):
        if bound in declaration:
            value = declaration[bound]
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                invalid(f"{bound} 必须是有限数字")
            try:
                number = Decimal(str(value))
            except InvalidOperation:
                invalid(f"{bound} 必须是有限数字")
            if not number.is_finite() or (kind == "int" and number != number.to_integral_value()):
                invalid(f"{bound} 与数值类型不匹配")
            if isinstance(value, str) and kind != "decimal":
                invalid(f"{bound} 仅在 decimal 中允许十进制文本")
            result[bound] = value
    if "min" in result and "max" in result and Decimal(str(result["min"])) > Decimal(str(result["max"])):
        invalid("min 不能大于 max")
    if kind == "array":
        result["items"] = child(declaration.get("items", "any"), "items")
    if kind == "object":
        properties = declaration.get("properties", {})
        if not isinstance(properties, dict) or any(not isinstance(key, str) for key in properties):
            invalid("properties 必须是字段名到类型的对象")
        result["properties"] = {key: child(value, f"properties.{key}") for key, value in properties.items()}
        required = declaration.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) or key not in properties for key in required):
            invalid("required 必须引用 properties 中的字段")
        if len(set(required)) != len(required):
            invalid("required 不能重复")
        result["required"] = list(required)
        additional = declaration.get("additional_properties", True)
        result["additional_properties"] = additional if isinstance(additional, bool) else child(additional, "additional_properties")
    if kind == "union":
        variants = declaration.get("variants")
        if not isinstance(variants, list) or not variants:
            invalid("union.variants 必须是非空类型数组")
        result["variants"] = [child(value, f"variants[{index}]") for index, value in enumerate(variants)]
    if kind in ("timestamp", "duration"):
        unit = declaration.get("unit", "seconds" if kind == "duration" else None)
        if unit not in TIME_UNITS:
            invalid("时间单位必须明确为 seconds 或 milliseconds")
        result["unit"] = unit
    if kind in ("datetime", "time"):
        zone = declaration.get("timezone", "any")
        if zone not in ("any", "aware", "naive"):
            invalid("timezone 必须为 any、aware 或 naive")
        result["timezone"] = zone
    if kind == "path":
        flavor = declaration.get("flavor", "any")
        if flavor not in ("any", "windows", "posix"):
            invalid("路径 flavor 必须为 any、windows 或 posix")
        result["flavor"] = flavor
        if "allow_empty" in declaration:
            if not isinstance(declaration["allow_empty"], bool):
                invalid("路径 allow_empty 必须是布尔值")
            result["allow_empty"] = declaration["allow_empty"]
    if kind == "url" and "schemes" in declaration:
        schemes = declaration["schemes"]
        if not isinstance(schemes, list) or not schemes or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9+.-]*", item) for item in schemes):
            invalid("schemes 必须是小写协议名数组")
        result["schemes"] = list(dict.fromkeys(schemes))
    return result


def field_type(field: dict | None, *, parameter: bool = False) -> dict:
    if not isinstance(field, dict):
        return {"type": "any"}
    if "value_type" in field:
        return normalize_type(field["value_type"])
    if field.get("format") in ("path", "url", "date", "time", "datetime", "uuid"):
        return normalize_type(field["format"])
    kind = field.get("type", "string" if parameter else "any")
    if parameter and kind in ("textarea", "select", "hotkey", "path", "time", "macro"):
        kind = "string"
    elif parameter and kind == "plugin_data":
        kind = "object"
    if not parameter and kind == "array" and "item_type" in field:
        return normalize_type({"type": "array", "items": field["item_type"]})
    return normalize_type(kind)


def has_type_contract(field):
    legacy = ("string", "number", "bool", "array", "object", "any")
    if "value_type" in field:
        return True
    return field.get("type", "any") not in legacy


def normalize_fields(values, fields, registry=None, *, parameters=False, location="$"):
    contracted = [field for field in fields or [] if isinstance(field, dict) and has_type_contract(field) and (not parameters or "value_type" in field)]
    if not contracted:
        return copy.deepcopy(values)
    if not isinstance(values, dict):
        raise DataTypeError("输入或输出必须是对象", location=location)
    result = copy.deepcopy(values)
    for field in contracted:
        name = field["name"]
        if name not in result:
            if parameters and "default" in field:
                result[name] = copy.deepcopy(field["default"])
            elif field.get("required", not parameters):
                raise DataTypeError("缺少必填字段", location=f"{location}.{name}", code="missing_value")
            else:
                continue
        result[name] = normalize_value(result[name], field_type(field, parameter=parameters), registry, location=f"{location}.{name}")
    return result


def infer_type(value: Any) -> dict:
    if value is None:
        kind = "null"
    elif isinstance(value, bool):
        kind = "bool"
    elif isinstance(value, int):
        kind = "int"
    elif isinstance(value, float):
        kind = "float"
    elif isinstance(value, Decimal):
        kind = "decimal"
    elif isinstance(value, datetime):
        return normalize_type({"type": "datetime", "timezone": "aware" if value.utcoffset() is not None else "naive"})
    elif isinstance(value, date):
        kind = "date"
    elif isinstance(value, time):
        return normalize_type({"type": "time", "timezone": "aware" if value.utcoffset() is not None else "naive"})
    elif isinstance(value, PurePath):
        kind = "path"
    elif isinstance(value, UUID):
        kind = "uuid"
    elif isinstance(value, bytes):
        kind = "bytes"
    elif isinstance(value, str):
        kind = "text"
    elif isinstance(value, list):
        kind = "array"
    elif isinstance(value, dict):
        kind = value["$type"] if is_custom_type(value.get("$type")) else "object"
    else:
        kind = "any"
    return normalize_type(kind)


def copy_value(value: Any, *, location: str = "$") -> Any:
    ancestors = set()

    def visit(item, path, depth):
        if depth > 64:
            raise DataTypeError("数据嵌套超过 64 层", location=path)
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, (float, Decimal)):
            if not (item.is_finite() if isinstance(item, Decimal) else math.isfinite(item)):
                raise DataTypeError("数字必须是有限值", location=path)
            return item
        if isinstance(item, (bytes, date, time, timedelta, PurePath, UUID)):
            return item
        if not isinstance(item, (dict, list)):
            raise DataTypeError(f"不支持的数据值类型: {type(item).__name__}", location=path)
        identity = id(item)
        if identity in ancestors:
            raise DataTypeError("数据不能循环引用", location=path)
        ancestors.add(identity)
        try:
            if isinstance(item, list):
                return [visit(child, f"{path}[{index}]", depth + 1) for index, child in enumerate(item)]
            if any(not isinstance(key, str) for key in item):
                raise DataTypeError("对象键必须是字符串", location=path)
            return {key: visit(child, f"{path}.{key}", depth + 1) for key, child in item.items()}
        finally:
            ancestors.remove(identity)

    return visit(value, location, 0)


def _enum_contains(values, value):
    return any(type(item) is type(value) and item == value for item in values)


def normalize_value(value: Any, declaration: Any, registry=None, *, location: str = "$") -> Any:
    schema = normalize_type(declaration, location=location)
    value = copy_value(value, location=location)

    def visit(item, spec, path, depth=0):
        kind = spec["type"]

        def invalid(message=None, code="type_mismatch"):
            raise DataTypeError(message or f"数据应为 {TYPE_LABELS.get(kind, kind)}", location=path, code=code)

        if depth > 64:
            invalid("类型展开超过 64 层")
        if "enum" in spec and not _enum_contains(spec["enum"], item):
            invalid("数据不在允许的枚举值中")
        if item is None and spec.get("nullable"):
            return None
        if kind == "union":
            for variant in spec["variants"]:
                try:
                    return visit(item, variant, path, depth + 1)
                except DataTypeError as error:
                    if error.code in ("unknown_type", "unavailable_type"):
                        raise
            invalid("数据不符合任一允许的类型")
        if is_custom_type(kind):
            definition = registry.definition(kind) if registry is not None else None
            if definition is None:
                invalid(f"数据类型未安装或版本不匹配: {kind}", "unknown_type")
            if not definition.get("available", True):
                invalid(f"数据类型不可用: {kind}", "unavailable_type")
            if not isinstance(item, dict) or item.get("$type") != kind or "data" not in item:
                invalid(f"数据必须声明 $type 为 {kind}")
            if "summary" in item and (not isinstance(item["summary"], str) or len(item["summary"]) > 160):
                invalid("数据摘要必须是不超过 160 字符的文本")
            result = dict(item)
            result["data"] = visit(item["data"], definition["schema"], f"{path}.data", depth + 1)
            return result
        if kind == "any":
            return item
        if kind == "null":
            if item is not None:
                invalid()
            return None
        if kind == "bool":
            if not isinstance(item, bool):
                invalid()
            return item
        if kind in ("int", "float", "number", "timestamp", "duration", "decimal"):
            if kind == "duration" and isinstance(item, timedelta):
                item = item.total_seconds() / TIME_UNITS[spec["unit"]]
            if kind == "decimal":
                if not isinstance(item, (int, Decimal)) or isinstance(item, bool):
                    invalid("精确小数必须是 Decimal 或整数，文本和浮点数需要显式转换")
                item = Decimal(item)
            elif isinstance(item, bool) or not isinstance(item, (int, float)):
                invalid()
            if kind == "int" and not isinstance(item, int):
                invalid("整数不能从小数自动截断")
            if kind == "float" and isinstance(item, int):
                try:
                    converted = float(item)
                except OverflowError:
                    invalid("整数超出浮点数范围")
                if not math.isfinite(converted) or int(converted) != item:
                    invalid("转为浮点数会丢失整数精度")
                item = converted
            for bound, comparison in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
                if bound in spec and comparison(Decimal(str(item)), Decimal(str(spec[bound]))):
                    invalid(f"数据超出 {bound} 限制")
            return item
        if kind == "bytes":
            if not isinstance(item, bytes):
                invalid("二进制值需要 bytes，文本需要显式解码")
            return item
        if kind == "array":
            if not isinstance(item, list):
                invalid()
            return [visit(child, spec["items"], f"{path}[{index}]", depth + 1) for index, child in enumerate(item)]
        if kind == "object":
            if not isinstance(item, dict):
                invalid()
            properties = spec["properties"]
            for name in spec["required"]:
                if name not in item:
                    raise DataTypeError("缺少必填字段", location=f"{path}.{name}", code="missing_value")
            result = {}
            for name, child in item.items():
                target = properties.get(name, spec["additional_properties"])
                if target is False:
                    raise DataTypeError("未声明的对象字段", location=f"{path}.{name}")
                result[name] = child if target is True else visit(child, target, f"{path}.{name}", depth + 1)
            return result
        if kind in ("date", "time", "datetime"):
            cls = {"date": date, "time": time, "datetime": datetime}[kind]
            if isinstance(item, cls) and not (kind == "date" and isinstance(item, datetime)):
                moment = item
            elif isinstance(item, str):
                if kind == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item):
                    invalid("日期必须使用 YYYY-MM-DD")
                if kind == "datetime" and (len(item) < 11 or item[10] not in "T "):
                    invalid("日期时间必须同时包含日期和时间")
                try:
                    moment = cls.fromisoformat(item)
                except ValueError:
                    invalid("日期或时间不是有效的 ISO 8601 值")
            else:
                invalid()
            if kind != "date":
                aware = moment.utcoffset() is not None
                if spec["timezone"] == "aware" and not aware:
                    invalid("日期或时间必须包含时区偏移")
                if spec["timezone"] == "naive" and aware:
                    invalid("该类型不接受时区偏移")
            return item if isinstance(item, str) else moment.isoformat()
        if kind == "path" and isinstance(item, PurePath):
            item = str(item)
        if kind == "uuid" and isinstance(item, UUID):
            item = str(item)
        if not isinstance(item, str):
            invalid()
        if kind == "path" and ((not item and not spec.get("allow_empty")) or "\x00" in item):
            invalid("路径不能为空或包含空字符")
        if kind == "url":
            if any(char.isspace() or ord(char) < 32 for char in item):
                invalid("网址不能包含未编码的空白或控制字符")
            try:
                parts = urlsplit(item)
                parts.port
            except ValueError:
                invalid("网址结构或端口无效")
            if not parts.scheme or not (parts.netloc or parts.path):
                invalid("网址必须包含协议和地址")
            if parts.scheme in ("http", "https", "ftp", "ws", "wss") and not parts.hostname:
                invalid("网址缺少主机名")
            if "schemes" in spec and parts.scheme not in spec["schemes"]:
                invalid("网址协议不在允许范围内")
        if kind == "uuid":
            try:
                item = str(UUID(item))
            except ValueError:
                invalid("UUID 格式无效")
        return item

    return visit(value, schema, location)


def types_compatible(source: Any, target: Any, registry=None) -> bool:
    source = normalize_type(source)
    target = normalize_type(target)

    def compatible(left, right):
        a, b = left["type"], right["type"]
        if a == "any" or b == "any":
            return True
        if a == "null":
            return b == "null" or right.get("nullable", False) or b == "union" and any(compatible(left, child) for child in right["variants"])
        if left.get("nullable") and not right.get("nullable"):
            return compatible({"type": "null"}, right) and compatible({key: value for key, value in left.items() if key != "nullable"}, right)
        if a == "union":
            return all(compatible(child, right) for child in left["variants"])
        if b == "union":
            return any(compatible(left, child) for child in right["variants"])
        if "enum" in right and ("enum" not in left or not all(_enum_contains(right["enum"], value) for value in left["enum"])):
            return False
        if a != b:
            if a == "text" and b in ("path", "url", "uuid", "date", "time", "datetime"):
                return True
            if a == "number" and b in ("int", "float", "decimal", "duration"):
                return True
            return (a == "int" and b in ("float", "number", "decimal")) or (a == "float" and b == "number") or (a in ("path", "url", "uuid", "date", "time", "datetime") and b == "text")
        if a in ("timestamp", "duration"):
            return left["unit"] == right["unit"]
        if a in ("time", "datetime"):
            return right["timezone"] == "any" or left["timezone"] == right["timezone"]
        if a == "path":
            return right["flavor"] == "any" or left["flavor"] == right["flavor"]
        if a == "url" and "schemes" in right:
            return "schemes" in left and set(left["schemes"]) <= set(right["schemes"])
        if a == "array":
            return compatible(left["items"], right["items"])
        if a == "object":
            if not set(right["required"]) <= set(left["required"]):
                return False
            for name, field in left["properties"].items():
                other = right["properties"].get(name, right["additional_properties"])
                if other is False or other is not True and not compatible(field, other):
                    return False
            extra = left["additional_properties"]
            if extra is not False:
                for name, other in right["properties"].items():
                    if name not in left["properties"] and not compatible({"type": "any"} if extra is True else extra, other):
                        return False
                target_extra = right["additional_properties"]
                if target_extra is False:
                    return False
                if target_extra is not True and not compatible({"type": "any"} if extra is True else extra, target_extra):
                    return False
        return True

    return bool(compatible(source, target))


def type_at_path(declaration: Any, path: list, registry=None) -> tuple[dict, bool]:
    current = normalize_type(declaration)
    optional = False
    for segment in path:
        if isinstance(segment, bool) or not isinstance(segment, (str, int)) or isinstance(segment, int) and segment < 0:
            raise DataTypeError("路径必须由对象键和非负整数下标组成", code="invalid_reference")
        kind = current["type"]
        optional = optional or current.get("nullable", False)
        if kind == "any":
            current = {"type": "any"}
        elif is_custom_type(kind):
            definition = registry.definition(kind) if registry is not None else None
            if definition is None:
                raise DataTypeError(f"数据类型未安装: {kind}", code="unknown_type")
            if definition["binding"] != "shared":
                raise DataTypeError("私有插件数据不支持字段引用", code="private_plugin_data")
            if segment == "data":
                current = definition["schema"]
            elif segment in ("$type", "summary"):
                current = {"type": "text"}
                optional = optional or segment == "summary"
            else:
                raise DataTypeError("自定义数据字段须从 data 读取", code="unknown_output")
        elif kind == "array" and isinstance(segment, int):
            current = current["items"]
            optional = True
        elif kind == "object" and isinstance(segment, str):
            optional = optional or segment not in current["required"]
            child = current["properties"].get(segment, current["additional_properties"])
            if child is False:
                raise DataTypeError("对象未声明该字段", code="unknown_output")
            current = {"type": "any"} if child is True else child
        elif kind == "union":
            children = []
            for variant in current["variants"]:
                try:
                    child, missing = type_at_path(variant, [segment], registry)
                    optional = optional or missing
                    children.append(child)
                except DataTypeError as error:
                    if error.code != "unknown_output":
                        raise
                    optional = True
            if not children:
                raise DataTypeError("所有候选类型均不提供该字段", code="unknown_output")
            current = children[0] if len(children) == 1 else {"type": "union", "variants": children}
        else:
            raise DataTypeError("类型不支持该字段或下标", code="unknown_output")
    return copy.deepcopy(current), bool(optional)
