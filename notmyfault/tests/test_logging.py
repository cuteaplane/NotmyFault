import json

import pytest

from notmyfault.core import logging as englog
from notmyfault.values import Secret


@pytest.mark.parametrize("line", [
    "", "plain output", "[INFO] output", "[2025-01-01 10:00:00] output",
    "[2025-01-01 10:00:00] [DEBUG] output", "[not-a-time] [INFO] output",
])
def test_parser_rejects_missing_timestamp_or_level(line):
    assert englog.parse_log_line(line) is None


@pytest.mark.parametrize("level", ["INFO", "WARN", "ERROR"])
def test_current_level_keeps_nested_markers_as_message_text(level):
    entry = englog.parse_log_line(f"[2025-01-01 10:00:00] [{level}] [INFO] [ERROR] body\n")
    assert entry == {
        "ts": "2025-01-01 10:00:00", "level": level,
        "text": "[INFO] [ERROR] body", "data": None,
    }


@pytest.mark.parametrize("payload", ['[1, 2]', 'null', '"text"', '{invalid'])
def test_non_object_error_payload_keeps_text(payload):
    entry = englog.parse_log_line(f"[2025-01-01 10:00:00] [ERROR] {payload}")
    assert entry["data"] is None
    assert entry["text"] == payload


def test_current_emitter_roundtrip_preserves_error_categories_and_secret_redaction(capsys, tmp_path):
    englog.engine_info("引擎启动")
    englog.engine_warn("[ERROR] 插件输出文字")
    englog.engine_error("plugin_load_failed", plugin="plugin1", reason=Secret("private-token"))
    englog.engine_error("rule_issue", rule="rule1", issue="invalid")
    englog.engine_error("action_failed", plugin="plugin1", reason="failed")
    englog.engine_error("hot_reload_error", reason="invalid")
    raw = capsys.readouterr().out
    assert "private-token" not in raw
    log_path = tmp_path / "engine.log"
    log_path.write_text("\n".join(f"[2025-01-01 10:00:00] {line}" for line in raw.splitlines()), encoding="utf-8")
    entries = englog.read_log_entries(str(log_path))
    assert [entry["level"] for entry in entries] == ["INFO", "WARN", "ERROR", "ERROR", "ERROR", "ERROR"]
    assert entries[2]["data"]["reason"] == "***"
    assert entries[2]["text"] == "[plugin_load_failed] plugin1: ***"
    diagnostics = englog.build_diagnostics(entries)
    assert diagnostics["error_count"] == 4
    assert diagnostics["warn_count"] == 1
    assert diagnostics["plugin_errors"] == [{"plugin": "plugin1", "type": "?", "reason": "***"}]
    assert diagnostics["rule_issues"] == [{"rule": "rule1", "issue": "invalid"}]
    assert diagnostics["action_fails"] == diagnostics["hot_reload_errors"] == 1
    assert "private-token" not in json.dumps(diagnostics)
