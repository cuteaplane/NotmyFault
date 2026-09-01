"""插件完整性清单的文件变化检测与落盘测试"""

import json
from pathlib import Path

from notmyfault.security import plugins as security_plugins


def _files(plugin_dir: Path) -> list[tuple[str, str]]:
    return [
        (path.name, str(path))
        for path in sorted(plugin_dir.iterdir())
        if path.is_file()
    ]


def _manifest_path(tmp_path) -> Path:
    return tmp_path / "config" / "plugin_manifest.json"


def test_first_check_records_all_files(tmp_path):
    manifest_path = _manifest_path(tmp_path)
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "action.json").write_text("{}", encoding="utf-8")
    (plugin_dir / "action.py").write_text("def run(): pass\n", encoding="utf-8")

    ok, message = security_plugins.verify_plugin_integrity(
        "demo", _files(plugin_dir), manifest_path
    )

    assert ok is True
    assert message == "完整性校验通过"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["demo"]) == {"action.json", "action.py"}


def test_changed_file_keeps_original_manifest(tmp_path):
    manifest_path = _manifest_path(tmp_path)
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    action_path = plugin_dir / "action.py"
    action_path.write_text("old\n", encoding="utf-8")
    security_plugins.verify_plugin_integrity(
        "demo", _files(plugin_dir), manifest_path
    )
    original = manifest_path.read_bytes()

    action_path.write_text("new\n", encoding="utf-8")
    ok, message = security_plugins.verify_plugin_integrity(
        "demo", _files(plugin_dir), manifest_path
    )

    assert ok is False
    assert "action.py 文件已被修改" in message
    assert manifest_path.read_bytes() == original


def test_added_file_is_reported_and_not_recorded(tmp_path):
    manifest_path = _manifest_path(tmp_path)
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "action.py").write_text("old\n", encoding="utf-8")
    security_plugins.verify_plugin_integrity(
        "demo", _files(plugin_dir), manifest_path
    )
    original = manifest_path.read_bytes()

    (plugin_dir / "helper.py").write_text("value = 1\n", encoding="utf-8")
    ok, message = security_plugins.verify_plugin_integrity(
        "demo", _files(plugin_dir), manifest_path
    )

    assert ok is False
    assert "helper.py 文件为清单外新增" in message
    assert manifest_path.read_bytes() == original


def test_deleted_file_is_reported_and_manifest_is_kept(tmp_path):
    manifest_path = _manifest_path(tmp_path)
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    action_path = plugin_dir / "action.py"
    helper_path = plugin_dir / "helper.py"
    action_path.write_text("old\n", encoding="utf-8")
    helper_path.write_text("value = 1\n", encoding="utf-8")
    security_plugins.verify_plugin_integrity(
        "demo", _files(plugin_dir), manifest_path
    )
    original = manifest_path.read_bytes()

    helper_path.unlink()
    ok, message = security_plugins.verify_plugin_integrity(
        "demo", _files(plugin_dir), manifest_path
    )

    assert ok is False
    assert "helper.py 文件已被删除" in message
    assert manifest_path.read_bytes() == original


def test_replace_failure_keeps_old_manifest(tmp_path, monkeypatch):
    manifest_path = _manifest_path(tmp_path)
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{"old": {}}', encoding="utf-8")
    monkeypatch.setattr(
        security_plugins.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(OSError("busy")),
    )

    saved = security_plugins.save_plugin_manifest({"new": {}}, manifest_path)

    assert saved is False
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {"old": {}}
    assert list(manifest_path.parent.glob("*.tmp")) == []


def test_first_check_reports_manifest_write_failure(tmp_path, monkeypatch):
    manifest_path = _manifest_path(tmp_path)
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "action.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        security_plugins,
        "save_plugin_manifest",
        lambda manifest, path=None: False,
    )

    ok, message = security_plugins.verify_plugin_integrity(
        "demo", _files(plugin_dir), manifest_path
    )

    assert ok is False
    assert message == "无法保存完整性清单"


def test_each_save_uses_a_different_temp_file(tmp_path, monkeypatch):
    manifest_path = _manifest_path(tmp_path)
    paths = []
    real_replace = security_plugins.os.replace

    def record_replace(source, target):
        paths.append(source)
        real_replace(source, target)

    monkeypatch.setattr(security_plugins.os, "replace", record_replace)

    assert security_plugins.save_plugin_manifest({"first": {}}, manifest_path) is True
    assert security_plugins.save_plugin_manifest({"second": {}}, manifest_path) is True
    assert len(paths) == 2
    assert paths[0] != paths[1]
