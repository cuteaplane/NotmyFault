"""插件扩展注册表、私有数据和 HTTP 会话。"""

import json
import threading
from types import SimpleNamespace

import pytest

from notmyfault.extensions.protocol import (
    OwnedValueError,
    make_owned_value,
    unpack_owned_value,
)
from notmyfault.extensions.registry import ExtensionRegistry
from notmyfault.extensions.session import ExtensionSessionManager
from notmyfault.core.run_summary import summarize_fields
from notmyfault.tests.api_support import FakeRunner, make_api_env
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


def native_editor_meta():
    return {
        "id": "native_sample",
        "name": "普通值编辑器",
        "description": "测试普通参数编辑器",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": "com.example.native_sample",
        "params": [{
            "name": "hotkey",
            "label": "快捷键",
            "type": "hotkey",
            "value_type": "string",
        }],
        "contributes": {
            "commands": [{
                "id": "capture",
                "title": "录制",
                "handler": "extension.py:capture",
            }],
            "parameter_editors": [{
                "id": "recorder",
                "parameter": "hotkey",
                "value_type": "string",
                "command": "capture",
                "ui": {"control": "button", "label": "录制"},
            }],
        },
    }


class ExtensionEngine:
    def __init__(self, root):
        self.triggers_meta = {}
        self.actions_meta = {"sample": extension_meta()}
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


class NativeEditorEngine:
    def __init__(self, root):
        self.triggers_meta = {"native_sample": native_editor_meta()}
        self.actions_meta = {}
        self.extensions = ExtensionRegistry()
        self.extensions.register_manifest(
            "native_sample", "trigger", native_editor_meta(), str(root)
        )
        self.extensions.register_command(
            "native_sample", "capture", self.capture, self
        )

    def extension_handler(self, plugin_id, command_id):
        return self.extensions.handler(plugin_id, command_id)

    @staticmethod
    def capture(context, payload):
        return context.commit_value("Ctrl+Shift+K")


def make_client(tmp_path, engine=None):
    (tmp_path / "editor.html").write_text("<h1>插件页面</h1>", encoding="utf-8")
    runner = FakeRunner()
    runner.current_engine = engine or ExtensionEngine(tmp_path)
    runner.engine_running = True
    runner.engine_state = "running"
    env = make_api_env(tmp_path, runner=runner)
    return env.client, env.headers


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


def test_owned_value_rejects_oversized_data():
    with pytest.raises(OwnedValueError) as error:
        make_owned_value(
            "com.example.sample",
            "document",
            2,
            {"text": "x" * (1024 * 1024)},
            "大文档",
        )
    assert "不能超过 1024 KiB" in str(error.value)


def test_sensitive_plugin_data_uses_redacted_run_summary():
    meta = extension_meta()
    meta["params"][0]["sensitive"] = True
    ok, errors = validate_plugin_meta(meta, "action")
    assert ok is True
    assert errors == []
    value = make_owned_value(
        "com.example.sample", "document", 2, {"token": "secret"}, "机密文档"
    )
    summary = summarize_fields({"document": value}, meta["params"])
    assert summary[0]["display"] == "敏感值已隐藏"
    assert summary[0]["redacted"] is True


def test_contribution_schema_checks_references():
    ok, errors = validate_plugin_meta(extension_meta(), "action")
    assert ok is True
    assert errors == []

    broken = json.loads(json.dumps(extension_meta()))
    broken["contributes"]["parameter_editors"][0]["command"] = "missing"
    ok, errors = validate_plugin_meta(broken, "action")
    assert ok is False
    assert any("未声明命令" in error for error in errors)


def test_native_parameter_editor_schema_checks_value_type():
    ok, errors = validate_plugin_meta(native_editor_meta(), "trigger")
    assert ok is True
    assert errors == []

    broken = json.loads(json.dumps(native_editor_meta()))
    broken["contributes"]["parameter_editors"][0]["value_type"] = "number"
    ok, errors = validate_plugin_meta(broken, "trigger")
    assert ok is False
    assert any("必须与参数 'hotkey' 的 value_type 一致" in error for error in errors)


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
        plugin_manifest_path=str(tmp_path / "manifest.json"),
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


def test_extension_command_session_and_private_value(tmp_path):
    client, headers = make_client(tmp_path)
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


