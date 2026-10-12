"""archive 动作：ZIP 压缩解压与路径软失败。"""

import builtins
import importlib.util
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

from notmyfault.core import workflow
from notmyfault.core.clock import FakeClock
from notmyfault.core.workflow import ActionCancellation, ActionCancelled
from notmyfault.security.plugin_schema import validate_plugin_meta


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "archive"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("archive_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_is_filesystem_zip_action():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    valid, errors = validate_plugin_meta(meta, "action")
    assert valid, errors
    assert meta["id"] == "archive"
    assert meta["name"] == "压缩与解压"
    assert meta["permissions"] == ["filesystem"]
    assert meta["security"]["rule_approval"] == "admin_key"


@pytest.mark.parametrize("overwrite", [False, True, "false", "true", 0, 1, None, [], {}])
def test_unzip_requires_boolean_overwrite_and_preserves_skipped_files(tmp_path, overwrite):
    module = load_module()
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("existing.txt", b"replacement")
        output.writestr("new.txt", b"new")
    target = tmp_path / "out"
    target.mkdir()
    existing = target / "existing.txt"
    existing.write_bytes(b"original")
    params = {"mode": "unzip", "source": str(archive), "destination": str(target), "overwrite": overwrite}
    if not isinstance(overwrite, bool):
        with pytest.raises(ValueError, match="布尔"):
            workflow.invoke_action(module.run, module, meta, params, {})
        assert existing.read_bytes() == b"original"
        assert list(target.iterdir()) == [existing]
        return

    result = workflow.invoke_action(module.run, module, meta, params, {})
    assert result["ok"] is True
    assert result["file_count"] == (2 if overwrite else 1)
    assert result["skipped"] == (0 if overwrite else 1)
    assert existing.read_bytes() == (b"replacement" if overwrite else b"original")
    assert (target / "new.txt").read_bytes() == b"new"


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("source_name,target_name", [("资料", "解压结果"), (" 资料", " 解压结果"), ("资料\u3000", "解压结果\u3000")])
def test_zip_and_unzip_round_trip(tmp_path, monkeypatch, empty, source_name, target_name):
    monkeypatch.chdir(tmp_path)
    module = load_module()
    source = tmp_path / source_name
    source.mkdir()
    if not empty:
        (source / "说明.txt").write_text("你好", encoding="utf-8")
        nested = source / "子目录"
        nested.mkdir()
        (nested / "数据.txt").write_text("内容", encoding="utf-8")

    archive = tmp_path / (target_name + ".zip")
    zipped = module.run({}, {"mode": "zip", "source": source_name, "destination": archive.name})
    assert zipped["ok"] is True
    assert zipped["file_count"] == (0 if empty else 2)
    assert zipped["archive"] == str(archive)
    assert archive.is_file()

    target = tmp_path / target_name
    unzipped = module.run(
        {},
        {"mode": "unzip", "source": archive.name, "destination": target_name},
    )
    assert unzipped["ok"] is True
    assert unzipped["file_count"] == (0 if empty else 2)
    assert (target / source_name).is_dir()
    if not empty:
        assert (target / source_name / "说明.txt").read_text(encoding="utf-8") == "你好"
        assert (target / source_name / "子目录" / "数据.txt").read_text(encoding="utf-8") == "内容"
        alias = tmp_path / "nested-source.zip"
        os.link(source / "子目录" / "数据.txt", alias)
        rejected = module.run({}, {"mode": "zip", "source": str(source), "destination": str(alias)})
        assert rejected["ok"] is False
        assert alias.read_text(encoding="utf-8") == "内容"
    else:
        assert list((target / source_name).iterdir()) == []
    monkeypatch.setattr(module, "_zip_sources", lambda *args: pytest.fail("压缩包位于源目录内时不应开始写入"))
    for root in (source, Path(tmp_path.anchor)):
        blocked = module.run({}, {"mode": "zip", "source": str(root), "destination": str(source / "blocked.zip")})
        assert blocked["ok"] is False
        assert not (source / "blocked.zip").exists()


def test_zip_single_file_and_default_destination(tmp_path):
    module = load_module()
    source = tmp_path / "单独.txt"
    source.write_text("单独", encoding="utf-8")
    result = module.run({}, {"mode": "zip", "source": str(source)})
    assert result["ok"] is True
    assert result["file_count"] == 1
    assert Path(result["archive"]).name == "单独.txt.zip"
    with zipfile.ZipFile(result["archive"]) as zf:
        assert zf.read("单独.txt").decode("utf-8") == "单独"
        assert zf.getinfo("单独.txt").compress_type == zipfile.ZIP_DEFLATED
    alias = tmp_path / "同一文件.zip"
    os.link(source, alias)
    rejected = module.run({}, {"mode": "zip", "source": str(source), "destination": str(alias)})
    assert rejected["ok"] is False
    assert source.read_text(encoding="utf-8") == alias.read_text(encoding="utf-8") == "单独"


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("existing_zip", [False, True])
def test_zip_preserves_existing_destination_unless_overwrite_is_enabled(tmp_path, overwrite, existing_zip):
    module = load_module()
    source = tmp_path / "source.txt"
    source.write_bytes(b"replacement")
    archive = tmp_path / "output.zip"
    if existing_zip:
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("old.txt", b"original")
    else:
        archive.write_bytes(b"original destination")
    original = archive.read_bytes()
    result = module.run({}, {
        "mode": "zip", "source": str(source),
        "destination": str(archive), "overwrite": overwrite,
    })
    assert result["ok"] is overwrite
    if overwrite:
        with zipfile.ZipFile(archive) as output:
            assert output.namelist() == ["source.txt"]
            assert output.read("source.txt") == b"replacement"
    else:
        assert result["error"]
        assert archive.read_bytes() == original


@pytest.mark.parametrize("mode", ["zip", "unzip"])
@pytest.mark.parametrize("reason", ["cancelled", "timeout"])
def test_archive_stops_during_single_file(tmp_path, monkeypatch, mode, reason):
    module = load_module()
    payload = bytes(range(256)) * 1024
    source = tmp_path / "source.bin"
    archive = tmp_path / "source.zip"
    target = tmp_path / "extracted"
    source.write_bytes(payload)
    if mode == "unzip":
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(source.name, payload)
    run_event = threading.Event()
    clock = FakeClock()
    cancellation = ActionCancellation(
        run_event, timeout_seconds=1 if reason == "timeout" else None, clock=clock,
    )
    bytes_read = 0

    def after_read(data):
        nonlocal bytes_read
        bytes_read += len(data)
        if data:
            if reason == "timeout":
                clock.advance(2)
            else:
                run_event.set()
        return data

    if mode == "zip":
        original_open = builtins.open

        class CancelOnRead:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def read(self, *args, **kwargs):
                return after_read(self.stream.read(*args, **kwargs))

            def __getattr__(self, name):
                return getattr(self.stream, name)

        def cancel_open(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return CancelOnRead(stream) if path == source and mode == "rb" else stream

        monkeypatch.setattr(builtins, "open", cancel_open)
    else:
        original_read = zipfile.ZipExtFile.read

        def cancel_read(stream, *args, **kwargs):
            return after_read(original_read(stream, *args, **kwargs))

        monkeypatch.setattr(zipfile.ZipExtFile, "read", cancel_read)
    params = {
        "mode": mode,
        "source": str(source if mode == "zip" else archive),
        "destination": str(archive if mode == "zip" else target),
    }
    with pytest.raises(ActionCancelled) as error:
        module.run_with_context({}, params, {"runtime": {"cancellation": cancellation}})
    assert error.value.reason == reason
    assert 0 < bytes_read < len(payload)
    if mode == "zip":
        with zipfile.ZipFile(archive) as zf:
            assert zf.getinfo(source.name).file_size < len(payload)
    else:
        assert (target / source.name).stat().st_size < len(payload)
    assert source.read_bytes() == payload


@pytest.mark.parametrize("failure", ["missing", "root_read", "nested_read", "file_stat"])
def test_zip_source_errors_keep_existing_archive(tmp_path, monkeypatch, failure):
    module = load_module()
    source = tmp_path / "源目录"
    archive = tmp_path / "已有.zip"
    archive.write_bytes(b"existing archive")
    if failure != "missing":
        nested = source / "子目录"
        nested.mkdir(parents=True)
        (source / "可读.txt").write_text("保留", encoding="utf-8")
        hidden = nested / "文件.txt"
        hidden.write_text("内容", encoding="utf-8")
        if failure in ("root_read", "nested_read"):
            denied = source if failure == "root_read" else nested
            scandir = os.scandir

            def fail_scan(path):
                if Path(path) == denied:
                    raise PermissionError("目录无法读取")
                return scandir(path)

            monkeypatch.setattr(os, "scandir", fail_scan)
        else:
            original_stat = Path.stat

            def fail_stat(path, *args, **kwargs):
                if path == hidden:
                    raise PermissionError("文件信息无法读取")
                return original_stat(path, *args, **kwargs)

            monkeypatch.setattr(Path, "stat", fail_stat)
    result = module.run({}, {"mode": "zip", "source": str(source), "destination": str(archive)})
    assert result["ok"] is False
    assert result["error"]
    assert archive.read_bytes() == b"existing archive"


def test_soft_error_on_bad_zip(tmp_path):
    module = load_module()
    fake = tmp_path / "假的.zip"
    fake.write_text("not-a-zip", encoding="utf-8")
    result = module.run({}, {"mode": "unzip", "source": str(fake), "destination": str(tmp_path / "out")})
    assert result["ok"] is False
    assert "ZIP" in result["error"] or "zip" in result["error"].lower()


@pytest.mark.parametrize("aliased_directory", [False, True])
def test_soft_error_rejects_path_traversal(tmp_path, aliased_directory):
    module = load_module()
    archive = tmp_path / "evil.zip"
    target = tmp_path / "safe"
    target.mkdir()
    link = None
    if aliased_directory:
        outside = tmp_path / "outside"
        outside.mkdir()
        link = target / ("linked_" if os.name == "nt" else "linked?")
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        else:
            link.symlink_to(outside, target_is_directory=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("linked?/escape.txt" if aliased_directory else "../escape.txt", "x")
    try:
        result = module.run({}, {"mode": "unzip", "source": str(archive), "destination": str(target)})
        assert result["ok"] is False
        assert "非法" in result["error"] or "越界" in result["error"]
        assert not (tmp_path / "escape.txt").exists()
        if aliased_directory:
            assert not (outside / "escape.txt").exists()
    finally:
        if link is not None:
            link.rmdir() if os.name == "nt" else link.unlink()


def test_unknown_mode_raises():
    module = load_module()
    with pytest.raises(ValueError, match="未知模式"):
        module.run({}, {"mode": "rar"})
