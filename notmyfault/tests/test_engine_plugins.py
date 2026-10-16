import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from notmyfault.tests.api_support import create_test_engine
from notmyfault.tests.canvas_fixtures import native_rule
from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
from notmyfault.security.security import SecurityMode


def make_engine(rules=None):
    engine = create_test_engine({"rules": rules or []})
    engine._alert_user = lambda *a, **k: None
    return engine


def make_meta(plugin_id="plug_a", **overrides):
    meta = {
        "id": plugin_id,
        "name": "测试插件",
        "description": "测试用插件",
        "version_code": 1,
        "version": "1.0",
        "package_name": "com.test.plug",
    }
    meta["trigger_api" if "trigger_api" in overrides or "semantic" in overrides else "execution_api"] = "event-v2" if "trigger_api" in overrides or "semantic" in overrides else "context-v1"
    meta.update(overrides)
    if "admin" in meta.get("permissions", []) and "security" not in meta:
        meta["security"] = {"admin_executables": ["cmd.exe"]}
    return meta


def write_plugin(root, folder, meta, py_code, json_name="action.json", py_name="action.py"):
    meta = dict(meta)
    meta.pop("execution_api" if json_name == "trigger.json" else "trigger_api", None)
    meta.setdefault("execution_api" if json_name == "action.json" else "trigger_api", "context-v1" if json_name == "action.json" else "event-v2")
    folder_path = Path(root) / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    (folder_path / json_name).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (folder_path / py_name).write_text(py_code, encoding="utf-8")
    return folder_path


class SudoStub:
    def __init__(self):
        self.calls = []

    def authorize_plugin(
        self,
        plugin_id,
        token,
        module=None,
        allowed_executables=None,
    ):
        self.calls.append(("authorize", plugin_id))

    def deauthorize_plugin(self, plugin_id, token):
        self.calls.append(("deauthorize", plugin_id))


def make_loader(tmp_path, mode=SecurityMode.PERMISSIVE, config=None):
    registry = PluginRegistry()
    plugin_errors = []
    diagnostics = SimpleNamespace(
        record_plugin_error=lambda store, pid, msg: plugin_errors.append((store, pid, msg))
    )
    sudo = SudoStub()
    loader = PluginLoader(
        registry=registry,
        config=config or {},
        diagnostics=diagnostics,
        security_mode=mode,
        sudo=sudo,
        engine_token="token",
        integrity_errors=[],
        plugin_manifest_path=str(tmp_path / "manifest.json"),
    )
    return loader, registry, plugin_errors, sudo


def load_actions(loader, tmp_path, meta_store=None, func_store=None, origin="builtin"):
    meta_store, func_store = loader._registry.stores("action")
    loaded, failed = loader.load(str(tmp_path / "actions"), "action", origin=origin)
    return loaded, failed, meta_store, func_store


def load_triggers(loader, tmp_path, meta_store=None, func_store=None, origin="builtin"):
    meta_store, func_store = loader._registry.stores("trigger")
    loaded, failed = loader.load(str(tmp_path / "triggers"), "trigger", origin=origin)
    return loaded, failed, meta_store, func_store


def sign_with_test_key(plugin_dir, monkeypatch):
    """生成临时 Ed25519 密钥对并用它签名，公钥表同步替换"""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from notmyfault.security import signing, signing_keys

    key = ed25519.Ed25519PrivateKey.generate()
    public_bytes = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [public_bytes])
    signing.self_sign_plugin(Path(plugin_dir), key)
    signing.counter_sign_author_key(Path(plugin_dir), key)


CLEAN_RUN = "def run_with_context(meta, params, context):\n    return None\n"


