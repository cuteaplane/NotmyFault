from datetime import datetime
from importlib import import_module
import json
from pathlib import Path
import threading

import pytest

from notmyfault.actions.create_directory.action import run_with_context as create_directory
from notmyfault.actions.file_info.action import run_with_context as file_info
from notmyfault.actions.list_files import action as list_files_action
from notmyfault.actions.list_files.action import run_with_context as list_files
from notmyfault.actions.read_text.action import run_with_context as read_text
from notmyfault.actions.write_text.action import run_with_context as write_text
from notmyfault.core.workflow import ActionCancellation, ActionCancelled, build_context
from notmyfault.core.rules import validate_rule_bindings
from notmyfault.tests.api_support import create_test_engine


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk", "utf-16", "utf-32", "gb18030"])
def test_text_file_round_trip_preserves_content_and_encoding(tmp_path, encoding):
    path = tmp_path / "nested" / "文本.txt"
    text = "第一行\r\n第二行\n\n"
    written = write_text({}, {
        "file_path": str(path), "text": text, "encoding": encoding,
        "create_parents": True,
    }, {})
    loaded = read_text({}, {"file_path": str(path), "encoding": encoding}, {})
    assert path.read_bytes() == text.encode(encoding)
    assert loaded["text"] == text
    assert loaded["file"] == written["file"] == str(path)
    assert loaded["bytes"] == written["bytes"] == len(path.read_bytes())
    assert loaded["characters"] == written["characters"] == len(text)


def test_text_write_requires_explicit_overwrite_and_encodes_before_replacing(tmp_path):
    path = tmp_path / "existing.txt"
    path.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        write_text({}, {"file_path": str(path), "text": "replacement"}, {})
    assert path.read_bytes() == b"original"
    with pytest.raises(UnicodeEncodeError):
        write_text({}, {
            "file_path": str(path), "text": "😀", "encoding": "gbk", "overwrite": True,
        }, {})
    assert path.read_bytes() == b"original"
    write_text({}, {"file_path": str(path), "text": "", "overwrite": True}, {})
    assert path.read_bytes() == b""


def test_text_read_removes_utf8_bom_and_rejects_oversized_input(tmp_path):
    path = tmp_path / "text.txt"
    path.write_bytes("内容".encode("utf-8-sig"))
    assert read_text({}, {"file_path": str(path)}, {})["text"] == "内容"
    with pytest.raises(ValueError, match="读取字节上限"):
        read_text({}, {"file_path": str(path), "max_bytes": 3}, {})


