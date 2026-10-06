import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from notmyfault.core.rules import validate_rule_bindings
from notmyfault.core.workflow import build_context, invoke_action
from notmyfault.tests.api_support import create_test_engine, event_fields


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


@pytest.mark.parametrize("header_prefix", ["", "\ufeff", "\ufeff\ufeff"])
@pytest.mark.parametrize("input_bom", ["", "\ufeff"])
@pytest.mark.parametrize("content_length", [0, 131073])
def test_csv_object_records_round_trip_quoted_multiline_and_empty_cells(header_prefix, input_bom, content_length, tmp_path):
    name_column = header_prefix + "姓名"
    content = '第一行\n"第二行"' + "文" * content_length
    records = [{name_column: "甲,乙", "内容": content, "数量": 2}, {name_column: "丙", "内容": "", "数量": None}]
    serialized = execute("csv_data", {"operation": "stringify", "value": records})
    assert serialized["columns"] == [name_column, "内容", "数量"]
    parsed = execute("csv_data", {"value": input_bom + serialized["text"]})
    assert parsed["records"] == [{name_column: "甲,乙", "内容": content, "数量": "2"}, {name_column: "丙", "内容": "", "数量": ""}]
    assert parsed["row_count"] == 2
    file_path = tmp_path / "quoted.csv"
    file_path.write_bytes((input_bom + serialized["text"]).encode("utf-8"))
    loaded = execute("read_text", {"file_path": str(file_path)})
    assert execute("csv_data", {"value": loaded["text"]})["records"] == parsed["records"]
    header_only = execute("csv_data", {"operation": "stringify", "value": [], "columns": parsed["columns"]})
    assert execute("csv_data", {"value": header_only["text"]})["columns"] == parsed["columns"]


@pytest.mark.parametrize("cell_prefix", ["", "\ufeff", "\ufeff\ufeff"])
@pytest.mark.parametrize("input_bom", ["", "\ufeff"])
@pytest.mark.parametrize("cell_text", ["01", ""])
@pytest.mark.parametrize("delimiter", [";", "\ufeff"])
@pytest.mark.parametrize("row_count", [1, 2])
def test_csv_arrays_support_custom_delimiters_and_explicit_columns(cell_prefix, input_bom, cell_text, delimiter, row_count, tmp_path):
    first_cell = cell_prefix + cell_text
    records = [[first_cell, True], ["02", False]][:row_count]
    params = {"operation": "stringify", "value": json.dumps(records), "has_header": False, "delimiter": delimiter}
    serialized = execute("csv_data", params)
    if not cell_prefix and cell_text == "01" and delimiter == ";" and row_count == 2:
        assert serialized["text"] == "01;true\r\n02;false\r\n"
    expected = [[first_cell, "true"], ["02", "false"]][:row_count]
    assert execute("csv_data", {"value": input_bom + serialized["text"], "has_header": False, "delimiter": delimiter})["records"] == expected
    file_path = tmp_path / "records.csv"
    file_path.write_bytes((input_bom + serialized["text"]).encode("utf-8"))
    loaded = execute("read_text", {"file_path": str(file_path)})
    assert execute("csv_data", {"value": loaded["text"], "has_header": False, "delimiter": delimiter})["records"] == expected
    if input_bom and cell_prefix and delimiter != "\ufeff":
        unquoted = input_bom + first_cell + delimiter + "true\r\n"
        if row_count == 2:
            unquoted += "02" + delimiter + "false\r\n"
        assert execute("csv_data", {"value": unquoted, "has_header": False, "delimiter": delimiter})["records"] == expected
    serialized = execute("csv_data", {**params, "has_header": True, "columns": '["id","active"]'})
    assert execute("csv_data", {"value": input_bom + serialized["text"], "delimiter": delimiter})["records"] == [{"id": first_cell, "active": "true"}, {"id": "02", "active": "false"}][:row_count]


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


