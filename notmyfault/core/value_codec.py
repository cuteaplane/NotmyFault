"""JSON 边界中的精确数值、二进制和转义对象。"""

from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from uuid import UUID

from notmyfault.core.data_types import DataTypeError, copy_value


WIRE_KEY = "$nmf_value"
MAX_SAFE_INTEGER = 2**53 - 1


def encode_value(value):
    value = copy_value(value)

    def tagged(kind, data):
        return {WIRE_KEY: {"type": kind, "data": data}}

    def encode(item):
        if isinstance(item, bool) or item is None or isinstance(item, (str, float)):
            return item
        if isinstance(item, int):
            return tagged("int", str(item)) if abs(item) > MAX_SAFE_INTEGER else item
        if isinstance(item, Decimal):
            return tagged("decimal", str(item))
        if isinstance(item, bytes):
            return tagged("bytes", base64.b64encode(item).decode("ascii"))
        if isinstance(item, datetime):
            return tagged("datetime", item.isoformat())
        if isinstance(item, date):
            return tagged("date", item.isoformat())
        if isinstance(item, time):
            return tagged("time", item.isoformat())
        if isinstance(item, timedelta):
            return tagged("timedelta", str((item.days * 86400 + item.seconds) * 1000000 + item.microseconds))
        if isinstance(item, PurePath):
            return tagged("windows_path" if isinstance(item, PureWindowsPath) else "posix_path", str(item))
        if isinstance(item, UUID):
            return tagged("uuid", str(item))
        if isinstance(item, list):
            return [encode(child) for child in item]
        if set(item) == {WIRE_KEY}:
            return tagged("object", [[key, encode(child)] for key, child in item.items()])
        return {key: encode(child) for key, child in item.items()}

    return encode(value)


def decode_value(value):
    value = copy_value(value)

    def decode(item, location):
        if isinstance(item, list):
            return [decode(child, f"{location}[{index}]") for index, child in enumerate(item)]
        if not isinstance(item, dict):
            return item
        if set(item) != {WIRE_KEY}:
            return {key: decode(child, f"{location}.{key}") for key, child in item.items()}
        record = item[WIRE_KEY]
        if not isinstance(record, dict) or set(record) != {"type", "data"}:
            raise DataTypeError("类型编码缺少 type 或 data", location=location, code="invalid_encoding")
        kind, data = record["type"], record["data"]
        try:
            if kind == "object":
                if not isinstance(data, list):
                    raise ValueError()
                result = {}
                for pair in data:
                    if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str) or pair[0] in result:
                        raise ValueError()
                    result[pair[0]] = decode(pair[1], f"{location}.{pair[0]}")
                return result
            if not isinstance(data, str):
                raise ValueError()
            if kind in ("int", "timedelta"):
                number = int(data)
                if str(number) != data:
                    raise ValueError()
                return number if kind == "int" else timedelta(microseconds=number)
            if kind == "decimal":
                number = Decimal(data)
                if not number.is_finite():
                    raise ValueError()
                return number
            if kind == "bytes":
                return base64.b64decode(data, validate=True)
            constructors = {
                "date": date.fromisoformat, "time": time.fromisoformat,
                "datetime": datetime.fromisoformat, "uuid": UUID,
                "windows_path": PureWindowsPath, "posix_path": PurePosixPath,
            }
            if kind in constructors:
                return constructors[kind](data)
        except (ValueError, TypeError, OverflowError, InvalidOperation, binascii.Error):
            raise DataTypeError("数据类型编码无效", location=location, code="invalid_encoding") from None
        raise DataTypeError("未知的数据类型编码", location=location, code="invalid_encoding")

    return decode(value, "$")
