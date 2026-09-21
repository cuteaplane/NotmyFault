import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import PurePosixPath, PureWindowsPath
from uuid import UUID

import pytest

from notmyfault.plugin_api import data_types_api
from notmyfault.core.workflow import invoke_action


types = data_types_api()


def test_legacy_field_declarations_and_numeric_contracts():
    assert types.field_type({"type": "textarea"}, parameter=True) == {"type": "text"}
    assert types.field_type({"type": "path"}, parameter=True) == {"type": "text"}
    assert types.field_type({"type": "number"}, parameter=True) == {"type": "number"}
    assert types.field_type({"type": "textarea", "value_type": {"type": "array", "items": "integer"}}, parameter=True) == {"type": "array", "items": {"type": "int"}}
    for value in (1, 1.5):
        actual = types.normalize_value(value, "number")
        assert actual == value and type(actual) is type(value)
    for declaration in ("int", "float", "number", "decimal"):
        with pytest.raises(types.DataTypeError):
            types.normalize_value(True, declaration)
    with pytest.raises(types.DataTypeError):
        types.normalize_value(3.5, "int")
    with pytest.raises(types.DataTypeError):
        types.normalize_value(2**53 + 1, "float")
    assert types.types_compatible("int", "float")
    assert not types.types_compatible("float", "int")
    assert not types.types_compatible("bool", "number")
    assert types.types_compatible("path", "text")
    assert types.types_compatible("text", "path")
    for kind, valid, invalid in (
        ("bool", False, "false"), ("number", 2, "2"), ("string", "text", 2),
        ("array", [], "[]"), ("object", {}, "{}"), ("any", None, {1, 2}),
    ):
        field = {"name": "value", "type": "textarea", "value_type": kind}
        meta = {"params": [field], "outputs": [field]}
        assert invoke_action(lambda info, params: params, None, meta, {"value": valid}, {}) == {"value": valid}
        with pytest.raises(types.DataTypeError):
            invoke_action(lambda info, params: params, None, meta, {"value": invalid}, {})
        with pytest.raises(types.DataTypeError):
            invoke_action(lambda info, params: {"value": invalid}, None, meta, {"value": valid}, {})


def test_structured_values_keep_field_types_optionality_and_independent_copies():
    schema = {
        "type": "object",
        "properties": {
            "记录": {"type": "array", "items": {
                "type": "object", "properties": {"文件.名": "path", "数量": "int"},
                "required": ["文件.名"], "additional_properties": False,
            }},
            "标签": {"type": "object", "additional_properties": {"type": "array", "items": "text"}},
        },
        "required": ["记录"],
        "additional_properties": False,
    }
    original = {"记录": [{"文件.名": "目录/文件.txt", "数量": 2}], "标签": {"工作 项目": ["甲", "乙"]}}
    result = types.normalize_value(original, schema)
    result["记录"][0]["数量"] = 10
    assert original["记录"][0]["数量"] == 2
    field, optional = types.type_at_path(schema, ["记录", 0, "文件.名"])
    assert field == types.normalize_type("path") and optional
    assert types.type_at_path(schema, ["记录"])[1] is False
    with pytest.raises(types.DataTypeError) as caught:
        types.normalize_value({"记录": [{"文件.名": "x", "数量": "2"}]}, schema)
    assert caught.value.location == "$.记录[0].数量"
    with pytest.raises(types.DataTypeError):
        types.type_at_path(schema, ["记录", "0"])
    with pytest.raises(types.DataTypeError):
        types.type_at_path(schema, ["记录", 0, "不存在"])


def test_nullable_enum_and_union_are_distinct_from_missing_or_truthiness():
    schema = {"type": "object", "properties": {
        "value": {"type": "int", "nullable": True},
        "state": {"type": "union", "variants": ["bool", {"type": "text", "enum": ["pending"]}]},
    }, "required": ["value", "state"]}
    for value in (None, 0, 2):
        assert types.normalize_value({"value": value, "state": False}, schema)["value"] == value
    assert types.normalize_value({"value": 2, "state": "pending"}, schema)["state"] == "pending"
    with pytest.raises(types.DataTypeError) as caught:
        types.normalize_value({"state": False}, schema)
    assert caught.value.code == "missing_value"
    with pytest.raises(types.DataTypeError):
        types.normalize_value({"value": 2, "state": 0}, schema)
    assert not types.types_compatible({"type": "int", "nullable": True}, "int")
    assert types.types_compatible({"type": "int", "nullable": True}, {"type": "union", "variants": ["int", "null"]})
    assert types.types_compatible("null", {"type": "int", "nullable": True})