def test_file_listing_filters_recursive_paths_and_reports_truncation(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.csv").write_text("b", encoding="utf-8")
    (tmp_path / "reports" / "c.txt").write_text("c", encoding="utf-8")
    (tmp_path / "reports" / "d.txt").write_text("d", encoding="utf-8")
    params = {"directory": str(tmp_path), "pattern": "*.txt"}
    result = list_files({}, params, {})
    assert result["files"] == [str(tmp_path / "a.txt")]
    assert result["count"] == 1
    assert result["truncated"] is False
    result = list_files({}, {**params, "recursive": True, "max_results": 2}, {})
    assert result["files"] == [str(tmp_path / "a.txt"), str(tmp_path / "reports" / "c.txt")]
    assert result["count"] == 2
    assert result["truncated"] is True
    result = list_files({}, {
        **params, "pattern": "reports/*.txt", "recursive": True, "max_results": 2,
    }, {})
    assert result["files"] == [str(tmp_path / "reports" / name) for name in ("c.txt", "d.txt")]
    assert result["truncated"] is False


def test_file_listing_stops_when_runtime_cancels_during_traversal(tmp_path, monkeypatch):
    (tmp_path / "nested").mkdir()
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    (tmp_path / "nested" / "second.txt").write_text("second", encoding="utf-8")
    cancel_event = threading.Event()
    original_walk = list_files_action.os.walk

    def cancel_after_first_directory(*args, **kwargs):
        for entry in original_walk(*args, **kwargs):
            yield entry
            cancel_event.set()

    monkeypatch.setattr(list_files_action.os, "walk", cancel_after_first_directory)
    context = {"runtime": {"cancellation": ActionCancellation(cancel_event)}}
    with pytest.raises(ActionCancelled):
        list_files({}, {"directory": str(tmp_path), "recursive": True}, context)


def test_file_info_distinguishes_missing_files_and_directories(tmp_path):
    path = tmp_path / "文本.txt"
    missing = file_info({}, {"file_path": str(path)}, {})
    assert missing["exists"] is False
    assert missing["modified_at"] == ""
    assert missing["modified_time"] is None
    path.write_bytes(b"example")
    existing = file_info({}, {"file_path": str(path)}, {})
    assert existing["exists"] is True
    assert existing["is_file"] is True
    assert existing["is_directory"] is False
    assert existing["name"] == "文本.txt"
    assert existing["extension"] == ".txt"
    assert existing["size_bytes"] == 7
    assert datetime.fromisoformat(existing["modified_at"]).utcoffset().total_seconds() == 0
    assert existing["modified_time"] == existing["modified_at"]
    directory = file_info({}, {"file_path": str(tmp_path)}, {})
    assert directory["is_directory"] is True
    assert directory["is_file"] is False
    assert directory["size_bytes"] == 0


def test_directory_creation_reports_existing_directories_and_rejects_files(tmp_path):
    path = tmp_path / "parent" / "child"
    assert create_directory({}, {"directory": str(path)}, {}) == {
        "path": str(path), "created": True,
    }
    assert path.is_dir()
    assert create_directory({}, {"directory": str(path)}, {})["created"] is False
    file_path = tmp_path / "file"
    file_path.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        create_directory({}, {"directory": str(file_path)}, {})
    assert file_path.read_bytes() == b"original"


def test_file_actions_use_paths_and_content_from_trigger_data(tmp_path):
    actions_root = Path(__file__).parents[1] / "actions"
    modules = {name: import_module(f"notmyfault.actions.{name}.action") for name in ("create_directory", "write_text", "read_text")}
    metas = {name: json.loads((actions_root / name / "action.json").read_text(encoding="utf-8")) for name in modules}
    payload = {"directory": str(tmp_path / "任务"), "file": str(tmp_path / "任务" / "结果.txt"), "text": "动态内容"}
    reference = lambda key: {"$ref": {"scope": "event", "path": [key]}}
    actions = [
        {"type": "create_directory", "binding_id": "a_createdir", "params": {"directory": reference("directory")}},
        {"type": "write_text", "binding_id": "a_writetext", "params": {"file_path": reference("file"), "text": reference("text")}},
        {"type": "read_text", "binding_id": "a_readtext", "params": {"file_path": {"$ref": {"scope": "step", "node": "a_writetext", "path": ["file"]}}}},
    ]
    trigger = {"outputs": [{"name": name, "type": "string", "value_type": "text" if name == "text" else "path"} for name in payload]}
    rule = {"name": "按事件内容保存文件", "condition": {"type": "manual", "binding_id": "t_manual01", "params": {}}, "actions": actions}
    assert validate_rule_bindings(rule, {"manual": trigger}, metas) == []
    engine = create_test_engine({"rules": []})
    for name, module in modules.items():
        engine.actions_funcs[name] = module.run
        engine.actions_meta[name] = metas[name]
        engine._plugin_modules[name] = module
    context = build_context(rule["name"], "manual", payload, [])
    engine.execute_workflow("file_actions", rule, rule["name"], context)
    assert [context["steps"][action["binding_id"]]["status"] for action in actions] == ["ok"] * 3
    result = context["steps"]["a_readtext"]["result"]
    assert result["text"] == payload["text"]
    assert Path(result["file"]).read_text(encoding="utf-8") == payload["text"]
