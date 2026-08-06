"""引擎日志的解析、轮转、发射与诊断汇总"""

import json
import os
import time

import pytest

from notmyfault.core import logging as englog


class TestParseLogLine:
    def test_empty_line_returns_none(self):
        assert englog.parse_log_line("") is None

    def test_whitespace_only_returns_none(self):
        assert englog.parse_log_line("   \n") is None

    def test_no_bracket_start_returns_none(self):
        assert englog.parse_log_line("hello world") is None

    def test_no_closing_bracket_for_ts(self):
        assert englog.parse_log_line("[2025-01-01 10:00:00 no close") is None

    def test_ts_minimal(self):
        entry = englog.parse_log_line("[t] msg")
        assert entry == {"ts": "t", "level": "INFO", "text": "msg", "data": None}

    def test_ts_with_spaces(self):
        entry = englog.parse_log_line("[2025-01-01 10:00:00] msg")
        assert entry["ts"] == "2025-01-01 10:00:00"

    def test_info_format(self):
        entry = englog.parse_log_line("[2025-01-01 10:00:00] [INFO] 装载Trigger")
        assert entry["level"] == "INFO"
        assert entry["text"] == "装载Trigger"
        assert entry["data"] is None

    def test_warn_format(self):
        entry = englog.parse_log_line("[ts] [WARN] something odd")
        assert entry["level"] == "WARN"
        assert entry["text"] == "something odd"

    def test_info_multiline_payload(self):
        entry = englog.parse_log_line("[ts] [INFO] line1\nline2")
        assert entry["level"] == "INFO"
        assert "line1\nline2" in entry["text"]

    def test_error_json_format(self):
        payload = json.dumps(
            {"event": "plugin_load_failed", "plugin": "p1", "reason": "bad"},
            ensure_ascii=False,
        )
        entry = englog.parse_log_line(f"[ts] [ERROR] {payload}")
        assert entry["level"] == "ERROR"
        assert entry["data"]["event"] == "plugin_load_failed"
        assert entry["text"] == "[plugin_load_failed] p1: bad"

    def test_error_json_with_rule_field(self):
        payload = json.dumps({"event": "rule_issue", "rule": "规则1", "reason": "缺失"})
        entry = englog.parse_log_line(f"[ts] [ERROR] {payload}")
        assert entry["text"] == "[rule_issue] 规则1: 缺失"

    def test_error_json_without_plugin(self):
        payload = json.dumps({"event": "hot_reload_error", "reason": "解析失败"})
        entry = englog.parse_log_line(f"[ts] [ERROR] {payload}")
        assert entry["text"] == "[hot_reload_error] 解析失败"

    def test_error_plain_text(self):
        entry = englog.parse_log_line("[ts] [ERROR] plain text")
        assert entry["level"] == "ERROR"
        assert entry["text"] == "plain text"
        assert entry["data"] is None

    def test_error_with_corrupt_json(self):
        entry = englog.parse_log_line('[ts] [ERROR] {"event": broken')
        assert entry["level"] == "ERROR"
        assert entry["data"] is None
        assert entry["text"] == '{"event": broken'

    def test_old_format_no_level(self):
        entry = englog.parse_log_line("[ts] legacy message")
        assert entry["level"] == "INFO"
        assert entry["text"] == "legacy message"

    def test_old_format_bracket_in_payload(self):
        # 正文自带非级别方括号时保持 INFO 且正文原样保留
        entry = englog.parse_log_line("[ts] [进程] started")
        assert entry["level"] == "INFO"
        assert entry["text"] == "[进程] started"

    def test_chinese_characters(self):
        entry = englog.parse_log_line("[ts] [INFO] 中文日志内容")
        assert entry["text"] == "中文日志内容"


class TestReadLogEntries:
    def test_read_file_not_found(self, tmp_path):
        assert englog.read_log_entries(str(tmp_path / "missing.log")) == []

    def test_read_default_lines(self, tmp_path):
        path = tmp_path / "a.log"
        path.write_text(
            "".join(f"[ts{i}] [INFO] msg{i}\n" for i in range(10)),
            encoding="utf-8",
        )
        entries = englog.read_log_entries(str(path))
        assert len(entries) == 10
        assert entries[0]["text"] == "msg0"

    def test_read_last_n_lines(self, tmp_path):
        path = tmp_path / "a.log"
        path.write_text(
            "".join(f"[ts{i}] [INFO] msg{i}\n" for i in range(10)),
            encoding="utf-8",
        )
        entries = englog.read_log_entries(str(path), lines=3)
        assert [e["text"] for e in entries] == ["msg7", "msg8", "msg9"]