class TestEngineStart:
    def test_engine_no_rules(self):
        engine = create_test_engine({})
        assert engine.rules == []
        engine.emit_event("hotkey", {})


    def test_get_diagnostics_initial(self):
        engine = make_engine(rules=[{"name": "a", "workflow": {"entry": None, "nodes": [], "edges": []}}])
        diag = engine.get_diagnostics()
        assert diag["uptime_seconds"] == 0
        assert diag["plugins"]["actions_loaded"] == 0
        assert diag["plugins"]["triggers_loaded"] == 0
        assert diag["rules"]["total"] == 1

    def test_start_no_trigger_threads_alerts(self, monkeypatch):
        from notmyfault.platform import platform_support
        monkeypatch.setattr(platform_support, "show_notification", lambda *a, **k: None)
        import os
        if os.name == "nt":
            from Win_toaster import AUMID_Register
            monkeypatch.setattr(AUMID_Register, "register_toaster", lambda: None)

        alerts = []
        engine = make_engine(rules=[native_rule({"type": "ghost_trigger", "params": {}}, [{"type": "noop", "params": {}}], name="r")])
        engine._alert_user = (
            lambda title, message, open_dashboard=False: alerts.append((title, open_dashboard))
        )
        engine._security_mode = SecurityMode.PERMISSIVE
        stopped = threading.Event()
        monkeypatch.setattr(engine._rule_watcher, "start", stopped.set)
        engine.start(shutdown_event=stopped)
        assert engine.get_diagnostics()["rules"]["issue_count"] == 1
        assert alerts


