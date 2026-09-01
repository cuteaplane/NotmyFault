"""插件发现与物化之间的 TOCTOU 防护。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
from notmyfault.security.security import SecurityMode


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


@pytest.mark.parametrize(
    ("kind", "plugins_dir", "json_name", "py_name", "resolver_name"),
    [
        ("action", "actions", "action.json", "action.py", "resolve_action"),
        ("trigger", "triggers", "trigger.json", "trigger.py", "resolve_trigger"),
    ],
)
def test_materialize_rejects_plugin_changed_after_discovery(
    tmp_path,
    kind,
    plugins_dir,
    json_name,
    py_name,
    resolver_name,
):
    loader, registry, errors = make_loader(tmp_path)
    plugin_id = f"toctou_{kind}"
    folder = write_plugin(
        tmp_path / plugins_dir,
        plugin_id,
        make_meta(plugin_id),
        "def run(meta, params):\n    return 'ok'\n",
        json_name=json_name,
        py_name=py_name,
    )
    loaded, failed = loader.load(
        base_dir=str(tmp_path),
        plugins_dir=plugins_dir,
        json_filename=json_name,
        py_filename=py_name,
        module_prefix=f"notmyfault.{kind}_",
        meta_store={},
        func_store={},
        store_name=kind.title(),
        origin="builtin",
    )
    assert (loaded, failed) == (1, 0)

    (folder / py_name).write_text(
        "def run(meta, params):\n    return 'changed'\n",
        encoding="utf-8",
    )
    assert getattr(registry, resolver_name)(plugin_id) is None
    assert any("校验后发生变化" in message for _, _, message in errors)
    assert registry.get_module(plugin_id) is None
    assert plugin_id not in registry.pending
