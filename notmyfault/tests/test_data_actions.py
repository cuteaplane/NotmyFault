import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from notmyfault.core.rules import validate_rule_bindings
from notmyfault.core.workflow import build_context, invoke_action
from notmyfault.tests.api_support import create_test_engine


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "actions"


def load_action(plugin_id):
    directory = PLUGIN_ROOT / plugin_id
    meta = json.loads((directory / "action.json").read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location(f"data_action_{plugin_id}", directory / "action.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return meta, module


def execute(plugin_id, params):
    meta, module = load_action(plugin_id)
    return invoke_action(module.run, module, meta, params, {})


@pytest.mark.parametrize(
    ("operation", "text", "extra", "expected"),
    [
        ("trim", "\t 自动化 \r\n", {}, "自动化"),
        ("upper", "Straße api", {}, "STRASSE API"),
        ("lower", "JSON Api", {}, "json api"),
        ("title", "hello world", {}, "Hello World"),
        ("casefold", "Straße", {}, "strasse"),
        ("replace", "a.b a?b a.b", {"find": "a.b", "replacement": "完成"}, "完成 a?b 完成"),
    ],
)
def test_text_operations_preserve_unicode_and_replace_literal_text(operation, text, extra, expected):
    result = execute("text_transform", {"operation": operation, "text": text, **extra})
    assert result["text"] == expected
    assert result["length"] == len(expected)
    assert result["lines"] == [expected]


def test_line_splitting_keeps_blank_lines_until_requested():
    params = {"operation": "split_lines", "text": " A \r\n\r\n B \n"}
    result = execute("text_transform", params)
    assert result == {"text": " A \n\n B ", "lines": [" A ", "", " B "], "length": 8, "line_count": 3}
    result = execute("text_transform", {**params, "trim_lines": True, "skip_empty_lines": True})
    assert result == {"text": "A\nB", "lines": ["A", "B"], "length": 3, "line_count": 2}
    parts = execute("text_transform", {"operation": "split", "text": "甲||乙|", "delimiter": "|"})
    assert parts["lines"] == ["甲", "", "乙", ""]
    joined = execute("text_transform", {"operation": "join", "parts": parts["lines"], "delimiter": "|"})
    assert joined["text"] == "甲||乙|"
    with pytest.raises(ValueError):
        execute("text_transform", {"operation": "join", "parts": ["甲", 3]})


@pytest.mark.parametrize(
    ("value", "value_type"),
    [(None, "null"), (False, "bool"), (42, "number"), (1.25, "number"), ("中文", "string"), ("42", "string"), ([1, False, None], "array"), ({"rows": [{"name": "甲"}]}, "object")],
)
def test_json_parse_extract_and_stringify_preserve_json_types(value, value_type):
    parsed = execute("json_data", {"value": json.dumps(value)})
    assert parsed["value"] == value
    assert type(parsed["value"]) is type(value)
    assert parsed["type"] == value_type
    extracted = execute("json_data", {"operation": "extract", "value": parsed["value"], "pointer": ""})
    assert extracted["value"] == value
    assert type(extracted["value"]) is type(value)
    assert extracted["type"] == value_type
    serialized = execute("json_data", {"operation": "stringify", "value": extracted["value"]})
    assert json.loads(serialized["json"]) == value


def test_json_pointer_distinguishes_missing_paths_from_null_and_supports_escaped_keys():
    value = {"a/b": {"~name": [None, {"标题": "完成"}]}}
    result = execute("json_data", {"operation": "extract", "value": value, "pointer": "/a~1b/~0name/1/标题"})
    assert result["value"] == "完成"
    parsed = execute("json_data", {"operation": "parse", "value": json.dumps(value)})
    result = execute("json_data", {"operation": "extract", "value": parsed["value"], "pointer": "/a~1b/~0name/0"})
    assert result["value"] is None
    with pytest.raises(ValueError, match="不存在"):
        execute("json_data", {"operation": "extract", "value": value, "pointer": "/a~1b/~0name/2"})


@pytest.mark.parametrize("value", [float("nan"), {1: "value"}, {"nested": {1, 2}}])
def test_json_serialization_rejects_values_that_would_lose_types(value):
    with pytest.raises(ValueError):
        execute("json_data", {"operation": "stringify", "value": value})


def test_csv_object_records_round_trip_quoted_multiline_and_empty_cells():
    records = [{"姓名": "甲,乙", "内容": '第一行\n"第二行"', "数量": 2}, {"姓名": "丙", "内容": "", "数量": None}]
    serialized = execute("csv_data", {"operation": "stringify", "value": records})
    assert serialized["columns"] == ["姓名", "内容", "数量"]
    parsed = execute("csv_data", {"value": "\ufeff" + serialized["text"]})
    assert parsed["records"] == [{"姓名": "甲,乙", "内容": '第一行\n"第二行"', "数量": "2"}, {"姓名": "丙", "内容": "", "数量": ""}]
    assert parsed["row_count"] == 2
    header_only = execute("csv_data", {"operation": "stringify", "value": [], "columns": parsed["columns"]})
    assert execute("csv_data", {"value": header_only["text"]})["columns"] == parsed["columns"]


def test_csv_arrays_support_custom_delimiters_and_explicit_columns():
    params = {"operation": "stringify", "value": '[["01",true],["02",false]]', "has_header": False, "delimiter": ";"}
    serialized = execute("csv_data", params)
    assert serialized["text"] == "01;true\r\n02;false\r\n"
    assert execute("csv_data", {"value": serialized["text"], "has_header": False, "delimiter": ";"})["records"] == [["01", "true"], ["02", "false"]]
    serialized = execute("csv_data", {**params, "has_header": True, "columns": '["id","active"]'})
    assert execute("csv_data", {"value": serialized["text"], "delimiter": ";"})["records"] == [{"id": "01", "active": "true"}, {"id": "02", "active": "false"}]


@pytest.mark.parametrize(
    "params",
    [
        {"value": "name,name\na,b\n"},
        {"value": "name,value\na,b,c\n"},
        {"operation": "stringify", "value": [{"name": "a", "value": "b"}], "columns": ["name"]},
        {"operation": "stringify", "value": [{"name": ["a", "b"]}]},
    ],
)
def test_csv_rejects_shapes_that_would_discard_cells(params):
    with pytest.raises(ValueError):
        execute("csv_data", params)


def test_datetime_offset_crosses_leap_day_and_converts_timezone():
    result = execute("datetime_format", {"value": "2024-02-28T23:30:00+08:00", "timezone": "utc", "offset": 1, "offset_unit": "days", "format": "%Y%m%d_%H%M"})
    assert result["iso"] == "2024-02-29T15:30:00+00:00"
    assert result["text"] == "20240229_1530"
    assert result["date"] == "2024-02-29"
    assert result["time"] == "15:30:00+00:00"
    assert result["timestamp"] == 1709220600
    prior = execute("datetime_format", {"value": result["iso"], "timezone": "preserve", "offset": -90, "offset_unit": "minutes"})
    assert prior["timestamp"] == result["timestamp"] - 5400
    from_stamp = execute("datetime_format", {"input_type": "timestamp", "timestamp": result["timestamp"], "timezone": "utc"})
    assert from_stamp["iso"] == result["iso"]


def test_datetime_empty_input_uses_current_time(monkeypatch):
    meta, module = load_action("datetime_format")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 5, 10, 15, tzinfo=timezone.utc)

    monkeypatch.setattr(module, "datetime", Clock)
    result = invoke_action(module.run, module, meta, {"timezone": "utc"}, {})
    assert result["iso"] == "2026-09-05T10:15:00+00:00"


@pytest.mark.parametrize("form_mode", [False, True])
def test_url_encoding_round_trips_unicode_spaces_and_literal_plus(form_mode):
    value = "中文 / a+b?x=1&y=%"
    encoded = execute("url_codec", {"value": value, "form_mode": form_mode})
    assert "/" not in encoded["text"]
    assert "+" in encoded["text"] if form_mode else "%20" in encoded["text"]
    decoded = execute("url_codec", {"operation": "decode", "value": encoded["text"], "form_mode": form_mode})
    assert decoded["text"] == value


def test_url_query_generation_and_parsing_keep_repeated_and_empty_values():
    query = {"tag": ["甲 乙", "a+b"], "empty": None, "active": True, "page": 2}
    encoded = execute("url_codec", {"operation": "query_encode", "value": query})
    parsed = execute("url_codec", {"operation": "parse", "value": "https://example.com:8443/a%2Fb?" + encoded["text"] + "#part"})
    assert parsed["query"] == {"tag": ["甲 乙", "a+b"], "empty": [""], "active": ["true"], "page": ["2"]}
    assert parsed["scheme"] == "https"
    assert parsed["hostname"] == "example.com"
    assert parsed["port"] == 8443
    assert parsed["path"] == "/a%2Fb"
    assert parsed["fragment"] == "part"
    composed = execute("url_codec", {"operation": "compose", "value": parsed})
    assert composed == parsed
    ipv6 = execute("url_codec", {"operation": "compose", "value": {**parsed, "hostname": "::1"}})
    assert ipv6["hostname"] == "::1" and ipv6["port"] == 8443


def test_data_actions_bind_structured_values_and_keep_content_out_of_summaries():
    actions = [
        {"type": "json_data", "binding_id": "a_parse001", "params": {"value": '{"rows":[{"name":"甲","count":2}]}'}},
        {"type": "json_data", "binding_id": "a_extract1", "params": {"operation": "extract", "value": {"$ref": {"scope": "step", "node": "a_parse001", "path": ["value"]}}, "pointer": "/rows"}},
        {"type": "csv_data", "binding_id": "a_csv00001", "params": {"operation": "stringify", "value": {"$ref": {"scope": "step", "node": "a_extract1", "path": ["value"]}}}},
        {"type": "text_transform", "binding_id": "a_text0001", "params": {"operation": "trim", "text": {"$ref": {"scope": "step", "node": "a_csv00001", "path": ["text"]}}}},
    ]
    rule = {"name": "数据转换", "condition": {"type": "manual", "binding_id": "t_manual01", "params": {}}, "actions": actions}
    events = []
    engine = create_test_engine({"rules": []}, on_event=lambda name, data: events.append((name, data)))
    for plugin_id in {action["type"] for action in actions}:
        meta, module = load_action(plugin_id)
        engine.actions_meta[plugin_id] = meta
        engine.actions_funcs[plugin_id] = module.run
        engine._plugin_modules[plugin_id] = module
    assert validate_rule_bindings(rule, {"manual": {"params": [], "outputs": []}}, engine.actions_meta) == []
    context = build_context(rule["name"], "manual", {}, [])
    engine.execute_workflow("data_actions", rule, rule["name"], context)
    assert context["steps"]["a_text0001"]["result"]["text"] == "name,count\r\n甲,2"
    executed = [data for name, data in events if name == "action_executed"]
    assert len(executed) == 4
    assert all(data["status"] == "ok" for data in executed)
    assert "甲" not in repr(events)
