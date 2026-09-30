"""archive 动作：ZIP 压缩解压与路径软失败。"""

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

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


def test_zip_and_unzip_round_trip(tmp_path):
    module = load_module()
    source = tmp_path / "资料"
    source.mkdir()
    (source / "说明.txt").write_text("你好", encoding="utf-8")
    nested = source / "子目录"
    nested.mkdir()
    (nested / "数据.txt").write_text("内容", encoding="utf-8")

    archive = tmp_path / "out" / "资料.zip"
    zipped = module.run({}, {"mode": "zip", "source": str(source), "destination": str(archive)})
    assert zipped["ok"] is True
    assert zipped["file_count"] == 2
    assert Path(zipped["archive"]).is_file()

    target = tmp_path / "解压结果"
    unzipped = module.run(
        {},
        {"mode": "unzip", "source": str(archive), "destination": str(target)},
    )
    assert unzipped["ok"] is True
    assert unzipped["file_count"] == 2
    assert (target / "资料" / "说明.txt").read_text(encoding="utf-8") == "你好"
    assert (target / "资料" / "子目录" / "数据.txt").read_text(encoding="utf-8") == "内容"


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


def test_soft_error_on_missing_source(tmp_path):
    module = load_module()
    missing = tmp_path / "没有这个"
    result = module.run({}, {"mode": "zip", "source": str(missing)})
    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_soft_error_on_bad_zip(tmp_path):
    module = load_module()
    fake = tmp_path / "假的.zip"
    fake.write_text("not-a-zip", encoding="utf-8")
    result = module.run({}, {"mode": "unzip", "source": str(fake), "destination": str(tmp_path / "out")})
    assert result["ok"] is False
    assert "ZIP" in result["error"] or "zip" in result["error"].lower()


def test_soft_error_rejects_path_traversal(tmp_path):
    module = load_module()
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", "x")
    result = module.run(
        {},
        {"mode": "unzip", "source": str(archive), "destination": str(tmp_path / "safe")},
    )
    assert result["ok"] is False
    assert "非法" in result["error"] or "越界" in result["error"]
    assert not (tmp_path / "escape.txt").exists()


def test_unknown_mode_raises():
    module = load_module()
    with pytest.raises(ValueError, match="未知模式"):
        module.run({}, {"mode": "rar"})
