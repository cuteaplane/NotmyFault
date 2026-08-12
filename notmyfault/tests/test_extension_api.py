"""插件扩展注册表、私有数据和 HTTP 会话。"""

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from notmyfault.extensions.protocol import (
    OwnedValueError,
    make_owned_value,
    unpack_owned_value,
)
from notmyfault.extensions.registry import ExtensionRegistry
from notmyfault.host import api_server
from notmyfault.host.api_server import EngineAPI
from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
from notmyfault.security.plugin_schema import validate_plugin_meta
from notmyfault.security.security import SecurityMode


def extension_meta():
    return {
        "id": "sample",
        "name": "示例扩展",
        "description": "测试扩展 API",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": "com.example.sample",
        "params": [{
            "name": "document",
            "label": "插件文档",
            "type": "plugin_data",
            "data_type": "document",
            "required": True,
        }],
        "contributes": {
            "commands": [
                {"id": "open", "title": "打开", "handler": "extension.py:open_editor"},
                {"id": "save", "title": "保存", "handler": "extension.py:save"},
                {"id": "hidden", "title": "隐藏命令", "handler": "extension.py:hidden"},
            ],
            "views": [{
                "id": "editor",
                "title": "文档编辑器",
                "page": "editor.html",
                "commands": ["save"],
            }],
            "data_types": [{
                "id": "document",
                "version": 2,
                "binding": "private",
            }],
            "parameter_editors": [{
                "id": "document_editor",
                "parameter": "document",
                "data_type": "document",
                "command": "open",
                "view": "editor",
                "ui": {"control": "button", "empty_label": "创建文档"},
            }],
        },
    }


class ExtensionEngine:
    def __init__(self, root):
        self.extensions = ExtensionRegistry()
        self.extensions.register_manifest("sample", "action", extension_meta(), str(root))
        self.extensions.register_command("sample", "open", self.open_editor, self)
        self.extensions.register_command("sample", "save", self.save, self)
        self.extensions.register_command("sample", "hidden", self.hidden, self)

    def extension_handler(self, plugin_id, command_id):
        return self.extensions.handler(plugin_id, command_id)

    @staticmethod
    def open_editor(context, payload):
        return context.open_view("editor", {"text": "old"})

    @staticmethod
    def save(context, payload):
        text = payload.get("text", "") if isinstance(payload, dict) else ""
        return context.commit({"text": text}, f"文档 · {len(text)} 字")

    @staticmethod
    def hidden(context, payload):
        return context.result({"secret": True})


