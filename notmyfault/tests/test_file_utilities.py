from datetime import datetime
from importlib import import_module
import json
import os
from pathlib import Path
import threading

import pytest

from notmyfault.actions.create_directory.action import run_with_context as create_directory
from notmyfault.actions.file_operation import action as file_operation
from notmyfault.actions.file_info.action import run_with_context as file_info
from notmyfault.actions.list_files import action as list_files_action
from notmyfault.actions.list_files.action import run_with_context as list_files
from notmyfault.actions.read_text.action import run_with_context as read_text
from notmyfault.actions.write_text.action import run_with_context as write_text
from notmyfault.core.workflow import ActionCancellation, ActionCancelled, build_context
from notmyfault.core.rules import validate_rule_bindings
from notmyfault.tests.canvas_fixtures import native_rule
from notmyfault.tests.api_support import create_test_engine
from notmyfault.values import plain_value


@pytest.mark.parametrize("operation", ["copy", "move"])
@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("overwrite", [False, True])
def test_file_copy_and_move_respect_overwrite(tmp_path, monkeypatch, operation, directory, existing, overwrite):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    source_parent = tmp_path / " source"
    source_parent.mkdir()
    source = source_parent / ("任务" if directory else "内容.txt")
    destination_parent = tmp_path / "destination\u3000"
    destination_parent.mkdir()
    target = destination_parent / source.name
    destination = target if directory and operation == "copy" else destination_parent
    if directory:
        source.mkdir()
        (source / "nested").mkdir()
        source_content = source / "nested" / "content.txt"
        target_content = target / "nested" / "content.txt"
        if existing:
            target_content.parent.mkdir(parents=True)
            (target / "keep.txt").write_bytes(b"keep")
    else:
        source_content = source
        target_content = target
    source_content.write_bytes(b"new")
    if existing:
        target_content.write_bytes(b"old")
    params = {
        "operation": operation, "source": str(source.relative_to(tmp_path)), "destination": str(destination.relative_to(tmp_path)),
        "overwrite": overwrite,
    }
    if existing and not overwrite:
        with pytest.raises(FileExistsError):
            file_operation.run_with_context({}, params, {})
        assert source_content.read_bytes() == b"new"
        assert target_content.read_bytes() == b"old"
    else:
        file_operation.run_with_context({}, params, {})
        assert target_content.read_bytes() == b"new"
        assert source.exists() is (operation == "copy")
    if directory and existing:
        assert (target / "keep.txt").read_bytes() == b"keep"


@pytest.mark.parametrize("fail_during_merge", [False, True])
def test_directory_move_keeps_links_and_remaining_source_on_failure(tmp_path, monkeypatch, fail_during_merge):
    source = tmp_path / "source" / "batch"
    source.mkdir(parents=True)
    destination = tmp_path / "destination"
    target = destination / source.name
    target.mkdir(parents=True)
    (target / "keep.txt").write_bytes(b"keep")
    (source / "part.txt").write_bytes(b"part")
    (source / "rest.txt").write_bytes(b"rest")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.txt").write_bytes(b"external")
    link = source / "linked"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    moved = []
    original_move = file_operation.shutil.move

    def fail_after_first(source_path, target_path):
        if moved:
            raise PermissionError("目标不可写")
        result = original_move(source_path, target_path)
        moved.append(Path(source_path).name)
        return result

    if fail_during_merge:
        monkeypatch.setattr(file_operation.shutil, "move", fail_after_first)
    try:
        params = {
            "operation": "move", "source": str(source), "destination": str(destination),
            "overwrite": True,
        }
        if fail_during_merge:
            with pytest.raises(PermissionError):
                file_operation.run_with_context({}, params, {})
            assert source.is_dir()
            assert len(moved) == 1
        else:
            file_operation.run_with_context({}, params, {})
            assert not source.exists()
        for name, content in (("part.txt", b"part"), ("rest.txt", b"rest"), ("linked", None)):
            source_path, target_path = source / name, target / name
            assert source_path.exists() != target_path.exists()
            remaining = source_path if source_path.exists() else target_path
            if content is None:
                assert (remaining / "external.txt").read_bytes() == b"external"
            else:
                assert remaining.read_bytes() == content
        assert (target / "keep.txt").read_bytes() == b"keep"
        assert list(outside.iterdir()) == [outside / "external.txt"]
        assert (outside / "external.txt").read_bytes() == b"external"
    finally:
        remaining_link = link if link.exists() else target / "linked"
        remaining_link.rmdir() if os.name == "nt" else remaining_link.unlink()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk", "utf-16", "utf-32", "gb18030"])
@pytest.mark.parametrize("prefix,suffix", [("", ""), (" ", ""), ("", "\u3000")])
def test_text_file_round_trip_preserves_content_and_encoding(tmp_path, monkeypatch, encoding, prefix, suffix):
    monkeypatch.chdir(tmp_path)
    relative = Path(prefix + "nested") / ("文本.txt" + suffix)
    path = tmp_path / relative
    text = "第一行\r\n第二行\n\n"
    written = write_text({}, {
        "file_path": str(relative), "text": text, "encoding": encoding,
        "create_parents": True,
    }, {})
    loaded = read_text({}, {"file_path": str(relative), "encoding": encoding}, {})
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
    with pytest.raises(ValueError, match="无法表示"):
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