class TestLoadPlugins:
    @pytest.mark.parametrize("factory,valid", [
        ("", False),
        ("def create_sampler(context, required):\n    pass\n", False),
        ("def create_sampler(context):\n    return object()\n", True),
    ])
    def test_sampled_trigger_requires_context_factory(self, tmp_path, factory, valid):
        loader, registry, errors, _ = make_loader(tmp_path)
        meta = make_meta(trigger_api="event-v2", sampling_api="poll-v1")
        write_plugin(tmp_path / "triggers", "plug_a", meta,
                     "def run(meta, config, emit, stop):\n    pass\n" + factory,
                     json_name="trigger.json", py_name="trigger.py")
        loaded, failed, _, _ = load_triggers(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert (registry.resolve_trigger("plug_a") is not None) is valid
        assert ("plug_a" in registry.triggers_funcs) is valid
        if not valid:
            assert any("create_sampler(context)" in message for _, _, message in errors)

    def test_json_parse_error(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        folder = tmp_path / "actions" / "bad"
        folder.mkdir(parents=True)
        (folder / "action.json").write_text("{bad", encoding="utf-8")
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert any("JSON 解析失败" in msg for _, _, msg in errors)

    def test_missing_entrypoint_is_recorded(self, tmp_path):
        # 平台适用但入口文件不在：以前是静默跳过，用户看不到插件为什么用不了
        loader, registry, errors, _ = make_loader(tmp_path)
        folder = tmp_path / "actions" / "no_code"
        folder.mkdir(parents=True)
        (folder / "action.json").write_text(json.dumps(make_meta()), encoding="utf-8")

        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert any("当前平台入口不存在" in msg for _, _, msg in errors)

    def test_diagnostics_returns_every_retained_plugin_error(self):
        engine = make_engine()
        for index in range(25):
            engine._diag_obj.record_plugin_error("Action", f"p{index}", "boom")

        plugins = engine.get_diagnostics()["plugins"]
        assert plugins["error_count"] == 25
        assert len(plugins["errors"]) == 25

    @pytest.mark.parametrize("mode", list(SecurityMode))
    @pytest.mark.parametrize("kind,execution_mode", [("action", "in-process"), ("action", "isolated"), ("trigger", "in-process")])
    @pytest.mark.parametrize("retained_errors", [0, 200])
    @pytest.mark.parametrize("command_ready", [False, True])
    def test_plugin_load_check_requires_registered_commands(
        self, tmp_path, monkeypatch, mode, kind, execution_mode, retained_errors, command_ready
    ):
        from notmyfault.extensions.session import ExtensionContext, ExtensionSession

        monkeypatch.setattr("notmyfault.core.engine._detect_security_mode", lambda: mode)
        engine = make_engine()
        plugin_id = "with_command"
        meta = make_meta(plugin_id, contributes={"commands": [{
            "id": "perform", "title": "执行", "handler": "command.py:perform",
        }]})
        if kind == "trigger":
            meta["trigger_api"] = "event-v2"
        else:
            meta["execution_mode"] = execution_mode
        source = CLEAN_RUN if kind == "action" else (
            "def run(meta, config, emit, stop_event):\n    pass\n"
        )
        root = tmp_path / (kind + "s")
        folder = write_plugin(
            root, plugin_id, meta, source,
            json_name=f"{kind}.json", py_name=f"{kind}.py",
        )
        command_file = folder / "command.py"
        command_source = "def perform(context, payload):\n    return payload\n"
        command_file.write_text(command_source if command_ready else "x = 1\n", encoding="utf-8")
        sign_with_test_key(folder, monkeypatch)

        def invoke(value):
            session = ExtensionSession(
                plugin_id=plugin_id, command_id="perform", plugin_meta=meta,
                source_kind="parameter_editors", source_id="editor",
                allowed_commands=("perform",), data_type=None, current_value=None,
                value_type="object",
            )
            try:
                return session.invoke(
                    engine.extensions.handler(plugin_id, "perform"),
                    ExtensionContext(session, engine.extensions), {"value": value},
                )
            finally:
                session.close()

        try:
            assert engine._plugin_loader.load(str(root), kind) == (1, 0)
            for index in range(retained_errors):
                previous_id = plugin_id if index == 0 else f"other_{index}"
                engine._diag_obj.record_plugin_error("plugin", previous_id, "old error")
            assert engine.check_plugin_load(kind, plugin_id, str(folder)) is command_ready
            assert engine.check_plugin_load(kind, plugin_id, str(folder)) is command_ready
            if command_ready:
                assert invoke(1) == {"value": 1}
            else:
                assert engine.extensions.handler(plugin_id, "perform") is None
                command_file.write_text(command_source, encoding="utf-8")
                sign_with_test_key(folder, monkeypatch)
                assert engine._plugin_loader.load(str(root), kind) == (1, 0)
                assert engine.check_plugin_load(kind, plugin_id, str(folder)) is True
                assert invoke(2) == {"value": 2}
        finally:
            engine.shutdown()

    def test_schema_validation_failure(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        meta = make_meta()
        del meta["name"]
        write_plugin(tmp_path / "actions", "bad", meta, CLEAN_RUN)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert any("schema 校验失败" in msg for _, _, msg in errors)

    def test_malformed_metadata_does_not_stop_scan(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        broken = make_meta(
            plugin_id="broken",
            params=None,
            security={"literal_only_params": ["p"]},
        )
        write_plugin(tmp_path / "actions", "broken", broken, CLEAN_RUN)
        write_plugin(
            tmp_path / "actions", "healthy", make_meta(plugin_id="healthy"), CLEAN_RUN
        )
        loaded, failed, meta_store, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 1)
        assert "healthy" in meta_store
        assert any(plugin == "broken" for _store, plugin, _msg in errors)

    def test_missing_run_function(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        write_plugin(tmp_path / "actions", "no_run", make_meta(), "x = 1\n")
        # 发现阶段不执行代码，缺 run() 要到首次物化才暴露
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert registry.resolve_action("plug_a") is None
        assert any("缺少 run_with_context() 函数" in msg for _, _, msg in errors)

    def test_python_import_error(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        write_plugin(
            tmp_path / "actions", "broken", make_meta(),
            "raise ImportError('依赖缺失')\n",
        )
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert registry.resolve_action("plug_a") is None
        assert any("Python 加载异常" in msg for _, _, msg in errors)

    def test_builtin_plugin_disabled_by_config(self, tmp_path):
        loader, registry, errors, _ = make_loader(
            tmp_path, config={"disabled_plugins": {"actions": ["plug_a"]}}
        )
        write_plugin(tmp_path / "actions", "off", make_meta(), CLEAN_RUN)
        loaded, failed, meta_store, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 0)
        assert meta_store == {}
        assert registry.resolve_action("plug_a") is None

    def test_user_plugin_disabled_by_config(self, tmp_path):
        loader, registry, errors, _ = make_loader(
            tmp_path, config={"disabled_plugins": {"actions": ["plug_a"]}}
        )
        write_plugin(tmp_path / "actions", "off", make_meta(), CLEAN_RUN)
        loaded, failed, meta_store, _ = load_actions(loader, tmp_path, origin="user")
        assert (loaded, failed) == (0, 0)
        assert meta_store == {}
        assert registry.resolve_action("plug_a") is None

    def test_disabled_plugin_loads_again_after_config_cleared(self, tmp_path):
        write_plugin(tmp_path / "actions", "off", make_meta(), CLEAN_RUN)
        disabled_loader, _, _, _ = make_loader(
            tmp_path, config={"disabled_plugins": {"actions": ["plug_a"]}}
        )
        assert load_actions(disabled_loader, tmp_path)[:2] == (0, 0)
        loader, registry, errors, _ = make_loader(tmp_path)
        loaded, failed, meta_store, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert meta_store["plug_a"]["id"] == "plug_a"

    def test_successful_plugin_load(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        write_plugin(tmp_path / "actions", "good", make_meta(), CLEAN_RUN)
        loaded, failed, meta_store, func_store = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        # 动作插件懒物化：发现阶段只写 meta，首次解析才导入
        assert func_store == {}
        assert meta_store["plug_a"]["origin"] == "builtin"
        assert registry.get_module("plug_a") is None
        assert callable(registry.resolve_action("plug_a"))
        assert callable(func_store["plug_a"])
        assert registry.get_module("plug_a") is not None
        assert errors == []

    def test_setup_raises_exception(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        code = (
            "def setup(meta):\n"
            "    raise RuntimeError('setup 炸了')\n"
            "def run(meta, config, emit, stop_event):\n"
            "    pass\n"
        )
        write_plugin(
            tmp_path / "triggers", "bad_setup", make_meta("bad_setup"),
            code, json_name="trigger.json", py_name="trigger.py",
        )
        loaded, failed, meta_store, func_store = load_triggers(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert registry.resolve_trigger("bad_setup") is None
        assert "bad_setup" not in func_store
        assert "bad_setup" not in meta_store
        assert registry.get_module("bad_setup") is None
        assert any("setup() 执行异常" in msg for _, _, msg in errors)

    def test_sudo_import_warning_no_admin_permission(self, tmp_path, capsys):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.PERMISSIVE)
        code = (
            "import notmyfault.security.sudo\n"
            "def run_with_context(meta, params, context):\n"
            "    return None\n"
        )
        write_plugin(tmp_path / "actions", "warned", make_meta("warned"), code)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        # 宽松模式只警告，不拒载
        assert (loaded, failed) == (1, 0)
        assert "未在元数据中声明" in capsys.readouterr().err

    def test_override_plugin_does_not_inherit_admin(self, tmp_path):
        loader, registry, errors, sudo = make_loader(tmp_path, mode=SecurityMode.PERMISSIVE)
        meta_store, func_store = {}, {}

        admin_code = (
            "from notmyfault.security import sudo\n"
            "def run_with_context(meta, params, context):\n"
            "    return None\n"
        )
        write_plugin(
            tmp_path / "actions", "adminp",
            make_meta("adminp", permissions=["admin"]), admin_code,
        )
        loaded, failed, meta_store, func_store = load_actions(
            loader, tmp_path, meta_store, func_store
        )
        assert (loaded, failed) == (1, 0)
        # 懒物化后才走 sudo 授权
        registry.resolve_action("adminp")
        assert sudo.calls == [("authorize", "adminp")]

        # 用户覆盖版没有声明 admin，旧授权必须被撤销且不再重新授权
        write_plugin(tmp_path / "actions", "adminp", make_meta("adminp"), CLEAN_RUN)
        loaded, failed, meta_store, func_store = load_actions(
            loader, tmp_path, meta_store, func_store, origin="user"
        )
        assert (loaded, failed) == (1, 0)
        assert sudo.calls == [("authorize", "adminp"), ("deauthorize", "adminp")]
        assert meta_store["adminp"]["origin"] == "user"

    @pytest.mark.parametrize("mode", list(SecurityMode))
    @pytest.mark.parametrize(
        "old_mode,new_mode,valid",
        [
            ("isolated", "isolated", True),
            ("isolated", "in-process", True),
            ("in-process", "isolated", True),
            ("in-process", "in-process", True),
            ("isolated", "in-process", False),
            ("in-process", "in-process", False),
        ],
    )
    def test_replace_materialized_action(
        self, tmp_path, monkeypatch, mode, old_mode, new_mode, valid
    ):
        from notmyfault.extensions.session import ExtensionContext, ExtensionSession
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from notmyfault.security import signing, signing_keys

        key = ed25519.Ed25519PrivateKey.generate()
        public_bytes = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [public_bytes])
        loader, registry, errors, _sudo = make_loader(tmp_path, mode=mode)
        source = "from helper import value\ndef run_with_context(meta, params, context): return value\n"
        contributes = {"commands": [{"id": "value", "title": "读取", "handler": "command.py:read_value"}]}
        command_source = "from helper import value\ndef read_value(context, payload): return context.result(value)\n"
        old_folder = write_plugin(
            tmp_path / "builtin" / "actions", "plug_a",
            make_meta(execution_mode=old_mode, contributes=contributes), source,
        )
        (old_folder / "command.py").write_text(command_source, encoding="utf-8")
        (old_folder / "helper.py").write_text("value = 1\n", encoding="utf-8")
        (old_folder / "version.txt").write_text("1", encoding="utf-8")
        signing.sign_plugin(old_folder, private_key=key)

        try:
            assert load_actions(loader, tmp_path / "builtin")[:2] == (1, 0)
            old_run = registry.resolve_action("plug_a")
            assert old_run(registry.actions_meta["plug_a"], {}, {}) == 1
            old_meta = registry.actions_meta["plug_a"]

            new_folder = write_plugin(
                tmp_path / "user" / "actions", "plug_a",
                make_meta(execution_mode=new_mode, version_code=2, version="2.0", contributes=contributes),
                source if valid else "from helper import value\n",
            )
            (new_folder / "helper.py").write_text("value = 2\n", encoding="utf-8")
            (new_folder / "command.py").write_text(command_source, encoding="utf-8")
            (new_folder / "version.txt").write_text("2", encoding="utf-8")
            signing.self_sign_plugin(new_folder, key)
            signing.counter_sign_author_key(new_folder, key)

            loaded, failed, meta_store, _func_store = load_actions(
                loader, tmp_path / "user", origin="user"
            )
            run = registry.resolve_action("plug_a")
            if valid:
                assert (loaded, failed) == (1, 0)
                assert run is not old_run
                assert run(meta_store["plug_a"], {}, {}) == 2
                assert meta_store["plug_a"]["version_code"] == 2
                assert meta_store["plug_a"]["origin"] == "user"
                assert Path(registry.resource("plug_a", "version.txt")).read_text() == "2"
                assert errors == []
            else:
                assert (loaded, failed) == (0, 1)
                assert run is old_run
                assert meta_store["plug_a"] is old_meta
                assert run(meta_store["plug_a"], {}, {}) == 1
                assert Path(registry.resource("plug_a", "version.txt")).read_text() == "1"
                assert any("缺少 run_with_context() 函数" in msg for _, _, msg in errors)
            session = ExtensionSession(
                plugin_id="plug_a", command_id="value", plugin_meta=meta_store["plug_a"],
                source_kind="parameter_editors", source_id="editor",
                allowed_commands=("value",), data_type=None, current_value=None, value_type="int",
            )
            try:
                result = session.invoke(
                    registry.extensions.handler("plug_a", "value"),
                    ExtensionContext(session, registry.extensions), {},
                )
                assert result["data"] == (2 if valid else 1)
            finally:
                session.close()
            assert "plug_a" not in registry.pending
        finally:
            registry.clear()


class TestSecurityScanIntegration:
    def test_builtin_plugin_does_not_use_user_hash_cache(self, tmp_path):
        manifest_path = tmp_path / "manifest.json"
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.PERMISSIVE)
        write_plugin(tmp_path / "actions", "good", make_meta(), CLEAN_RUN)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        # builtin 插件依赖构建签名，不写入用户哈希清单
        assert (loaded, failed) == (1, 0)
        assert not manifest_path.exists()

    def test_trigger_with_wrong_entry_signature_is_rejected(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        code = "def run(meta, config):\n    pass\n"
        write_plugin(
            tmp_path / "triggers", "badapi",
            make_meta("badapi", trigger_api="event-v2"),
            code, json_name="trigger.json", py_name="trigger.py",
        )
        loaded, failed, meta_store, func_store = load_triggers(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert registry.resolve_trigger("badapi") is None
        assert "badapi" not in func_store
        assert "badapi" not in meta_store
        assert any("触发器入口参数不符" in msg for _, _, msg in errors)

    def test_permissive_warns_but_loads(self, tmp_path, capsys):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.PERMISSIVE)
        code = "import subprocess\ndef run_with_context(meta, params, context):\n    return None\n"
        write_plugin(tmp_path / "actions", "wild", make_meta("wild"), code)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert "未在清单声明" in capsys.readouterr().err

    def test_strict_allows_declared_subprocess(self, tmp_path, monkeypatch):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        code = "import subprocess\ndef run_with_context(meta, params, context):\n    return None\n"
        folder = write_plugin(
            tmp_path / "actions", "declared",
            make_meta("declared", permissions=["external_binary"]), code,
        )
        sign_with_test_key(folder, monkeypatch)
        loaded, failed, _, func_store = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert callable(registry.resolve_action("declared"))
        assert "declared" in func_store
        assert errors == []

    def test_strict_rejects_undeclared_subprocess(self, tmp_path, monkeypatch):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        code = "import subprocess\ndef run_with_context(meta, params, context):\n    return None\n"
        folder = write_plugin(tmp_path / "actions", "sneaky", make_meta("sneaky"), code)
        sign_with_test_key(folder, monkeypatch)
        loaded, failed, _, func_store = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert "sneaky" not in func_store
        assert any("未声明能力" in msg for _, _, msg in errors)

    def test_strict_invalid_signature_is_rejected_before_module_execution(
        self, tmp_path, monkeypatch
    ):
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from notmyfault.security import signing_keys

        key = ed25519.Ed25519PrivateKey.generate()
        public_bytes = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [public_bytes])

        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        marker = tmp_path / "executed.txt"
        code = (
            f"from pathlib import Path\n"
            f"Path(r'{marker}').write_text('x', encoding='utf-8')\n"
            "def run_with_context(meta, params, context):\n"
            "    return None\n"
        )
        folder = write_plugin(tmp_path / "actions", "tampered", make_meta("tampered"), code)
        (folder / "signature.sig").write_bytes(b"\x00" * 64)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        # 验签失败必须发生在模块导入之前
        assert not marker.exists()
        assert any("签名无效" in msg for _, _, msg in errors)

    def test_strict_rejects_self_signed_admin_plugin(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from notmyfault.security import signing

        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        admin_code = (
            "from notmyfault.security.sudo import run_as_admin\n"
            "def run_with_context(meta, params, context):\n"
            "    return None\n"
        )
        folder = write_plugin(
            tmp_path / "actions", "selfadmin",
            make_meta("selfadmin", permissions=["admin"]), admin_code,
        )
        key = ed25519.Ed25519PrivateKey.generate()
        signing.self_sign_plugin(folder, private_key=key)
        loaded, failed, _, _ = load_actions(loader, tmp_path, origin="user")
        assert (loaded, failed) == (0, 1)
        assert any("未使用官方签名" in msg for _, _, msg in errors)

    def test_strict_rejects_user_admin_plugin_without_official_signature(self, tmp_path, monkeypatch):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        admin_code = (
            "from notmyfault.security.sudo import run_as_admin\n"
            "def run_with_context(meta, params, context):\n"
            "    return None\n"
        )
        folder = write_plugin(
            tmp_path / "actions", "officialadmin",
            make_meta("officialadmin", permissions=["admin"]), admin_code,
        )
        sign_with_test_key(folder, monkeypatch)
        loaded, failed, _, _ = load_actions(loader, tmp_path, origin="user")
        assert (loaded, failed) == (0, 1)
        assert any("未使用官方签名" in msg for _, _, msg in errors)

    def test_strict_loads_builtin_admin_plugin(self, tmp_path, monkeypatch):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        admin_code = (
            "from notmyfault.security.sudo import run_as_admin\n"
            "def run_with_context(meta, params, context):\n"
            "    return None\n"
        )
        folder = write_plugin(
            tmp_path / "actions", "builtinadmin",
            make_meta("builtinadmin", permissions=["admin"]), admin_code,
        )
        sign_with_test_key(folder, monkeypatch)
        loaded, failed, meta_store, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert meta_store["builtinadmin"]["signature_kind"] == "official"

    def test_strict_rejects_user_plugin_when_recorded_hashes_change(
        self, tmp_path, monkeypatch
    ):
        loader, _, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        folder = write_plugin(
            tmp_path / "actions", "hashed", make_meta("hashed"), CLEAN_RUN
        )
        sign_with_test_key(folder, monkeypatch)
        loaded, failed, _, _ = load_actions(loader, tmp_path, origin="user")
        assert (loaded, failed) == (1, 0)
        (folder / "action.py").write_text(
            "def run_with_context(meta, params, context):\n    return {'value': 2}\n",
            encoding="utf-8",
        )
        sign_with_test_key(folder, monkeypatch)
        loader2, _, errors2, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        loaded2, failed2, _, _ = load_actions(loader2, tmp_path, origin="user")
        assert (loaded2, failed2) == (0, 1)
        assert any("已被修改" in msg for _, _, msg in errors2)

    def test_author_signature_requires_user_counter_signature(self, tmp_path, monkeypatch):
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from notmyfault.security import signing, signing_keys

        folder = write_plugin(tmp_path / "actions", "authored", make_meta("authored"), CLEAN_RUN)
        author_key = ed25519.Ed25519PrivateKey.generate()
        signing.self_sign_plugin(folder, private_key=author_key)

        loader, _, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        loaded, failed, _, _ = load_actions(loader, tmp_path, origin="user")
        assert (loaded, failed) == (0, 1)
        assert any("副签" in msg for _, _, msg in errors)

        user_key = ed25519.Ed25519PrivateKey.generate()
        user_pub = user_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [user_pub])
        signing.counter_sign_author_key(folder, private_key=user_key)

        loader2, _, errors2, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        loaded, failed, meta_store, _ = load_actions(loader2, tmp_path, origin="user")
        assert (loaded, failed) == (1, 0)
        assert meta_store["authored"]["signature_kind"] == "author"
        assert errors2 == []


def test_plugin_registry_registers_and_unregisters_atomically():
    registry = PluginRegistry()
    module = SimpleNamespace()
    meta = make_meta("plug_a")
    run = lambda m, p: None

    registry.register("action", "plug_a", meta, run, module)
    assert registry.actions_funcs["plug_a"] is run
    assert registry.actions_meta["plug_a"] is meta
    assert registry.get_module("plug_a") is module

    registry.unregister("action", "plug_a")
    assert "plug_a" not in registry.actions_funcs
    assert "plug_a" not in registry.actions_meta
    assert registry.get_module("plug_a") is None
    # 重复卸载不产生异常
    registry.unregister("action", "plug_a")


@pytest.mark.parametrize("relative", [False, True])
def test_plugins_keep_separate_verified_sibling_modules(tmp_path, monkeypatch, relative):
    import sys
    from notmyfault.security import plugin_loader

    loader, registry, errors, _sudo = make_loader(tmp_path)
    prefix = "." if relative else ""
    for plugin_id, value in (("first", 1), ("second", 2)):
        folder = write_plugin(tmp_path / "actions", plugin_id, make_meta(plugin_id),
                              f"def run_with_context(meta, params, context):\n    from {prefix}helper import VALUE\n    from {prefix}lib import nested\n    return VALUE, nested.VALUE\n")
        (folder / "helper.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
        (folder / "lib").mkdir()
        (folder / "lib" / "__init__.py").write_text("from . import nested\n", encoding="utf-8")
        (folder / "lib" / "nested.py").write_text(f"VALUE = {value * 10}\n", encoding="utf-8")
    loaded, failed, _meta, funcs = load_actions(loader, tmp_path)
    assert (loaded, failed) == (2, 0)
    original_inspect = plugin_loader.inspect_plugin_tree

    def inspect_then_change(path):
        tree = original_inspect(path)
        (Path(path) / "action.py").write_text("raise RuntimeError('changed entry')\n", encoding="utf-8")
        (Path(path) / "helper.py").write_text("VALUE = 99\n", encoding="utf-8")
        return tree

    monkeypatch.setattr(plugin_loader, "inspect_plugin_tree", inspect_then_change)
    try:
        loader.materialize_pending("first")
        loader.materialize_pending("second")
        assert errors == []
        assert funcs["first"]({}, {}, {}) == (1, 10)
        assert funcs["second"]({}, {}, {}) == (2, 20)
        assert registry.plugin_roots["first"] not in sys.path
        first_names = set(registry.importers["first"].modules)
        registry.unregister("action", "first")
        assert first_names.isdisjoint(sys.modules)
        assert funcs["second"]({}, {}, {}) == (2, 20)
    finally:
        registry.clear()


def test_action_with_two_argument_context_entry_is_rejected(tmp_path):
    loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.PERMISSIVE)
    write_plugin(tmp_path / "actions", "plug_a", make_meta(), "def run_with_context(meta, params):\n    return None\n")
    assert load_actions(loader, tmp_path)[:2] == (1, 0)
    assert registry.resolve_action("plug_a") is None
    assert any("动作入口参数不符" in message for _, _, message in errors)