@pytest.mark.parametrize(
    ("path", "fragment", "encoded_path", "encoded_fragment"),
    [
        ("/a%2Fb", "part", "/a%2Fb", "part"),
        ("/a!$&'()*+,;=:@%2f", "part/?:@!$&'()*+,;=%23", "/a!$&'()*+,;=:@%2f", "part/?:@!$&'()*+,;=%23"),
        ("/reports?final.csv", "part", "/reports%3Ffinal.csv", "part"),
        ("/a#b", "part", "/a%23b", "part"),
        ("/甲 乙\t\n\r丙", "part", "/%E7%94%B2%20%E4%B9%99%09%0A%0D%E4%B8%99", "part"),
        ("/a%2Fb", "part 甲\n?#tail", "/a%2Fb", "part%20%E7%94%B2%0A?%23tail"),
        ("/report:final.txt", "part", "/report:final.txt", "part"),
        ("/a:b/c:d", "part", "/a:b/c:d", "part"),
    ],
)
def test_url_query_generation_and_parsing_keep_repeated_and_empty_values(path, fragment, encoded_path, encoded_fragment):
    query = {"tag": ["甲 乙", "a+b"], "empty": None, "active": True, "page": 2}
    encoded = execute("url_codec", {"operation": "query_encode", "value": query})
    parsed = execute("url_codec", {"operation": "parse", "value": "https://example.com:8443" + encoded_path + "?" + encoded["text"] + "#" + encoded_fragment})
    assert parsed["query"] == {"tag": ["甲 乙", "a+b"], "empty": [""], "active": ["true"], "page": ["2"]}
    assert parsed["scheme"] == "https"
    assert parsed["hostname"] == "example.com"
    assert parsed["port"] == 8443
    assert parsed["path"] == encoded_path
    assert parsed["fragment"] == encoded_fragment
    composed = execute("url_codec", {"operation": "compose", "value": parsed})
    assert composed == parsed
    from_parts = execute("url_codec", {"operation": "compose", "value": {**parsed, "path": path, "fragment": fragment}})
    assert from_parts == parsed
    ipv6 = execute("url_codec", {"operation": "compose", "value": {**parsed, "hostname": "::1"}})
    assert ipv6["hostname"] == "::1" and ipv6["port"] == 8443
    first, separator, rest = encoded_path.lstrip("/").partition("/")
    relative_path = first.replace(":", "%3A") + separator + rest
    relative_value = {"path": path.lstrip("/"), "query": query, "fragment": fragment}
    for value in (relative_value, json.dumps(relative_value)):
        relative = execute("url_codec", {"operation": "compose", "value": value})
        assert relative == {**parsed, "scheme": "", "hostname": "", "port": None, "path": relative_path,
                            "text": relative_path + "?" + encoded["text"] + "#" + encoded_fragment}
        assert execute("url_codec", {"operation": "compose", "value": relative}) == relative
    opaque = execute("url_codec", {"operation": "compose", "value": {"scheme": "URN", "path": "example:animal:ferret"}})
    assert opaque["scheme"] == "urn" and opaque["path"] == "example:animal:ferret"
    for scheme in ("invalid scheme", "1https", "http:", "http/", "http_", "中文", "http\n", "http\0"):
        with pytest.raises(ValueError):
            execute("url_codec", {"operation": "compose", "value": {**parsed, "scheme": scheme}})


def test_data_actions_bind_structured_values_and_keep_content_out_of_summaries():
    actions = [
        {"type": "json_data", "binding_id": "a_parse001", "params": {"value": '{"rows":[{"name":"甲","count":2}]}'}},
        {"type": "json_data", "binding_id": "a_extract1", "params": {"operation": "extract", "value": {"$ref": {"scope": "step", "node": "a_parse001", "path": ["value"]}}, "pointer": "/rows"}},
        {"type": "csv_data", "binding_id": "a_csv00001", "params": {"operation": "stringify", "value": {"$ref": {"scope": "step", "node": "a_extract1", "path": ["value"]}}}},
        {"type": "text_transform", "binding_id": "a_text0001", "params": {"operation": "trim", "text": {"$ref": {"scope": "step", "node": "a_csv00001", "path": ["text"]}}}},
    ]
    rule = {"name": "数据转换", "condition": {"type": "manual", "binding_id": "t_manual01", "params": {}}, "actions": actions}
    events = []
    engine = create_test_engine({"rules": []}, on_event=lambda event: events.append((event.type, event_fields(event))))
    for plugin_id in {action["type"] for action in actions}:
        meta, module = load_action(plugin_id)
        engine.actions_meta[plugin_id] = meta
        engine.actions_funcs[plugin_id] = module.run
        engine._plugin_modules[plugin_id] = module
    assert validate_rule_bindings(rule, {"manual": {"params": [], "outputs": []}}, engine.actions_meta) == []
    context = build_context(rule["name"], "manual", {}, [])
    engine.execute_workflow("data_actions", rule, rule["name"], context)
    assert context["steps"]["a_text0001"]["result"]["text"] == "name,count\r\n甲,2"
    assert engine.events.flush()
    executed = [data for name, data in events if name == "run.step.succeeded"]
    assert len(executed) == 4
    assert all(data["status"] == "ok" for data in executed)
    assert engine.events.flush()
    assert "甲" not in repr(events)