def _make_log(directory, name, mtime):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[ts] [INFO] x\n")
    os.utime(path, (mtime, mtime))
    return path


class TestLogRotation:
    def test_init_session_log_creates_file(self, tmp_path):
        path = englog.init_session_log(str(tmp_path))
        assert os.path.dirname(path) == str(tmp_path)
        assert os.path.basename(path).startswith("engine-")
        assert path.endswith(".log")

    def test_init_session_log_calls_rotation(self, tmp_path):
        base = time.time() - 1000
        for i in range(10):
            _make_log(str(tmp_path), f"engine-20250101-100{i:03d}.log", base + i)
        englog.init_session_log(str(tmp_path))
        remaining = [f for f in os.listdir(tmp_path) if f.endswith(".log")]
        assert len(remaining) == 7

    def test_rotate_empty_dir(self, tmp_path):
        englog._rotate_logs(str(tmp_path))

    def test_rotate_keeps_n_most_recent(self, tmp_path):
        base = time.time() - 1000
        paths = [
            _make_log(str(tmp_path), f"engine-20250101-100{i:03d}.log", base + i)
            for i in range(10)
        ]
        englog._rotate_logs(str(tmp_path), keep=3)
        remaining = set(os.listdir(tmp_path))
        assert remaining == {os.path.basename(p) for p in paths[-3:]}

    def test_rotate_fewer_than_keep(self, tmp_path):
        base = time.time() - 100
        for i in range(3):
            _make_log(str(tmp_path), f"engine-{i}.log", base + i)
        englog._rotate_logs(str(tmp_path), keep=7)
        assert len(os.listdir(tmp_path)) == 3

    def test_rotate_custom_keep(self, tmp_path):
        base = time.time() - 100
        for i in range(5):
            _make_log(str(tmp_path), f"engine-{i}.log", base + i)
        englog._rotate_logs(str(tmp_path), keep=2)
        assert len(os.listdir(tmp_path)) == 2

    def test_get_latest_log_empty_dir(self, tmp_path):
        assert englog.get_latest_log(str(tmp_path)) is None

    def test_get_latest_log_returns_most_recent(self, tmp_path):
        base = time.time() - 100
        _make_log(str(tmp_path), "engine-old.log", base)
        newest = _make_log(str(tmp_path), "engine-new.log", base + 50)
        assert englog.get_latest_log(str(tmp_path)) == newest

    def test_list_logs_returns_metadata(self, tmp_path):
        _make_log(str(tmp_path), "engine-a.log", time.time())
        logs = englog.list_logs(str(tmp_path))
        assert len(logs) == 1
        entry = logs[0]
        assert entry["name"] == "engine-a.log"
        assert entry["lines"] == 1
        assert entry["size"] > 0
        assert entry["mtime"] > 0
        assert entry["path"].endswith("engine-a.log")

    def test_list_logs_io_error_graceful(self, tmp_path, monkeypatch):
        _make_log(str(tmp_path), "engine-a.log", time.time())
        real_getsize = os.path.getsize

        def fake_getsize(path):
            raise OSError("denied")

        monkeypatch.setattr(os.path, "getsize", fake_getsize)
        logs = englog.list_logs(str(tmp_path))
        monkeypatch.setattr(os.path, "getsize", real_getsize)
        assert logs[0]["size"] == 0
        assert logs[0]["lines"] == 0


class TestLogEmitters:
    def test_engine_info_emits(self, capsys):
        englog.engine_info("hello info")
        out = capsys.readouterr().out
        assert "[INFO] hello info" in out

    def test_engine_warn_emits(self, capsys):
        englog.engine_warn("hello warn")
        out = capsys.readouterr().out
        assert "[WARN] hello warn" in out

    def test_engine_error_emits_json(self, capsys):
        englog.engine_error("plugin_load_failed", plugin="p", reason="r")
        out = capsys.readouterr().out
        assert out.startswith("[ERROR] ")
        data = json.loads(out[len("[ERROR] "):].strip())
        assert data["event"] == "plugin_load_failed"
        assert data["plugin"] == "p"