@pytest.mark.parametrize("directory", ["root", " root", "root\u3000"])
def test_file_listing_filters_recursive_paths_and_reports_truncation(tmp_path, monkeypatch, directory):
    monkeypatch.chdir(tmp_path)
    tmp_path = tmp_path / directory
    tmp_path.mkdir()
    (tmp_path / "reports").mkdir()
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.csv").write_text("b", encoding="utf-8")
    (tmp_path / "reports" / "c.txt").write_text("c", encoding="utf-8")
    (tmp_path / "reports" / "d.txt").write_text("d", encoding="utf-8")
    params = {"directory": directory, "pattern": "*.txt"}
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


@pytest.mark.parametrize("name", ["文本.txt", " 文本.txt", "文本.txt\u3000"])
def test_file_info_distinguishes_missing_files_and_directories(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / name
    missing = file_info({}, {"file_path": name}, {})
    assert missing["exists"] is False
    assert missing["modified_time"] is None
    assert missing["modified_time"] is None
    path.write_bytes(b"example")
    existing = file_info({}, {"file_path": name}, {})
    assert existing["exists"] is True
    assert existing["is_file"] is True
    assert existing["is_directory"] is False
    assert existing["path"] == str(path)
    assert existing["name"] == name
    assert existing["extension"] == path.suffix
    assert existing["size_bytes"] == 7
    assert datetime.fromisoformat(existing["modified_time"]).utcoffset().total_seconds() == 0
    assert "modified_at" not in existing
    directory = file_info({}, {"file_path": str(tmp_path)}, {})
    assert directory["is_directory"] is True
    assert directory["is_file"] is False
    assert directory["size_bytes"] == 0


@pytest.mark.parametrize("directory", ["parent/child", " parent/child", "parent/child\u3000"])
def test_directory_creation_reports_existing_directories_and_rejects_files(tmp_path, monkeypatch, directory):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / directory
    assert create_directory({}, {"directory": directory}, {}) == {
        "path": str(path), "created": True,
    }
    assert path.is_dir()
    assert create_directory({}, {"directory": directory}, {})["created"] is False
    file_path = tmp_path / "file"
    file_path.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        create_directory({}, {"directory": str(file_path)}, {})
    assert file_path.read_bytes() == b"original"


@pytest.mark.parametrize("directory", ["任务", " 任务", "任务\u3000"])
def test_file_actions_use_paths_and_content_from_trigger_data(tmp_path, monkeypatch, directory):
    monkeypatch.chdir(tmp_path)
    actions_root = Path(__file__).parents[1] / "actions"
    modules = {name: import_module(f"notmyfault.actions.{name}.action") for name in ("create_directory", "write_text", "read_text")}
    metas = {name: json.loads((actions_root / name / "action.json").read_text(encoding="utf-8")) for name in modules}
    payload = {"directory": directory, "file": str(Path(directory) / "结果.txt\u3000"), "text": "动态内容"}
    reference = lambda key: {"$ref": {"scope": "event", "path": [key]}}
    actions = [
        {"type": "create_directory", "binding_id": "a_createdir", "params": {"directory": reference("directory")}},
        {"type": "write_text", "binding_id": "a_writetext", "params": {"file_path": reference("file"), "text": reference("text")}},
        {"type": "read_text", "binding_id": "a_readtext", "params": {"file_path": {"$ref": {"scope": "step", "node": "a_writetext", "path": ["file"]}}}},
    ]
    trigger = {"outputs": [{"name": name, "type": "string", "value_type": "text" if name == "text" else "path"} for name in payload]}
    rule = native_rule({"type": "manual", "binding_id": "t_manual01", "params": {}}, actions, name="按事件内容保存文件")
    assert validate_rule_bindings(rule, {"manual": trigger}, metas) == []
    engine = create_test_engine({"rules": []})
    engine.triggers_meta["manual"] = trigger
    engine.triggers_funcs["manual"] = lambda *args: None
    for name, module in modules.items():
        engine.actions_funcs[name] = module.run_with_context
        engine.actions_meta[name] = metas[name]
        engine._plugin_modules[name] = module
    context = build_context(rule["name"], "manual", payload, [{"binding_id": "t_manual01", "event": {"type": "manual", "params": {}}, "payload": payload}])
    engine.execute_workflow("file_actions", rule, rule["name"], context)
    assert [context["steps"][action["binding_id"]]["status"] for action in actions] == ["ok"] * 3

    result = plain_value(context["steps"]["a_readtext"]["result"])
    assert result["text"] == payload["text"]
    assert result["file"] == str((tmp_path / payload["file"]).absolute())
    assert Path(result["file"]).read_text(encoding="utf-8") == payload["text"]
