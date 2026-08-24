"""发现/物化阶段单次读文件的结构性约束。"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry, inspect_plugin_tree
from notmyfault.security.security import SecurityMode
from notmyfault.security.signing import plugin_files


def make_meta(plugin_id="plug_a", **overrides):
    meta = {
        "id": plugin_id,
        "name": "测试插件",
        "description": "测试用插件",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": "com.test.plug",
    }
    meta.update(overrides)
    return meta


def write_plugin(root, folder, meta, py_code, json_name="action.json", py_name="action.py"):
    folder_path = Path(root) / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    (folder_path / json_name).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (folder_path / py_name).write_text(py_code, encoding="utf-8")
    return folder_path


class SudoStub:
    def authorize_plugin(self, plugin_id, token, module=None):
        pass

    def deauthorize_plugin(self, plugin_id, token):
        pass


def make_loader(tmp_path, mode=SecurityMode.PERMISSIVE):
    registry = PluginRegistry()
    plugin_errors = []
    diagnostics = SimpleNamespace(
        record_plugin_error=lambda store, pid, msg: plugin_errors.append((store, pid, msg))
    )
    loader = PluginLoader(
        registry=registry,
        config={},
        diagnostics=diagnostics,
        security_mode=mode,
        sudo=SudoStub(),
        engine_token="token",
        integrity_errors=[],
        plugin_manifest_path=str(tmp_path / "manifest.json"),
    )
    return loader, registry, plugin_errors


def load_actions(loader, tmp_path, origin="builtin", meta_store=None, func_store=None):
    if meta_store is None:
        meta_store = {}
    if func_store is None:
        func_store = {}
    loaded, failed = loader.load(
        base_dir=str(tmp_path),
        plugins_dir="actions",
        json_filename="action.json",
        py_filename="action.py",
        module_prefix="notmyfault.action_",
        meta_store=meta_store,
        func_store=func_store,
        store_name="Actioner",
        origin=origin,
    )
    return loaded, failed, meta_store, func_store


def load_triggers(loader, tmp_path, origin="builtin", meta_store=None, func_store=None):
    if meta_store is None:
        meta_store = {}
    if func_store is None:
        func_store = {}
    loaded, failed = loader.load(
        base_dir=str(tmp_path),
        plugins_dir="triggers",
        json_filename="trigger.json",
        py_filename="trigger.py",
        module_prefix="notmyfault.trigger_",
        meta_store=meta_store,
        func_store=func_store,
        store_name="Trigger",
        origin=origin,
    )
    return loaded, failed, meta_store, func_store


def test_discovery_reads_each_plugin_file_once(tmp_path, monkeypatch):
    loader, registry, _errors = make_loader(tmp_path, mode=SecurityMode.PERMISSIVE)
    write_plugin(
        tmp_path / "actions",
        "once",
        make_meta("once_a"),
        "import os\n\ndef run(meta, params):\n    return None\n",
    )
    folder = tmp_path / "actions" / "once"
    expected = {str(p.resolve()) for p in plugin_files(folder)}

    real_read = Path.read_bytes
    counts = {}

    def counting(self):
        key = str(self.resolve())
        if key in expected:
            counts[key] = counts.get(key, 0) + 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    loaded, failed, meta_store, func_store = load_actions(loader, tmp_path)
    assert (loaded, failed) == (1, 0)
    assert "once_a" in meta_store
    assert func_store == {}
    assert set(counts) == expected
    assert all(n == 1 for n in counts.values()), counts


def test_materialize_toctou_rereads_once_and_rejects_change(tmp_path, monkeypatch):
    loader, registry, errors = make_loader(tmp_path, mode=SecurityMode.PERMISSIVE)
    folder = write_plugin(
        tmp_path / "actions",
        "toctou",
        make_meta("toctou_a"),
        "def run(meta, params):\n    return 'ok'\n",
    )
    load_actions(loader, tmp_path)

    expected = {str(p.resolve()) for p in plugin_files(folder)}
    real_read = Path.read_bytes
    counts = {}

    def counting(self):
        key = str(self.resolve())
        if key in expected:
            counts[key] = counts.get(key, 0) + 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    (folder / "action.py").write_text(
        "def run(meta, params):\n    return 'changed'\n",
        encoding="utf-8",
    )
    counts.clear()
    assert registry.resolve_action("toctou_a") is None
    assert any("校验后发生变化" in message for _, _, message in errors)
    assert set(counts) == expected
    assert all(n == 1 for n in counts.values()), counts


def test_trigger_discovery_stores_pending_without_import(tmp_path, monkeypatch):
    loader, registry, _errors = make_loader(tmp_path, mode=SecurityMode.PERMISSIVE)
    folder = write_plugin(
        tmp_path / "triggers",
        "lazy_trig",
        make_meta("lazy_trig"),
        "def run(meta, params):\n    return 'ok'\n",
        json_name="trigger.json",
        py_name="trigger.py",
    )
    expected = {str(p.resolve()) for p in plugin_files(folder)}

    real_read = Path.read_bytes
    counts = {}

    def counting(self):
        key = str(self.resolve())
        if key in expected:
            counts[key] = counts.get(key, 0) + 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    loaded, failed, meta_store, func_store = load_triggers(loader, tmp_path)
    assert (loaded, failed) == (1, 0)
    # 发现阶段只入账元数据和待物化条目，不导入模块、不注册入口函数。
    assert "lazy_trig" in meta_store
    assert func_store == {}
    assert registry.triggers_funcs == {}
    assert registry.get_module("lazy_trig") is None
    assert registry.pending.get("lazy_trig") is not None
    assert registry.pending["lazy_trig"]["kind"] == "trigger"
    assert "notmyfault.trigger_lazy_trig" not in sys.modules
    assert set(counts) == expected
    assert all(n == 1 for n in counts.values()), counts


def test_trigger_materialize_toctou_rereads_once_and_rejects_change(tmp_path, monkeypatch):
    loader, registry, errors = make_loader(tmp_path, mode=SecurityMode.PERMISSIVE)
    folder = write_plugin(
        tmp_path / "triggers",
        "toctou_trig",
        make_meta("toctou_trig"),
        "def run(meta, params):\n    return 'ok'\n",
        json_name="trigger.json",
        py_name="trigger.py",
    )
    load_triggers(loader, tmp_path)

    expected = {str(p.resolve()) for p in plugin_files(folder)}
    real_read = Path.read_bytes
    counts = {}

    def counting(self):
        key = str(self.resolve())
        if key in expected:
            counts[key] = counts.get(key, 0) + 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    (folder / "trigger.py").write_text(
        "def run(meta, params):\n    return 'changed'\n",
        encoding="utf-8",
    )
    counts.clear()
    assert registry.resolve_trigger("toctou_trig") is None
    assert any("校验后发生变化" in message for _, _, message in errors)
    assert "toctou_trig" not in registry.triggers_funcs
    assert registry.get_module("toctou_trig") is None
    assert "toctou_trig" not in registry.pending
    assert set(counts) == expected
    assert all(n == 1 for n in counts.values()), counts

    counts.clear()
    assert registry.resolve_trigger("toctou_trig") is None
    assert counts == {}


def test_inspect_plugin_tree_still_available():
    assert callable(inspect_plugin_tree)
