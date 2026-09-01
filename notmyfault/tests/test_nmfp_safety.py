from types import SimpleNamespace

import py7zr
import pytest

from notmyfault.security.plugin_package import PluginPackageLimits, extract_nmfp
import pack_plugin


def make_archive(tmp_path, entries):
    archive_path = str(tmp_path / "plugin.nmfp")
    src = tmp_path / "src.txt"
    with py7zr.SevenZipFile(archive_path, "w") as zf:
        for arcname, content in entries:
            src.write_text(content, encoding="utf-8")
            zf.write(src, arcname)
    return archive_path


class FakeArchive:
    def __init__(self, infos, raw_infos=None):
        self.infos = infos
        self.files = raw_infos or infos
        self.extract_called = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list(self):
        return self.infos

    def extractall(self, dest):
        self.extract_called = True


def install_fake_archive(monkeypatch, infos, raw_infos=None):
    archive = FakeArchive(infos, raw_infos)

    def factory(path, mode="r", password=None):
        return archive

    monkeypatch.setattr(py7zr, "SevenZipFile", factory)
    return archive


def test_extract_accepts_normal_plugin(tmp_path):
    archive_path = make_archive(
        tmp_path,
        [
            ("demo_plugin/action.json", '{"id": "demo_plugin"}'),
            ("demo_plugin/action.py", "def run(params):\n    return True\n"),
        ],
    )
    extract_dir = tmp_path / "out"
    extract_nmfp(archive_path, str(extract_dir), None, PluginPackageLimits())
    assert (extract_dir / "demo_plugin" / "action.json").exists()
    assert (extract_dir / "demo_plugin" / "action.py").exists()


def test_extract_rejects_absolute_path(tmp_path, monkeypatch):
    install_fake_archive(
        monkeypatch,
        [SimpleNamespace(filename="C:\\Windows\\evil.dll", uncompressed=10)],
    )
    with pytest.raises(ValueError) as excinfo:
        extract_nmfp("x.nmfp", str(tmp_path), None, PluginPackageLimits())
    assert "非法路径" in str(excinfo.value)


def test_extract_rejects_parent_traversal(tmp_path, monkeypatch):
    install_fake_archive(
        monkeypatch,
        [SimpleNamespace(filename="../evil.py", uncompressed=10)],
    )
    with pytest.raises(ValueError) as excinfo:
        extract_nmfp("x.nmfp", str(tmp_path), None, PluginPackageLimits())
    assert "非法路径" in str(excinfo.value)


def test_extract_rejects_too_many_entries(tmp_path, monkeypatch):
    infos = [
        SimpleNamespace(filename=f"plugin/file{i}.txt", uncompressed=1)
        for i in range(3)
    ]
    install_fake_archive(monkeypatch, infos)
    with pytest.raises(ValueError) as excinfo:
        extract_nmfp(
            "x.nmfp",
            str(tmp_path),
            None,
            PluginPackageLimits(max_entries=2),
        )
    assert "条目过多" in str(excinfo.value)


def test_extract_rejects_uncompressed_size(tmp_path, monkeypatch):
    infos = [
        SimpleNamespace(filename="plugin/big.bin", uncompressed=1024 * 1024)
    ]
    install_fake_archive(monkeypatch, infos)
    with pytest.raises(ValueError) as excinfo:
        extract_nmfp(
            "x.nmfp",
            str(tmp_path),
            None,
            PluginPackageLimits(max_uncompressed_bytes=100),
        )
    assert "体积过大" in str(excinfo.value)


@pytest.mark.parametrize("attribute", ["is_symlink", "is_junction", "is_socket"])
def test_extract_rejects_archive_links_and_special_files(
    tmp_path, monkeypatch, attribute
):
    listed = [SimpleNamespace(filename="plugin/link", uncompressed=1)]
    raw = SimpleNamespace(filename="plugin/link", **{attribute: True})
    archive = install_fake_archive(monkeypatch, listed, [raw])

    with pytest.raises(ValueError) as excinfo:
        extract_nmfp("x.nmfp", str(tmp_path), None, PluginPackageLimits())

    assert "链接或特殊文件" in str(excinfo.value)
    assert archive.extract_called is False


def test_absolute_path_is_sanitized_by_py7zr(tmp_path):
    archive_path = make_archive(tmp_path, [(r"C:\evil.txt", "data")])
    with py7zr.SevenZipFile(archive_path) as zf:
        names = [info.filename for info in zf.list()]
    assert len(names) == 1
    assert "C:" not in names[0]
    assert "\\" not in names[0]
    assert names[0].lstrip("/\\") == "evil.txt"


def test_pack_all_prefix_preserves_plugin_subdirectories(tmp_path):
    plugin_dir = tmp_path / "plugins" / "actions" / "demo"
    nested_dir = plugin_dir / "lib"
    nested_dir.mkdir(parents=True)
    (plugin_dir / "action.json").write_text(
        """{
            "id": "demo",
            "name": "测试插件",
            "description": "测试",
            "enabled": true,
            "version_code": 1,
            "version": "1.0",
            "package_name": "com.test.demo"
        }""",
        encoding="utf-8",
    )
    (plugin_dir / "action.py").write_text("def run(meta, params): pass\n", encoding="utf-8")
    (nested_dir / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    archive_path = pack_plugin.pack_plugin(
        plugin_dir,
        output_dir=tmp_path / "dist",
        arc_prefix="demo",
    )

    with py7zr.SevenZipFile(archive_path) as zf:
        names = zf.getnames()
    assert "demo/lib/helper.py" in names
