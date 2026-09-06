"""显式的数据类型转换。"""

from __future__ import annotations

import base64
import binascii
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from notmyfault.core.data_types import (
    TIME_UNITS,
    DataTypeError,
    infer_type,
    is_custom_type,
    normalize_type,
    normalize_value,
)
from notmyfault.core.value_codec import decode_value, encode_value


def convert_value(value, target, registry=None, *, source=None, options=None, location="$"):
    target = normalize_type(target, location=location)
    source = normalize_type(source, location=location) if source is not None else infer_type(value)
    options = options or {}
    if not isinstance(options, dict) or set(options) - {"rounding", "encoding", "timezone", "allow_lossy"}:
        raise DataTypeError("转换选项无效", location=location, code="invalid_conversion")
    kind, source_kind = target["type"], source["type"]

    def invalid(message):
        raise DataTypeError(message, location=location, code="invalid_conversion")

    if source_kind == kind and source == target and not (kind == "datetime" and options.get("timezone") not in (None, "preserve")):
        return normalize_value(value, target, registry, location=location)
    if is_custom_type(source_kind):
        value = normalize_value(value, source, registry, location=location)
        definition = registry.definition(source_kind)
        if definition["binding"] != "shared":
            invalid("私有插件数据不能转换为共享值")
        value = value["data"]
        source = definition["schema"]
        source_kind = source["type"]
    if is_custom_type(kind):
        definition = registry.definition(kind) if registry is not None else None
        if definition is None:
            invalid(f"数据类型未安装: {kind}")
        if definition["binding"] != "shared":
            invalid("私有插件数据只能由所属插件生成")
        return registry.make_value(kind, normalize_value(value, definition["schema"], registry, location=location))
    if kind == "union":
        invalid("转换目标必须是明确类型，不能是联合类型")
    if value is None:
        return normalize_value(value, target, registry, location=location)

    try:
        if kind in ("timestamp", "duration") and source_kind == kind:
            value = normalize_value(value, source, registry, location=location)
            ratio = Decimal(str(TIME_UNITS[source["unit"]])) / Decimal(str(TIME_UNITS[target["unit"]]))
            converted = Decimal(str(value)) * ratio
            value = int(converted) if converted == converted.to_integral_value() else float(converted)
        elif kind == "timestamp":
            if source_kind not in ("text", "datetime"):
                invalid("生成时间戳需要日期时间；数字必须先声明时间戳单位")
            moment = _datetime(value, options, location)
            value = moment.timestamp() / TIME_UNITS[target["unit"]]
        elif kind == "duration":
            if isinstance(value, timedelta):
                value = value.total_seconds() / TIME_UNITS[target["unit"]]
            elif source_kind not in ("int", "float", "number", "text"):
                invalid("时长需要有限数字或 timedelta")
            else:
                value = float(value)
        elif kind in ("date", "time", "datetime"):
            if source_kind == "timestamp":
                stamp = normalize_value(value, source, registry, location=location)
                moment = datetime.fromtimestamp(stamp * TIME_UNITS[source["unit"]], timezone.utc)
                moment = _with_zone(moment, options.get("timezone", "utc"), location)
                value = moment.date() if kind == "date" else moment.timetz() if kind == "time" else moment
            elif source_kind == "datetime" or isinstance(value, datetime):
                moment = _datetime(value, options, location, require_zone=False)
                value = moment.date() if kind == "date" else moment.timetz() if kind == "time" else moment
            elif kind == "datetime" and source_kind == "date":
                day = date.fromisoformat(value) if isinstance(value, str) else value
                value = _with_zone(datetime.combine(day, time()), options.get("timezone"), location)
            elif isinstance(value, str):
                value = normalize_value(value, target, registry, location=location)
                if kind == "datetime" and "timezone" in options:
                    value = _datetime(value, options, location, require_zone=False)
            else:
                invalid("日期转换需要 ISO 文本或明确声明的日期时间类型")
        elif kind in ("int", "float", "number", "decimal"):
            if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
                invalid("数值转换需要文本或数字")
            number = Decimal(value.strip()) if isinstance(value, str) else Decimal(str(value))
            if not number.is_finite():
                invalid("数字必须是有限值")
            if kind == "int":
                rounding = options.get("rounding", "exact")
                if rounding not in ("exact", "truncate", "floor", "ceil", "round"):
                    invalid("取整方式无效")
                if rounding == "exact" and number != number.to_integral_value():
                    invalid("小数转整数须选择取整方式")
                value = {"exact": int, "truncate": int, "floor": math.floor, "ceil": math.ceil, "round": round}[rounding](number)
            elif kind == "decimal":
                value = number
            elif kind == "number" and number == number.to_integral_value():
                value = int(number)
            else:
                value = float(number)
                if isinstance(value, float) and not math.isfinite(value):
                    invalid("数字超出浮点数范围")
                if source_kind == "int" and int(value) != number and options.get("allow_lossy") is not True:
                    invalid("转为浮点数会丢失整数精度")
        elif kind == "bool":
            if isinstance(value, bool):
                pass
            elif isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
                value = value.strip().lower() in ("true", "1")
            elif isinstance(value, (int, float)) and value in (0, 1):
                value = bool(value)
            else:
                invalid("布尔转换只接受 true、false、1 或 0")
        elif kind == "bytes":
            if not isinstance(value, str):
                invalid("二进制转换需要文本输入")
            encoding = options.get("encoding", "utf-8")
            value = base64.b64decode(value, validate=True) if encoding == "base64" else value.encode(encoding)
        elif kind == "text":
            if isinstance(value, bytes):
                encoding = options.get("encoding", "utf-8")
                value = base64.b64encode(value).decode("ascii") if encoding == "base64" else value.decode(encoding)
            elif isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, (dict, list)):
                value = json.dumps(encode_value(value) if options.get("encoding") == "typed-v1" else value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            elif isinstance(value, (datetime, date, time)):
                value = value.isoformat()
            else:
                value = str(value)
        elif kind in ("object", "array") and isinstance(value, str):
            value = json.loads(value, parse_constant=lambda _name: invalid("JSON 数字必须有限"))
            if options.get("encoding") == "typed-v1":
                value = decode_value(value)
    except (ValueError, TypeError, LookupError, OverflowError, OSError, InvalidOperation, binascii.Error) as error:
        if isinstance(error, DataTypeError):
            raise
        invalid(f"不能转换为 {kind}")
    return normalize_value(value, target, registry, location=location)


def _with_zone(moment, zone, location):
    if zone is None or zone == "preserve":
        return moment
    if zone == "utc":
        return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
    if zone == "local":
        return moment.astimezone()
    if isinstance(zone, str):
        try:
            from zoneinfo import ZoneInfo

            target = ZoneInfo(zone)
            return moment.replace(tzinfo=target) if moment.tzinfo is None else moment.astimezone(target)
        except (KeyError, ValueError):
            pass
    raise DataTypeError("时区必须为 utc、local、preserve 或已安装的 IANA 时区名", location=location, code="invalid_conversion")


def _datetime(value, options, location, require_zone=True):
    text = normalize_value(value, "datetime", location=location)
    moment = _with_zone(datetime.fromisoformat(text), options.get("timezone"), location)
    if require_zone and moment.utcoffset() is None:
        raise DataTypeError("无时区时间生成时间戳前必须选择时区", location=location, code="invalid_conversion")
    return moment
