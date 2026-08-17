"""inspect_plugin_tree 的单遍读取与快照一致性测试。"""

from pathlib import Path

import pytest

from notmyfault.security import plugin_loader
from notmyfault.security.plugin_loader import inspect_plugin_tree
from notmyfault.security.signing import plugin_files


@pytest.fixture
def plugin_folder(tmp_path):
    root = tmp_path / "demo_plugin"
    root.mkdir()
    (root / "action.json").write_text('{"id": "demo"}', encoding="utf-8")
    (root / "action.py").write_text("def run():\n    pass\n", encoding="utf-8")
    lib = root / "lib"
    lib.mkdir()
    (lib / "helper.txt").write_text("你好", encoding="utf-8")
    # 签名产物和生成目录都不进清单
    (root / "signature.sig").write_bytes(b"deadbeef")
    pycache = root / "__pycache__"
    pycache.mkdir()
    (pycache / "gen.py").write_text("x = 1", encoding="utf-8")
    return root


def test_snapshot_matches_legacy_snapshot(plugin_folder):
    tree = inspect_plugin_tree(str(plugin_folder))
    assert tree is not None
    assert tree.file_snapshot == plugin_loader._snapshot_plugin_files(str(plugin_folder))


def test_payload_is_concatenated_files_in_manifest_order(plugin_folder):
    tree = inspect_plugin_tree(str(plugin_folder))
    assert tree is not None
    expected = b"".join(f.read_bytes() for f in plugin_files(str(plugin_folder)))
    assert tree.payload == expected
    assert tree.files == plugin_files(str(plugin_folder))


def test_each_file_read_exactly_once(monkeypatch, plugin_folder):
    real_read_bytes = Path.read_bytes
    read_counts = {}

    def counting_read_bytes(self):
        read_counts[str(self)] = read_counts.get(str(self), 0) + 1
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    tree = inspect_plugin_tree(str(plugin_folder))
    assert tree is not None
    expected = [str(f) for f in plugin_files(str(plugin_folder))]
    assert sorted(read_counts) == sorted(expected)
    assert all(count == 1 for count in read_counts.values())


def test_py_sources_contains_decoded_python_files(plugin_folder):
    tree = inspect_plugin_tree(str(plugin_folder))
    assert tree is not None
    action_path = plugin_folder / "action.py"
    expected = action_path.read_bytes().decode("utf-8")
    assert tree.py_sources == {str(action_path): expected}


def test_read_error_returns_none(monkeypatch, plugin_folder):
    def boom(self):
        raise OSError("denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert inspect_plugin_tree(str(plugin_folder)) is None
