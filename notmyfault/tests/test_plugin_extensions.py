"""插件资源路径与安装期 build 清单。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
from notmyfault.security.plugin_resources import plugin_resource
from notmyfault.security.plugin_schema import validate_plugin_meta
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


def write_plugin(root, folder, meta):
    folder_path = Path(root) / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    (folder_path / "action.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (folder_path / "action.py").write_text(
        "def run(meta, params):\n    return None\n", encoding="utf-8"
    )
    return folder_path


class SudoStub:
    def authorize_plugin(self, plugin_id, token, module=None):
        pass

    def deauthorize_plugin(self, plugin_id, token):
        pass


def make_loader(tmp_path):
    errors = []
    loader = PluginLoader(
        registry=PluginRegistry(),
        config={},
        diagnostics=SimpleNamespace(
            record_plugin_error=lambda store, pid, msg: errors.append((store, pid, msg))
        ),
        security_mode=SecurityMode.PERMISSIVE,
        sudo=SudoStub(),
        engine_token="token",
        integrity_errors=[],
        plugin_manifest_path=str(tmp_path / "manifest.json"),
    )
    return loader, errors


def load_actions(loader, tmp_path, origin="builtin"):
    return loader.load(
        base_dir=str(tmp_path),
        plugins_dir="actions",
        json_filename="action.json",
        py_filename="action.py",
        module_prefix="notmyfault.action_extension_",
        meta_store={},
        func_store={},
        store_name="Action",
        origin=origin,
    )


def test_plugin_resource_resolves_inside_registered_plugin(tmp_path):
    loader, _errors = make_loader(tmp_path)
    folder = write_plugin(tmp_path / "actions", "res", make_meta("res_a"))
    (folder / "bin").mkdir()
    (folder / "bin" / "tool.exe").write_bytes(b"\x00")
    assert load_actions(loader, tmp_path) == (1, 0)
    assert plugin_resource("res_a", "bin", "tool.exe") == os.path.realpath(
        str(folder / "bin" / "tool.exe")
    )


@pytest.mark.parametrize(
    "parts", [("..", "secret.txt"), ("sub", "..", "..", "secret.txt")]
)
def test_plugin_resource_rejects_escape(tmp_path, parts):
    loader, _errors = make_loader(tmp_path)
    write_plugin(tmp_path / "actions", "res", make_meta("res_b"))
    assert load_actions(loader, tmp_path) == (1, 0)
    with pytest.raises(ValueError):
        plugin_resource("res_b", *parts)


def test_plugin_resource_rejects_unknown_plugin():
    with pytest.raises(ValueError):
        plugin_resource("missing-resource-plugin", "x.bin")


@pytest.mark.parametrize(
    "build",
    ["gcc", {"command": []}, {"command": [1]}, {"outputs": ["../escape.bin"]}],
)
def test_build_schema_rejects_invalid_shapes(build):
    ok, _errors = validate_plugin_meta(make_meta(build=build), "action")
    assert ok is False


def test_build_schema_accepts_relative_outputs():
    ok, errors = validate_plugin_meta(
        make_meta(
            build={
                "command": ["gcc main.c -o bin/tool.exe"],
                "outputs": ["bin/tool.exe"],
            }
        ),
        "action",
    )
    assert ok, errors


def test_builtin_build_hook_is_rejected_by_loader(tmp_path):
    loader, errors = make_loader(tmp_path)
    write_plugin(
        tmp_path / "actions",
        "build-hook",
        make_meta(build={"command": ["make"]}),
    )
    assert load_actions(loader, tmp_path, origin="builtin") == (0, 1)
    assert any("build 编译钩子" in message for _, _, message in errors)