def test_explicit_conversion_never_uses_python_truthiness_or_silent_truncation():
    assert types.convert_value("false", "bool") is False
    assert types.convert_value("0", "bool") is False
    assert types.convert_value("true", "bool") is True
    with pytest.raises(types.DataTypeError):
        types.convert_value("yes", "bool")
    with pytest.raises(types.DataTypeError):
        types.convert_value(3.75, "int")
    assert types.convert_value(3.75, "int", options={"rounding": "floor"}) == 3
    assert types.convert_value("3.75", "int", options={"rounding": "ceil"}) == 4
    assert types.convert_value("9007199254740993", "int") == 9007199254740993
    assert types.convert_value(False, "bool", source="bool", options={"rounding": "exact", "timezone": "preserve"}) is False
    assert types.convert_value("0.100000000000000001", "decimal") == Decimal("0.100000000000000001")
    with pytest.raises(types.DataTypeError):
        types.convert_value(9007199254740993, "float")
    for value in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(types.DataTypeError):
            types.convert_value(value, "decimal")
    text = types.convert_value({"active": False, "values": [1, None]}, "text")
    assert types.convert_value(text, "object") == {"active": False, "values": [1, None]}
    structured = {"amount": Decimal("0.100000000000000001"), "raw": {"$nmf_value": {"type": "int", "data": "99"}}}
    typed_text = types.convert_value(structured, "text", options={"encoding": "typed-v1"})
    assert types.convert_value(typed_text, "object", options={"encoding": "typed-v1"}) == structured
    encoded = types.convert_value(b"\x00\xff", "text", options={"encoding": "base64"})
    assert types.convert_value(encoded, "bytes", options={"encoding": "base64"}) == b"\x00\xff"


def test_time_conversions_require_units_and_timezone_and_preserve_instants():
    seconds = {"type": "timestamp", "unit": "seconds"}
    milliseconds = {"type": "timestamp", "unit": "milliseconds"}
    iso = "2024-02-29T23:30:00+08:00"
    stamp = types.convert_value(iso, seconds)
    assert stamp == 1709220600
    assert types.convert_value(stamp, milliseconds, source=seconds) == 1709220600000
    assert types.convert_value(stamp, "datetime", source=seconds) == "2024-02-29T15:30:00+00:00"
    assert types.convert_value(stamp, "date", source=seconds) == "2024-02-29"
    assert not types.types_compatible(seconds, milliseconds)
    assert types.types_compatible("number", {"type": "duration", "unit": "seconds"})
    assert types.normalize_value(timedelta(seconds=1, milliseconds=250), {"type": "duration", "unit": "milliseconds"}) == 1250
    with pytest.raises(types.DataTypeError):
        types.normalize_type("timestamp")
    with pytest.raises(types.DataTypeError):
        types.convert_value("2024-02-29T15:30:00", seconds)
    assert types.convert_value("2024-02-29T15:30:00", seconds, options={"timezone": "utc"}) == stamp
    with pytest.raises(types.DataTypeError):
        types.normalize_value("2024-02-29", "datetime")
    with pytest.raises(types.DataTypeError):
        types.normalize_value("2023-02-29", "date")
    assert types.normalize_value("15:30:00+08:00", {"type": "time", "timezone": "aware"}) == "15:30:00+08:00"
    assert types.normalize_value("15:30", "time") == "15:30"
    assert types.convert_value(iso, "datetime", source="datetime", options={"timezone": "utc"}) == "2024-02-29T15:30:00+00:00"


def test_paths_urls_and_identifiers_validate_semantics_without_io():
    assert types.normalize_value("C:\\missing\\文件.txt", "path") == "C:\\missing\\文件.txt"
    assert types.normalize_value("../relative/文件.txt", "path") == "../relative/文件.txt"
    assert types.normalize_value("", {"type": "path", "allow_empty": True}) == ""
    with pytest.raises(types.DataTypeError):
        types.normalize_value("", "path")
    assert types.normalize_value("mailto:hello@example.com", "url") == "mailto:hello@example.com"
    assert types.normalize_value("file:///tmp/report.txt", "url") == "file:///tmp/report.txt"
    assert types.normalize_value("HTTPS://example.com/a", {"type": "url", "schemes": ["https"]}) == "HTTPS://example.com/a"
    with pytest.raises(types.DataTypeError):
        types.normalize_value("ftp://example.com/file", {"type": "url", "schemes": ["https"]})
    with pytest.raises(types.DataTypeError):
        types.normalize_value("https://", "url")
    with pytest.raises(types.DataTypeError):
        types.normalize_value("a\x00b", "path")
    assert types.normalize_value("A088C53C-F4D3-473E-A464-F410D111D8DB", "uuid") == "a088c53c-f4d3-473e-a464-f410d111d8db"