def test_extension_command_commits_native_value(tmp_path):
    client, headers = make_client(tmp_path, NativeEditorEngine(tmp_path))
    response = client.post(
        "/api/plugins/native_sample/extensions/commands/capture/invoke",
        headers=headers,
        json={
            "source_kind": "parameter_editors",
            "source_id": "recorder",
            "current_value": "Ctrl+A",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["value"] == "Ctrl+Shift+K"
    assert response.json()["close"] is True


def test_extension_rejects_wrong_native_current_value_type(tmp_path):
    client, headers = make_client(tmp_path, NativeEditorEngine(tmp_path))
    response = client.post(
        "/api/plugins/native_sample/extensions/commands/capture/invoke",
        headers=headers,
        json={
            "source_kind": "parameter_editors",
            "source_id": "recorder",
            "current_value": 3,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "当前值不是 string"


def test_extension_rejects_owned_value_from_other_plugin(tmp_path):
    client, headers = make_client(tmp_path)
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


def test_extension_session_can_be_closed_without_plugin_command(tmp_path):
    client, headers = make_client(tmp_path)
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


def test_extension_view_uses_declared_file(tmp_path):
    client, headers = make_client(tmp_path)
    response = client.get(
        "/api/plugins/sample/extensions/views/editor/page", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["html"] == "<h1>插件页面</h1>"


def test_extension_view_rejects_page_outside_plugin_root(tmp_path):
    outside = tmp_path.parent / "outside-plugin-page.html"
    outside.write_text("<h1>不应读取</h1>", encoding="utf-8")
    engine = ExtensionEngine(tmp_path)
    meta = extension_meta()
    meta["contributes"]["views"][0]["page"] = "../outside-plugin-page.html"
    engine.extensions.register_manifest("sample", "action", meta, str(tmp_path))
    client, headers = make_client(tmp_path, engine)

    response = client.get(
        "/api/plugins/sample/extensions/views/editor/page", headers=headers
    )

    assert response.status_code == 404
    assert "不应读取" not in response.text


@pytest.mark.parametrize("result_kind", ["exception", "json", "size", "error"])
def test_extension_exception_is_logged_but_not_returned(tmp_path, result_kind):
    engine = ExtensionEngine(tmp_path)
    cleaned = []

    def fail(_context, _payload):
        _context.register_cleanup(lambda: cleaned.append(True))
        if result_kind == "exception":
            raise RuntimeError("unit-test-secret-extension-detail")
        if result_kind == "json":
            return {"data": object()}
        if result_kind == "size":
            return {"data": "x" * (1024 * 1024 + 1)}
        return {"ok": False, "error": "操作失败"}

    engine.extensions.register_command("sample", "open", fail, engine)
    client, headers = make_client(tmp_path, engine)
    response = client.post(
        "/api/plugins/sample/extensions/commands/open/invoke",
        headers=headers,
        json={
            "source_kind": "parameter_editors",
            "source_id": "document_editor",
            "payload": {},
        },
    )

    assert response.status_code == (413 if result_kind == "size" else 400)
    assert response.json()["ok"] is False
    assert "unit-test-secret-extension-detail" not in response.text
    assert cleaned == [True]


def test_extension_session_manager_drop_all_runs_cleanup():
    manager = ExtensionSessionManager()
    cleaned = []
    session = manager.create(
        plugin_id="sample",
        command_id="open",
        plugin_meta=extension_meta(),
        source_kind="parameter_editors",
        source_id="document_editor",
        allowed_commands={"open"},
        data_type={"id": "document", "version": 2},
        current_value=None,
    )
    session.add_cleanup(lambda: cleaned.append(True))

    manager.drop_all()

    assert manager.get(session.session_id) is None
    assert cleaned == [True]


def test_extension_session_manager_expires_without_new_lookup():
    manager = ExtensionSessionManager(ttl_seconds=10)
    session = manager.create(
        plugin_id="sample",
        command_id="open",
        plugin_meta=extension_meta(),
        source_kind="parameter_editors",
        source_id="document_editor",
        allowed_commands={"open"},
        data_type={"id": "document", "version": 2},
        current_value=None,
    )
    cleaned = []
    session.add_cleanup(lambda: cleaned.append(True))
    session.updated_at -= 20

    assert manager.cleanup_expired() == 1
    assert cleaned == [True]


def test_extension_session_can_close_while_handler_is_running():
    manager = ExtensionSessionManager()
    session = manager.create(
        plugin_id="sample",
        command_id="open",
        plugin_meta=extension_meta(),
        source_kind="parameter_editors",
        source_id="document_editor",
        allowed_commands={"open"},
        data_type={"id": "document", "version": 2},
        current_value=None,
    )
    started = threading.Event()
    cancelled = threading.Event()
    session.add_cleanup(cancelled.set)

    def handler(context, payload):
        started.set()
        cancelled.wait(timeout=2)

    invoke_thread = threading.Thread(
        target=session.invoke,
        args=(handler, object(), None),
    )
    invoke_thread.start()
    assert started.wait(timeout=1)

    drop_thread = threading.Thread(target=manager.drop, args=(session.session_id,))
    drop_thread.start()
    drop_thread.join(timeout=1)
    invoke_thread.join(timeout=1)

    assert not drop_thread.is_alive()
    assert not invoke_thread.is_alive()
    assert cancelled.is_set()
