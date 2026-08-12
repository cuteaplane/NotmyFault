"""多文件插件、懒加载、作者自签、资源定位、build 钩子等扩展行为"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import py7zr
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import notmyfault.config as config_mod
from notmyfault.core.engine import AutomationEngine
from notmyfault.host import api_server
from notmyfault.host.api_server import EngineAPI
from notmyfault.security import plugins as security_plugins
from notmyfault.security import signing
from notmyfault.security import sudo
from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
from notmyfault.security.plugin_resources import plugin_resource
from notmyfault.security.plugin_schema import validate_plugin_meta
from notmyfault.security.plugins import plugin_signature_kind, verify_plugin_sig
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
        store_name="Action",
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


class TestComponentLoading:
    def test_plugin_without_components_untouched(self, tmp_path):
        loader, registry, plugin_errors = make_loader(tmp_path)
        write_plugin(
            tmp_path / "actions",
            "plain",
            make_meta("plain"),
            "def run(action_info, params):\n    return {'ok': True}\n",
        )
        loaded, failed, meta_store, func_store = load_actions(
            loader, tmp_path, meta_store={}, func_store={}
        )
        assert loaded == 1 and failed == 0
        assert registry.resolve_action("plain") is not None
        assert registry.components == {}
        assert "plain" in registry.actions_meta
        registry.unregister("action", "plain")
        assert registry.components == {}
        assert "plain" not in registry.modules
        assert plugin_errors == []

    def test_action_components_loaded_on_materialize(self, tmp_path):
        loader, registry, plugin_errors = make_loader(tmp_path)
        action_py = "def run(action_info, params):\n    return {'ok': True}\n"
        component_py = "\n".join([
            "def invoke(session, method, payload):",
            "    session.data['last'] = method",
            "    return {'ok': True, 'data': {'target': 'x'}}",
        ])
        folder = write_plugin(
            tmp_path / "actions",
            "plug_a",
            make_meta(components=[{
                "id": "record",
                "name": "录制",
                "entrypoint": "component.py",
            }]),
            action_py,
        )
        (folder / "component.py").write_text(component_py, encoding="utf-8")
        loaded, failed, meta_store, func_store = load_actions(
            loader, tmp_path, meta_store={}, func_store={}
        )
        assert loaded == 1 and failed == 0
        # 动作懒加载，组件也随动作一起推迟到首次使用。
        assert registry.components.get("plug_a") is None
        assert registry.resolve_action("plug_a") is not None
        component = registry.components.get("plug_a", {}).get("record")
        assert component is not None
        assert hasattr(component, "invoke")
        assert plugin_errors == []

    def test_action_component_missing_entry_records_diagnostic(self, tmp_path):
        loader, registry, plugin_errors = make_loader(tmp_path)
        write_plugin(
            tmp_path / "actions",
            "plug_a",
            make_meta(components=[{
                "id": "record",
                "name": "录制",
                "entrypoint": "component.py",
            }]),
            "def run(action_info, params):\n    return {'ok': True}\n",
        )
        loaded, failed, meta_store, func_store = load_actions(
            loader, tmp_path, meta_store={}, func_store={}
        )
        assert loaded == 1 and failed == 0
        registry.resolve_action("plug_a")
        assert registry.components.get("plug_a") is None
        assert any("入口缺失" in msg for _, _, msg in plugin_errors)

    def test_action_component_without_invoke_not_registered(self, tmp_path):
        loader, registry, plugin_errors = make_loader(tmp_path)
        folder = write_plugin(
            tmp_path / "actions",
            "plug_a",
            make_meta(components=[{
                "id": "record",
                "name": "录制",
                "entrypoint": "component.py",
            }]),
            "def run(action_info, params):\n    return {'ok': True}\n",
        )
        (folder / "component.py").write_text(
            "def describe():\n    return {}\n", encoding="utf-8"
        )
        loaded, failed, meta_store, func_store = load_actions(
            loader, tmp_path, meta_store={}, func_store={}
        )
        assert loaded == 1 and failed == 0
        registry.resolve_action("plug_a")
        assert registry.components.get("plug_a") is None
        assert any("缺少 invoke 函数" in msg for _, _, msg in plugin_errors)


class TestMultiFilePlugins:
    def test_entry_imports_sibling_module(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        folder = write_plugin(
            tmp_path / "actions", "multi", make_meta("multi_a"),
            "import helper_multi\n"
            "def run(meta, params):\n"
            "    return helper_multi.value()\n",
        )
        (folder / "helper_multi.py").write_text(
            "def value():\n    return 42\n", encoding="utf-8"
        )
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        run = registry.resolve_action("multi_a")
        assert callable(run)
        assert run({}, {}) == 42

    def test_unregister_cleans_sys_path_and_modules(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        folder = write_plugin(
            tmp_path / "actions", "multi", make_meta("multi_b"),
            "import helper_b\n"
            "def run(meta, params):\n"
            "    return helper_b.value()\n",
        )
        (folder / "helper_b.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        load_actions(loader, tmp_path)
        registry.resolve_action("multi_b")
        root = os.path.realpath(str(folder))
        assert root in sys.path
        assert "helper_b" in sys.modules

        registry.unregister("action", "multi_b")
        assert root not in sys.path
        assert "helper_b" not in sys.modules
        assert "multi_b" not in registry.plugin_roots
        with pytest.raises(ValueError):
            plugin_resource("multi_b", "helper_b.py")
        # 卸载后再次解析不到入口函数
        assert registry.resolve_action("multi_b") is None

    def test_entry_module_is_visible_while_executing(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        code = (
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class Result:\n"
            "    value: int = 7\n"
            "def run(meta, params):\n"
            "    return Result().value\n"
        )
        write_plugin(tmp_path / "actions", "dataclass", make_meta("data_a"), code)
        load_actions(loader, tmp_path)

        run = registry.resolve_action("data_a")

        assert callable(run)
        assert run({}, {}) == 7
        assert registry.get_module("data_a") is sys.modules["notmyfault.action_data_a"]

    def test_trigger_and_action_cannot_share_id(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        write_plugin(
            tmp_path / "triggers",
            "shared_trigger",
            make_meta("shared_id"),
            "def run(meta, config, emit, stop_event):\n    pass\n",
            json_name="trigger.json",
            py_name="trigger.py",
        )
        write_plugin(
            tmp_path / "actions",
            "shared_action",
            make_meta("shared_id"),
            "def run(meta, params):\n    pass\n",
        )

        assert load_triggers(loader, tmp_path)[:2] == (1, 0)
        assert load_actions(loader, tmp_path)[:2] == (0, 1)
        assert registry.plugin_kinds["shared_id"] == "trigger"
        assert any("id 已被触发器使用" in message for _, _, message in errors)

    def test_failed_override_restores_builtin_resources(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        builtin = write_plugin(
            tmp_path / "actions",
            "builtin",
            make_meta("override_a"),
            "def run(meta, params):\n    return 'builtin'\n",
        )
        load_actions(loader, tmp_path)
        registry.resolve_action("override_a")
        builtin_root = os.path.realpath(str(builtin))

        user_root = tmp_path / "user"
        write_plugin(
            user_root / "actions",
            "user",
            make_meta("override_a"),
            "raise RuntimeError('broken override')\n",
        )
        loaded, failed, _, _ = load_actions(
            loader,
            user_root,
            origin="user",
            meta_store=registry.actions_meta,
            func_store=registry.actions_funcs,
        )

        assert (loaded, failed) == (0, 1)
        assert registry.resolve_action("override_a")({}, {}) == "builtin"
        assert registry.plugin_roots["override_a"] == builtin_root
        assert registry.sys_path_entries["override_a"] == builtin_root


class TestLazyLoading:
    def test_discovery_does_not_execute_module(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        marker = tmp_path / "imported.txt"
        code = (
            "from pathlib import Path\n"
            f"Path(r'{marker}').write_text('imported', encoding='utf-8')\n"
            "def run(meta, params):\n"
            "    return None\n"
        )
        write_plugin(tmp_path / "actions", "lazy", make_meta("lazy_a"), code)
        loaded, failed, meta_store, func_store = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        # 发现阶段只登记 meta，不导入模块
        assert "lazy_a" in meta_store
        assert func_store == {}
        assert not marker.exists()

        registry.resolve_action("lazy_a")
        assert marker.exists()
        assert "lazy_a" in func_store

    def test_materialize_runs_only_once(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        counter = tmp_path / "counter.txt"
        code = (
            "from pathlib import Path\n"
            f"path = Path(r'{counter}')\n"
            "count = int(path.read_text(encoding='utf-8')) if path.exists() else 0\n"
            "path.write_text(str(count + 1), encoding='utf-8')\n"
            "def run(meta, params):\n"
            "    return None\n"
        )
        write_plugin(tmp_path / "actions", "lazy", make_meta("lazy_b"), code)
        load_actions(loader, tmp_path)
        first = registry.resolve_action("lazy_b")
        second = registry.resolve_action("lazy_b")
        assert first is second
        assert counter.read_text(encoding="utf-8") == "1"

    def test_resolve_unknown_action_returns_none(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        assert registry.resolve_action("ghost") is None


class TestEngineLazyWorkflow:
    def test_lazy_action_materialized_on_first_workflow(self, tmp_path, monkeypatch):
        # 源码运行默认 strict，未签名用户插件会被拒，这里切到宽松模式
        monkeypatch.setenv("NOTMYFAULT_MODE", "alpha")
        monkeypatch.setattr(
            security_plugins, "_PLUGIN_MANIFEST_FILE", str(tmp_path / "manifest.json")
        )
        engine = AutomationEngine({"rules": [{
            "name": "懒加载规则",
            "event": {"type": "manual", "params": {}},
            "actions": [{"type": "lazy_wf", "params": {}}],
        }]})
        engine._alert_user = lambda *a, **k: None
        engine.triggers_meta["manual"] = {}

        marker = tmp_path / "wf_imported.txt"
        code = (
            "from pathlib import Path\n"
            f"Path(r'{marker}').write_text('x', encoding='utf-8')\n"
            "def run(meta, params):\n"
            "    return None\n"
        )
        write_plugin(tmp_path / "actions", "lazywf", make_meta("lazy_wf"), code)
        loaded, failed = engine._load_plugins(
            base_dir=str(tmp_path),
            plugins_dir="actions",
            json_filename="action.json",
            py_filename="action.py",
            module_prefix="notmyfault.action_",
            meta_store=engine.actions_meta,
            func_store=engine.actions_funcs,
            store_name="Action",
            origin="user",
        )
        assert (loaded, failed) == (1, 0)
        assert "lazy_wf" not in engine.actions_funcs
        assert not marker.exists()

        engine.emit_event("manual", {})
        assert marker.exists()
        assert "lazy_wf" in engine.actions_funcs


class TestAuthorSelfSigning:
    def test_author_signature_verifies(self, tmp_path):
        folder = write_plugin(tmp_path, "plug", make_meta(), "def run(m, p):\n    pass\n")
        signing.self_sign_plugin(folder, Ed25519PrivateKey.generate())
        assert plugin_signature_kind(str(folder), "user") == "author"
        assert verify_plugin_sig(str(folder), "user") is True

    def test_tampered_binary_breaks_signature(self, tmp_path):
        folder = write_plugin(tmp_path, "plug", make_meta(), "def run(m, p):\n    pass\n")
        (folder / "bin").mkdir()
        (folder / "bin" / "tool.exe").write_bytes(b"\x00\x01\x02")
        signing.self_sign_plugin(folder, Ed25519PrivateKey.generate())
        assert plugin_signature_kind(str(folder), "user") == "author"

        (folder / "bin" / "tool.exe").write_bytes(b"\xff\xff\xff")
        assert plugin_signature_kind(str(folder), "user") == "none"

    def test_official_and_author_signatures_do_not_cross(self, tmp_path, monkeypatch):
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        official_key = Ed25519PrivateKey.generate()
        official_pub = official_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        from notmyfault.security import signing_keys
        monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [official_pub])

        # 官方签名的插件不带 public_key.pem，不能冒充作者自签
        official_folder = write_plugin(
            tmp_path / "official", "plug", make_meta(), "def run(m, p):\n    pass\n"
        )
        signing.sign_plugin(official_folder, "action.json", private_key=official_key)
        assert plugin_signature_kind(str(official_folder), "builtin") == "official"
        # 公钥表验签通过但类型单独区分，不会和作者自签混淆
        assert plugin_signature_kind(str(official_folder), "user") == "official-legacy"

        # 作者自签不在官方公钥表里，不能冒充官方签名
        author_folder = write_plugin(
            tmp_path / "author", "plug", make_meta(), "def run(m, p):\n    pass\n"
        )
        signing.self_sign_plugin(author_folder, Ed25519PrivateKey.generate())
        assert plugin_signature_kind(str(author_folder), "user") == "author"
        assert plugin_signature_kind(str(author_folder), "builtin") == "none"

    def test_unsigned_plugin_is_none(self, tmp_path):
        folder = write_plugin(tmp_path, "plug", make_meta(), "def run(m, p):\n    pass\n")
        assert plugin_signature_kind(str(folder), "user") == "none"
        assert plugin_signature_kind(str(folder), "third_party") == "none"

    def test_legacy_signature_without_public_key_is_recognized(self, tmp_path, monkeypatch):
        # 旧安装流程用本地密钥代签的插件没有 public_key.pem，靠公钥表验签
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from notmyfault.security import signing_keys

        builtin_key = Ed25519PrivateKey.generate()
        user_key = Ed25519PrivateKey.generate()
        pubs = [
            k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            for k in (builtin_key, user_key)
        ]
        monkeypatch.setattr(signing_keys, "get_public_keys", lambda: pubs)

        # 公钥表里的用户密钥（模拟 .private/signing_public.pem）签署也能认出
        folder = write_plugin(tmp_path, "plug", make_meta(), "def run(m, p):\n    pass\n")
        signing.sign_plugin(folder, "action.json", private_key=user_key)
        assert not (folder / "public_key.pem").exists()
        assert plugin_signature_kind(str(folder), "user") == "official-legacy"
        assert verify_plugin_sig(str(folder), "user") is True

    def test_legacy_signature_with_unknown_key_is_none(self, tmp_path, monkeypatch):
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from notmyfault.security import signing_keys

        table_key = Ed25519PrivateKey.generate()
        monkeypatch.setattr(
            signing_keys, "get_public_keys",
            lambda: [table_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)],
        )
        folder = write_plugin(tmp_path, "plug", make_meta(), "def run(m, p):\n    pass\n")
        # 签名用的密钥不在公钥表里，回退验签也过不了
        signing.sign_plugin(folder, "action.json", private_key=Ed25519PrivateKey.generate())
        assert plugin_signature_kind(str(folder), "user") == "none"

    def test_user_integrity_covers_all_files(self, tmp_path, monkeypatch):
        manifest_path = tmp_path / "manifest.json"
        monkeypatch.setattr(
            security_plugins, "_PLUGIN_MANIFEST_FILE", str(manifest_path)
        )
        loader, registry, errors = make_loader(tmp_path)
        folder = write_plugin(
            tmp_path / "actions", "integ", make_meta("integ"),
            "def run(meta, params):\n    return None\n",
        )
        (folder / "bin").mkdir()
        (folder / "bin" / "tool.exe").write_bytes(b"\x00")
        load_actions(loader, tmp_path, origin="user")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        recorded = set(manifest["integ"].keys())
        assert "action.json" in recorded
        assert "action.py" in recorded
        assert "bin/tool.exe" in recorded


class TestPluginResource:
    def test_resolves_in_plugin_dir(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        folder = write_plugin(
            tmp_path / "actions", "res", make_meta("res_a"),
            "def run(meta, params):\n    return None\n",
        )
        (folder / "bin").mkdir()
        (folder / "bin" / "tool.exe").write_bytes(b"\x00")
        load_actions(loader, tmp_path)

        resolved = plugin_resource("res_a", "bin", "tool.exe")
        assert resolved == os.path.realpath(str(folder / "bin" / "tool.exe"))

    def test_escape_rejected(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        write_plugin(
            tmp_path / "actions", "res", make_meta("res_b"),
            "def run(meta, params):\n    return None\n",
        )
        load_actions(loader, tmp_path)
        with pytest.raises(ValueError):
            plugin_resource("res_b", "..", "secret.txt")
        with pytest.raises(ValueError):
            plugin_resource("res_b", "sub", "..", "..", "secret.txt")

    def test_unknown_plugin_rejected(self, tmp_path):
        make_loader(tmp_path)
        with pytest.raises(ValueError):
            plugin_resource("ghost", "x.bin")


class TestBuildSchema:
    def test_valid_build_field(self):
        meta = make_meta(build={
            "command": ["gcc main.c -o bin/tool.exe"],
            "outputs": ["bin/tool.exe"],
        })
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok, errors

    def test_build_must_be_object(self):
        ok, errors = validate_plugin_meta(make_meta(build="gcc"), "action")
        assert not ok
        assert any("'build' 必须是对象" in e for e in errors)

    def test_build_command_must_be_nonempty_string_array(self):
        ok, errors = validate_plugin_meta(make_meta(build={"command": []}), "action")
        assert not ok
        ok, errors = validate_plugin_meta(make_meta(build={"command": [1]}), "action")
        assert not ok

    def test_build_outputs_must_stay_inside_plugin_dir(self):
        meta = make_meta(build={"outputs": ["../escape.bin"]})
        ok, errors = validate_plugin_meta(meta, "action")
        assert not ok
        assert any("相对路径" in e for e in errors)

    def test_builtin_build_hook_rejected_by_loader(self, tmp_path):
        loader, registry, errors = make_loader(tmp_path)
        write_plugin(
            tmp_path / "actions", "bh",
            make_meta(build={"command": ["make"]}),
            "def run(meta, params):\n    return None\n",
        )
        loaded, failed, _, _ = load_actions(loader, tmp_path, origin="builtin")
        assert (loaded, failed) == (0, 1)
        assert any("build 编译钩子" in msg for _, _, msg in errors)


@pytest.fixture
def clean_sudo_state():
    yield
    sudo._engine_token = None
    sudo._admin_plugins.clear()
    sudo._admin_by_module.clear()
    sudo._authorization_mode = "direct"
    sudo._admin_broker = None


class TestAdminSessionRecheck:
    def _make_engine(self, mode):
        config = {
            "rules": [{
                "name": "admin 规则",
                "event": {"type": "manual", "params": {}},
                "actions": [{"type": "admin_action", "params": {}}],
            }],
            "settings": {"admin_authorization_mode": mode},
        }
        engine = AutomationEngine(config)
        engine.actions_meta["admin_action"] = {"permissions": ["admin"]}
        return engine

    def test_engine_start_without_session_alerts(self, clean_sudo_state):
        engine = self._make_engine("engine_start")
        alerts = []
        engine._alert_user = (
            lambda title, message, open_dashboard=False: alerts.append(title)
        )
        engine._recheck_admin_session()
        assert alerts == ["需要重启以完成管理员授权"]

    def test_engine_start_with_session_silent(self, clean_sudo_state):
        engine = self._make_engine("engine_start")
        sudo._admin_broker = object()
        alerts = []
        engine._alert_user = (
            lambda title, message, open_dashboard=False: alerts.append(title)
        )
        engine._recheck_admin_session()
        assert alerts == []

    def test_per_execution_never_alerts(self, clean_sudo_state):
        engine = self._make_engine("per_execution")
        alerts = []
        engine._alert_user = (
            lambda title, message, open_dashboard=False: alerts.append(title)
        )
        engine._recheck_admin_session()
        assert alerts == []

class FakeRunner:
    engine_running = False
    engine_state = "stopped"
    current_engine = None

    def start_engine(self):
        return True

    def stop_engine(self):
        return True

    def request_process_shutdown(self, force_after=10):
        pass


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr(api_server, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(api_server, "API_TOKEN_FILE", str(tmp_path / ".api_token"))
    monkeypatch.setattr(api_server, "_PRIVATE_DIR", tmp_path / "private")
    api = EngineAPI(FakeRunner())
    user_dir = tmp_path / "user_plugins"
    api._get_user_plugins_dir = lambda: str(user_dir)
    client = TestClient(api.app)
    headers = {"Authorization": f"Bearer {api_server.API_TOKEN}"}
    return SimpleNamespace(
        api=api, client=client, headers=headers, tmp_path=tmp_path, user_dir=user_dir
    )


def build_nmfp_with_files(tmp_path, meta, ptype, tag, extra=None, sign=False):
    """归档内恰好一个插件文件夹，extra 是相对路径到内容的映射"""
    json_name = "trigger.json" if ptype == "triggers" else "action.json"
    source = tmp_path / f"src_{tag}" / f"plugin_{tag}"
    source.mkdir(parents=True)
    (source / json_name).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (source / "main.py").write_text(
        "def run(event, params):\n    return True\n", encoding="utf-8"
    )
    for rel_path, content in (extra or {}).items():
        target = source / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    if sign:
        signing.self_sign_plugin(source, Ed25519PrivateKey.generate())
    archive = tmp_path / f"plugin_{tag}.nmfp"
    folder = f"plugin_{tag}"
    with py7zr.SevenZipFile(str(archive), "w") as zf:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                zf.write(path, f"{folder}/{path.relative_to(source).as_posix()}")
    return archive


def install_file(client, headers, archive):
    with open(archive, "rb") as f:
        return client.post(
            "/api/plugins/install",
            headers=headers,
            files={"file": (archive.name, f, "application/octet-stream")},
        )


def preview_file(client, headers, archive):
    with open(archive, "rb") as file:
        return client.post(
            "/api/plugins/preview",
            headers=headers,
            files={"file": (archive.name, file, "application/octet-stream")},
        )


def python_step(code):
    return f'"{sys.executable}" -c "{code}"'


class TestInstallBuildHook:
    def test_direct_install_with_build_hook_requires_preview(self, api_env):
        meta = make_meta("actions", build={
            "command": [python_step("pass")],
        })
        archive = build_nmfp_with_files(
            api_env.tmp_path, meta, "actions", "direct_build"
        )

        response = install_file(api_env.client, api_env.headers, archive)

        assert response.status_code == 400
        assert "请先预览后安装" in response.json()["error"]
        assert any(risk["id"] == "build_hook" for risk in response.json()["risks"])

    def test_preview_reports_build_without_running_it(self, api_env):
        marker = api_env.tmp_path / "preview-build-ran.txt"
        meta = make_meta("actions", build={
            "command": [python_step(
                f"import pathlib; pathlib.Path(r'{marker}').write_text('ran')"
            )],
        })
        archive = build_nmfp_with_files(
            api_env.tmp_path, meta, "actions", "preview_build"
        )

        with open(archive, "rb") as file:
            preview = api_env.client.post(
                "/api/plugins/preview",
                headers=api_env.headers,
                files={"file": (archive.name, file, "application/octet-stream")},
            )

        assert preview.status_code == 200, preview.text
        assert not marker.exists()
        assert any(risk["id"] == "build_hook" for risk in preview.json()["risks"])

        install = api_env.client.post(
            "/api/plugins/install",
            headers=api_env.headers,
            data={"preview_token": preview.json()["preview_token"]},
        )

        assert install.status_code == 200, install.text
        assert marker.read_text(encoding="utf-8") == "ran"

    def test_build_hook_runs_and_outputs_installed(self, api_env):
        meta = make_meta("actions", build={
            "command": [python_step("import pathlib; pathlib.Path('built.bin').write_bytes(b'x')")],
            "outputs": ["built.bin"],
        })
        archive = build_nmfp_with_files(api_env.tmp_path, meta, "actions", "built")
        preview = preview_file(api_env.client, api_env.headers, archive)
        assert preview.status_code == 200, preview.text
        response = api_env.client.post(
            "/api/plugins/install",
            headers=api_env.headers,
            data={"preview_token": preview.json()["preview_token"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        dest = api_env.user_dir / "actions" / meta["id"]
        assert (dest / "built.bin").exists()

    def test_build_hook_failure_blocks_install(self, api_env):
        meta = make_meta("actions", build={
            "command": [python_step("raise SystemExit(3)")],
        })
        archive = build_nmfp_with_files(api_env.tmp_path, meta, "actions", "boom")
        preview = preview_file(api_env.client, api_env.headers, archive)
        assert preview.status_code == 200, preview.text
        response = api_env.client.post(
            "/api/plugins/install",
            headers=api_env.headers,
            data={"preview_token": preview.json()["preview_token"]},
        )
        assert response.status_code == 400
        assert "build 命令失败" in response.json()["error"]

    def test_build_hook_missing_output_blocks_install(self, api_env):
        meta = make_meta("actions", build={
            "command": [python_step("pass")],
            "outputs": ["nothere.bin"],
        })
        archive = build_nmfp_with_files(api_env.tmp_path, meta, "actions", "miss")
        preview = preview_file(api_env.client, api_env.headers, archive)
        assert preview.status_code == 200, preview.text
        response = api_env.client.post(
            "/api/plugins/install",
            headers=api_env.headers,
            data={"preview_token": preview.json()["preview_token"]},
        )
        assert response.status_code == 400
        assert "build 产物不存在" in response.json()["error"]

    def test_build_hook_strips_archive_signature(self, api_env):
        meta = make_meta("actions", build={
            "command": [python_step("import pathlib; pathlib.Path('built.bin').write_bytes(b'x')")],
            "outputs": ["built.bin"],
        })
        archive = build_nmfp_with_files(
            api_env.tmp_path, meta, "actions", "stripped", sign=True
        )
        preview = preview_file(api_env.client, api_env.headers, archive)
        assert preview.status_code == 200, preview.text
        response = api_env.client.post(
            "/api/plugins/install",
            headers=api_env.headers,
            data={"preview_token": preview.json()["preview_token"]},
        )
        assert response.status_code == 200, response.text
        dest = api_env.user_dir / "actions" / meta["id"]
        # 编译产物不继承归档签名，按未签名插件处理
        assert not (dest / "signature.sig").exists()
        assert not (dest / "public_key.pem").exists()

    def test_symlink_executable_rejected(self, api_env):
        meta = make_meta("actions")
        source = api_env.tmp_path / "src_sym" / "plugin_sym"
        source.mkdir(parents=True)
        (source / "action.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        (source / "main.py").write_text(
            "def run(event, params):\n    return True\n", encoding="utf-8"
        )
        target = api_env.tmp_path / "real_target.txt"
        target.write_text("target")
        link = source / "loader.exe"
        try:
            os.symlink(str(target), str(link))
        except OSError:
            pytest.skip("当前系统不允许创建符号链接")
        archive = api_env.tmp_path / "plugin_sym.nmfp"
        with py7zr.SevenZipFile(str(archive), "w") as zf:
            for path in sorted(source.rglob("*")):
                if path.is_file() or path.is_symlink():
                    zf.write(path, f"plugin_sym/{path.relative_to(source).as_posix()}")
        response = install_file(api_env.client, api_env.headers, archive)
        assert response.status_code == 400
        assert "符号链接" in response.json()["error"]