def test_wire_round_trip_preserves_precision_native_values_and_reserved_objects():
    original = {
        "small": 3, "large": 2**100 + 1, "negative": -(2**100 + 1),
        "decimal": Decimal("0.123456789012345678900"), "binary": b"\x00\xfe\xff",
        "date": date(2024, 2, 29), "time": time(12, 30, tzinfo=timezone.utc),
        "datetime": datetime(2024, 2, 29, 12, 30, tzinfo=timezone.utc),
        "duration": timedelta(days=-1, microseconds=1),
        "win_path": PureWindowsPath("C:/文件"), "posix_path": PurePosixPath("/tmp/文件"),
        "id": UUID("a088c53c-f4d3-473e-a464-f410d111d8db"),
        "literal": {"$nmf_value": {"type": "int", "data": "9007199254740993"}},
        "values": [None, False, 0, "", 3.25],
    }
    encoded = types.encode_value(original)
    json_value = json.loads(json.dumps(encoded, ensure_ascii=False, allow_nan=False))
    decoded = types.decode_value(json_value)
    assert decoded == original
    assert isinstance(decoded["decimal"], Decimal)
    assert isinstance(decoded["win_path"], PureWindowsPath)
    assert isinstance(decoded["posix_path"], PurePosixPath)
    assert decoded["decimal"].as_tuple() == original["decimal"].as_tuple()
    assert json_value["large"] == {"$nmf_value": {"type": "int", "data": str(2**100 + 1)}}
    with pytest.raises(types.DataTypeError):
        types.decode_value({"$nmf_value": {"type": "bytes", "data": "invalid base64"}})


def shared_meta(package="com.test.records", enabled=True):
    return {"id": package.rsplit(".", 1)[-1], "package_name": package, "enabled": enabled, "contributes": {"data_types": [{
        "id": "record", "version": 1, "binding": "shared", "label": "记录",
        "schema": {"type": "object", "properties": {"name": "text", "amount": "decimal"}, "required": ["name", "amount"]},
    }]}}


def test_custom_type_snapshot_is_namespaced_versioned_and_can_cross_wire():
    identity = "com.test.records/record@1"
    meta = shared_meta()
    registry = types.TypeRegistry.from_plugins([meta, shared_meta("com.other.records")])
    meta["contributes"]["data_types"][0]["schema"] = "text"
    value = registry.make_value(identity, {"name": "甲", "amount": Decimal("1.20")}, "记录")
    restored = types.decode_value(json.loads(json.dumps(types.encode_value(value))))
    assert types.normalize_value(restored, identity, registry) == value
    assert types.type_at_path(identity, ["data", "amount"], registry) == ({"type": "decimal"}, False)
    assert not types.types_compatible(identity, "com.other.records/record@1", registry)
    assert types.convert_value(value, "object", registry, source=identity) == value["data"]
    with pytest.raises(types.DataTypeError):
        types.normalize_value(value, "com.test.records/record@2", registry)
    with pytest.raises(types.DataTypeError):
        types.normalize_value(value, "com.other.records/record@1", registry)
    catalog = registry.catalog()
    catalog["custom"][identity]["schema"] = {"type": "text"}
    assert registry.definition(identity)["schema"]["type"] == "object"


def test_type_dependencies_private_values_and_disabled_plugins_are_explicit():
    private = {"id": "document", "package_name": "com.test.private", "contributes": {"data_types": [{"id": "document", "version": 1}]}}
    registry = types.TypeRegistry.from_plugins([private])
    identity = "com.test.private/document@1"
    value = registry.make_value(identity, {"private": "text"})
    with pytest.raises(types.DataTypeError):
        types.type_at_path(identity, ["data"], registry)
    with pytest.raises(types.DataTypeError):
        types.convert_value(value, "object", registry, source=identity)
    shared = shared_meta()
    shared["contributes"]["data_types"][0]["schema"] = identity
    with pytest.raises(types.DataTypeError):
        types.TypeRegistry.from_plugins([private, shared])
    missing = types.TypeRegistry.from_plugins([shared])
    assert missing.definition("com.test.records/record@1")["available"] is False
    disabled = types.TypeRegistry.from_plugins([shared_meta(enabled=False)], include_disabled=True)
    assert disabled.definition("com.test.records/record@1")["available"] is False
    with pytest.raises(types.DataTypeError):
        disabled.make_value("com.test.records/record@1", {"name": "甲", "amount": 1})
    conflicting = shared_meta()
    conflicting["contributes"]["data_types"][0]["schema"] = "text"
    with pytest.raises(types.DataTypeError):
        types.TypeRegistry.from_plugins([shared_meta(), conflicting])