def _entry(level, text=None, data=None):
    return {
        "ts": "ts",
        "level": level,
        "text": text or (data.get("event", "") if data else ""),
        "data": data,
    }


class TestBuildDiagnostics:
    def test_all_keys_present(self):
        diag = englog.build_diagnostics([])
        assert set(diag) == {
            "error_count", "warn_count", "plugin_errors", "rule_issues",
            "action_fails", "hot_reload_errors", "last_errors", "last_warns",
        }

    def test_empty_entries(self):
        diag = englog.build_diagnostics([])
        assert diag["error_count"] == 0
        assert diag["warn_count"] == 0
        assert diag["plugin_errors"] == []
        assert diag["rule_issues"] == []
        assert diag["action_fails"] == 0
        assert diag["hot_reload_errors"] == 0

    def test_counts_errors_and_warns(self):
        entries = [
            _entry("ERROR", "e1"),
            _entry("ERROR", "e2"),
            _entry("WARN", "w1"),
        ]
        diag = englog.build_diagnostics(entries)
        assert diag["error_count"] == 2
        assert diag["warn_count"] == 1

    def test_only_info_entries(self):
        diag = englog.build_diagnostics([_entry("INFO", "fine")])
        assert diag["error_count"] == 0
        assert diag["warn_count"] == 0
        assert diag["last_errors"] == []

    def test_plugin_load_failed(self):
        entries = [
            _entry(
                "ERROR",
                data={
                    "event": "plugin_load_failed",
                    "plugin": "p1",
                    "type": "Trigger",
                    "reason": "bad",
                },
            )
        ]
        diag = englog.build_diagnostics(entries)
        assert diag["plugin_errors"] == [
            {"plugin": "p1", "type": "Trigger", "reason": "bad"}
        ]

    def test_rule_issue(self):
        entries = [
            _entry("ERROR", data={"event": "rule_issue", "rule": "r1", "issue": "i"})
        ]
        diag = englog.build_diagnostics(entries)
        assert diag["rule_issues"] == [{"rule": "r1", "issue": "i"}]

    def test_action_failed(self):
        entries = [_entry("ERROR", data={"event": "action_failed"})]
        diag = englog.build_diagnostics(entries)
        assert diag["action_fails"] == 1

    def test_hot_reload_error(self):
        entries = [
            _entry("ERROR", data={"event": "hot_reload_error"}),
            _entry("ERROR", data={"event": "hot_reload_error"}),
        ]
        diag = englog.build_diagnostics(entries)
        assert diag["hot_reload_errors"] == 2

    def test_multiple_error_types(self):
        entries = [
            _entry("ERROR", data={"event": "plugin_load_failed", "plugin": "p"}),
            _entry("ERROR", data={"event": "action_failed"}),
            _entry("WARN", "w"),
        ]
        diag = englog.build_diagnostics(entries)
        assert diag["error_count"] == 2
        assert len(diag["plugin_errors"]) == 1
        assert diag["action_fails"] == 1
        assert diag["warn_count"] == 1

    def test_last_errors_truncated(self):
        entries = [_entry("ERROR", f"e{i}") for i in range(15)]
        diag = englog.build_diagnostics(entries)
        assert len(diag["last_errors"]) == 10
        assert diag["last_errors"][-1] == "e14"
        assert diag["last_errors"][0] == "e5"

    def test_last_warns_truncated(self):
        entries = [_entry("WARN", f"w{i}") for i in range(8)]
        diag = englog.build_diagnostics(entries)
        assert len(diag["last_warns"]) == 5
        assert diag["last_warns"][-1] == "w7"

    def test_no_data_field(self):
        entries = [_entry("ERROR", "plain error")]
        diag = englog.build_diagnostics(entries)
        assert diag["error_count"] == 1
        assert diag["last_errors"] == ["plain error"]
        assert diag["plugin_errors"] == []