def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(api_server, "CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setattr(api_server, "API_TOKEN_FILE", str(tmp_path / ".api_token"))
    monkeypatch.setattr(api_server, "_PRIVATE_DIR", tmp_path / "private")
    (tmp_path / "editor.html").write_text("<h1>插件页面</h1>", encoding="utf-8")
    runner = SimpleNamespace(
        current_engine=ExtensionEngine(tmp_path),
        engine_running=True,
        engine_state="running",
    )
    api = EngineAPI(runner)
    return (
        TestClient(api.app),
        {"Authorization": f"Bearer {api_server.API_TOKEN}"},
    )


def test_owned_value_round_trip_and_owner_check():
    value = make_owned_value(
        "com.example.sample", "document", 2, {"text": "你好"}, "文档 · 2 字"
    )
    assert unpack_owned_value(value, "com.example.sample", "document", 2) == {
        "text": "你好"
    }
    try:
        unpack_owned_value(value, "com.example.other", "document", 2)
    except OwnedValueError as error:
        assert "归属不匹配" in str(error)
    else:
        raise AssertionError("其他插件不应读取这项数据")


def test_contribution_schema_checks_references():
    ok, errors = validate_plugin_meta(extension_meta(), "action")
    assert ok is True
    assert errors == []

    broken = json.loads(json.dumps(extension_meta()))
    broken["contributes"]["parameter_editors"][0]["command"] = "missing"
    ok, errors = validate_plugin_meta(broken, "action")
    assert ok is False
    assert any("未声明命令" in error for error in errors)


def test_public_registry_does_not_expose_handler_path(tmp_path):
    registry = ExtensionRegistry()
    registry.register_manifest("sample", "action", extension_meta(), str(tmp_path))
    command = registry.public_contributions()["commands"][0]
    assert "handler" not in command
    assert command["loaded"] is False


def test_action_extension_commands_follow_lazy_loading(tmp_path):
    plugin_root = tmp_path / "actions" / "sample"
    plugin_root.mkdir(parents=True)
    (plugin_root / "action.json").write_text(
        json.dumps(extension_meta(), ensure_ascii=False), encoding="utf-8"
    )
    (plugin_root / "action.py").write_text(
        "def run(meta, params):\n    return {'ok': True}\n", encoding="utf-8"
    )
    (plugin_root / "extension.py").write_text(
        "def open_editor(context, payload):\n"
        "    return context.open_view('editor', {})\n"
        "def save(context, payload):\n"
        "    return context.result(payload)\n"
        "def hidden(context, payload):\n"
        "    return context.result(payload)\n",
        encoding="utf-8",
    )
    (plugin_root / "editor.html").write_text("<h1>编辑器</h1>", encoding="utf-8")
    registry = PluginRegistry()
    diagnostics = SimpleNamespace(record_plugin_error=lambda *args: None)
    sudo = SimpleNamespace(
        authorize_plugin=lambda *args, **kwargs: None,
        deauthorize_plugin=lambda *args, **kwargs: None,
    )
    loader = PluginLoader(
        registry=registry,
        config={},
        diagnostics=diagnostics,
        security_mode=SecurityMode.PERMISSIVE,
        sudo=sudo,
        engine_token="token",
        integrity_errors=[],
    )
    loaded, failed = loader.load(
        base_dir=str(tmp_path),
        plugins_dir="actions",
        json_filename="action.json",
        py_filename="action.py",
        module_prefix="notmyfault.test_extension_",
        meta_store=registry.actions_meta,
        func_store=registry.actions_funcs,
        store_name="Action",
        origin="builtin",
    )
    assert (loaded, failed) == (1, 0)
    assert registry.extensions.handler("sample", "open") is None
    assert registry.resolve_action("sample") is not None
    assert callable(registry.extensions.handler("sample", "open"))


def test_extension_command_session_and_private_value(tmp_path, monkeypatch):
    client, headers = make_client(tmp_path, monkeypatch)
    opened = client.post(
        "/api/plugins/sample/extensions/commands/open/invoke",
        headers=headers,
        json={
            "source_kind": "parameter_editors",
            "source_id": "document_editor",
            "current_value": None,
            "payload": {},
        },
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["view"] == "editor"

    session_id = opened.json()["session_id"]
    hidden = client.post(
        "/api/plugins/sample/extensions/commands/hidden/invoke",
        headers=headers,
        json={"session_id": session_id, "payload": {}},
    )
    assert hidden.status_code == 403

    saved = client.post(
        "/api/plugins/sample/extensions/commands/save/invoke",
        headers=headers,
        json={"session_id": session_id, "payload": {"text": "新的内容"}},
    )
    assert saved.status_code == 200, saved.text
    value = saved.json()["value"]
    assert value["$type"] == "com.example.sample/document@2"
    assert value["data"] == {"text": "新的内容"}
    assert saved.json()["close"] is True

    expired = client.post(
        "/api/plugins/sample/extensions/commands/save/invoke",
        headers=headers,
        json={"session_id": session_id, "payload": {"text": "again"}},
    )
    assert expired.status_code == 404


def test_extension_rejects_owned_value_from_other_plugin(tmp_path, monkeypatch):
    client, headers = make_client(tmp_path, monkeypatch)
    other_value = make_owned_value(
        "com.example.other", "document", 2, {"text": "x"}, "其他文档"
    )
    response = client.post(
        "/api/plugins/sample/extensions/commands/open/invoke",
        headers=headers,
        json={
            "source_kind": "parameter_editors",
            "source_id": "document_editor",
            "current_value": other_value,
            "payload": {},
        },
    )
    assert response.status_code == 400
    assert "归属不匹配" in response.json()["error"]


def test_extension_session_can_be_closed_without_plugin_command(tmp_path, monkeypatch):
    client, headers = make_client(tmp_path, monkeypatch)
    opened = client.post(
        "/api/plugins/sample/extensions/commands/open/invoke",
        headers=headers,
        json={
            "source_kind": "parameter_editors",
            "source_id": "document_editor",
            "payload": {},
        },
    )
    session_id = opened.json()["session_id"]
    closed = client.delete(
        f"/api/plugins/sample/extensions/sessions/{session_id}", headers=headers
    )
    assert closed.status_code == 200
    assert closed.json() == {"ok": True}
    resumed = client.post(
        "/api/plugins/sample/extensions/commands/save/invoke",
        headers=headers,
        json={"session_id": session_id, "payload": {"text": "x"}},
    )
    assert resumed.status_code == 404


def test_extension_view_uses_declared_file(tmp_path, monkeypatch):
    client, headers = make_client(tmp_path, monkeypatch)
    response = client.get(
        "/api/plugins/sample/extensions/views/editor/page", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["html"] == "<h1>插件页面</h1>"
