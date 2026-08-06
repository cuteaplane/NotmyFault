"""引擎插件加载流水线、安全扫描和注册表行为"""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from notmyfault.core.engine import AutomationEngine
from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
from notmyfault.security.security import SecurityMode


def make_engine(rules=None):
    engine = AutomationEngine({"rules": rules or []})
    engine._alert_user = lambda *a, **k: None
    return engine


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
    def __init__(self):
        self.calls = []

    def authorize_plugin(self, plugin_id, token, module=None):
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
    )
    return loader, registry, plugin_errors, sudo


def load_actions(loader, tmp_path, meta_store=None, func_store=None, origin="builtin"):
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


def load_triggers(loader, tmp_path, meta_store=None, func_store=None, origin="builtin"):
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


def sign_with_test_key(plugin_dir, monkeypatch):
    """生成临时 Ed25519 密钥对并用它签名，公钥表同步替换"""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from notmyfault.security import signing, signing_keys

    key = ed25519.Ed25519PrivateKey.generate()
    public_bytes = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [public_bytes])
    signing.sign_plugin(Path(plugin_dir), "action.json", private_key=key)


CLEAN_RUN = "def run(meta, params):\n    return None\n"


class TestEngineStart:
    def test_engine_no_rules(self):
        engine = AutomationEngine({})
        assert engine.rules == []
        engine.emit_event("hotkey", {})

    def test_get_diagnostics_initial(self):
        engine = make_engine(rules=[{"name": "a"}])
        diag = engine.get_diagnostics()
        assert diag["uptime_seconds"] == 0
        assert diag["plugins"]["actions_loaded"] == 0
        assert diag["plugins"]["triggers_loaded"] == 0
        assert diag["rules"]["total"] == 1

    def test_start_no_trigger_threads_alerts(self, monkeypatch):
        from notmyfault.platform import platform_support
        monkeypatch.setattr(platform_support, "show_notification", lambda *a, **k: None)

        alerts = []
        engine = make_engine(rules=[{
            "name": "r",
            "event": {"type": "ghost_trigger", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }])
        engine._alert_user = (
            lambda title, message, open_dashboard=False: alerts.append((title, open_dashboard))
        )
        engine.start(shutdown_event=threading.Event())
        assert any("启动失败" in title and dashboard for title, dashboard in alerts)


class TestLoadPlugins:
    def test_plugin_dir_not_exists(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        assert load_actions(loader, tmp_path)[:2] == (0, 0)
        assert errors == []

    def test_missing_json_file(self, tmp_path, capsys):
        loader, registry, errors, _ = make_loader(tmp_path)
        (tmp_path / "actions" / "bare").mkdir(parents=True)
        assert load_actions(loader, tmp_path)[:2] == (0, 0)
        assert "缺少 action.json" in capsys.readouterr().err

    def test_json_parse_error(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        folder = tmp_path / "actions" / "bad"
        folder.mkdir(parents=True)
        (folder / "action.json").write_text("{bad", encoding="utf-8")
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert any("JSON 解析失败" in msg for _, _, msg in errors)

    def test_schema_validation_failure(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        meta = make_meta()
        del meta["name"]
        write_plugin(tmp_path / "actions", "bad", meta, CLEAN_RUN)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert any("schema 校验失败" in msg for _, _, msg in errors)

    def test_missing_py_file(self, tmp_path, capsys):
        loader, registry, errors, _ = make_loader(tmp_path)
        folder = tmp_path / "actions" / "no_py"
        folder.mkdir(parents=True)
        (folder / "action.json").write_text(
            json.dumps(make_meta(), ensure_ascii=False), encoding="utf-8"
        )
        assert load_actions(loader, tmp_path)[:2] == (0, 0)
        assert "缺少 action.py" in capsys.readouterr().err

    def test_missing_run_function(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        write_plugin(tmp_path / "actions", "no_run", make_meta(), "x = 1\n")
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert any("缺少 run() 函数" in msg for _, _, msg in errors)

    def test_python_import_error(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        write_plugin(
            tmp_path / "actions", "broken", make_meta(),
            "raise ImportError('依赖缺失')\n",
        )
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert any("Python 加载异常" in msg for _, _, msg in errors)

    def test_plugin_disabled(self, tmp_path, capsys):
        loader, registry, errors, _ = make_loader(tmp_path)
        write_plugin(
            tmp_path / "actions", "off", make_meta(enabled=False), CLEAN_RUN
        )
        loaded, failed, meta_store, func_store = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 0)
        assert meta_store == {}
        assert "已禁用" in capsys.readouterr().out

    def test_successful_plugin_load(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        write_plugin(tmp_path / "actions", "good", make_meta(), CLEAN_RUN)
        loaded, failed, meta_store, func_store = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert callable(func_store["plug_a"])
        assert meta_store["plug_a"]["origin"] == "builtin"
        assert registry.get_module("plug_a") is not None
        assert errors == []

    def test_multiple_plugins_mixed(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        write_plugin(tmp_path / "actions", "aaa_good", make_meta("aaa_good"), CLEAN_RUN)
        write_plugin(
            tmp_path / "actions", "bbb_broken", make_meta("bbb_broken"),
            "raise RuntimeError('boom')\n",
        )
        loaded, failed, meta_store, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 1)
        assert "aaa_good" in meta_store
        assert "bbb_broken" not in meta_store

    @pytest.mark.parametrize(
        "folder_name",
        [".hidden", ".pytest_cache", "__pycache__", "__pypackages__", "node_modules"],
    )
    def test_generated_directories_are_silently_ignored(self, tmp_path, folder_name):
        loader, registry, errors, _ = make_loader(tmp_path)
        write_plugin(tmp_path / "actions" / folder_name, "inner", make_meta(), CLEAN_RUN)
        loaded, failed, meta_store, func_store = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 0)
        assert meta_store == {} and func_store == {}
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
        assert (loaded, failed) == (0, 1)
        assert "bad_setup" not in func_store
        assert "bad_setup" not in meta_store
        assert registry.get_module("bad_setup") is None

    def test_setup_returns_false(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        code = (
            "def setup(meta):\n"
            "    return False\n"
            "def run(meta, config, emit, stop_event):\n"
            "    pass\n"
        )
        write_plugin(
            tmp_path / "triggers", "no_setup", make_meta("no_setup"),
            code, json_name="trigger.json", py_name="trigger.py",
        )
        loaded, failed, meta_store, func_store = load_triggers(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert "no_setup" not in func_store
        assert any("setup() 返回 False" in msg for _, _, msg in errors)

    def test_sudo_import_warning_no_admin_permission(self, tmp_path, capsys):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.NORMAL)
        code = (
            "import notmyfault.security.sudo\n"
            "def run(meta, params):\n"
            "    return None\n"
        )
        write_plugin(tmp_path / "actions", "warned", make_meta("warned"), code)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        # normal 模式只警告，不拒载
        assert (loaded, failed) == (1, 0)
        assert "未在元数据中声明" in capsys.readouterr().err

    def test_override_plugin_does_not_inherit_admin(self, tmp_path, monkeypatch):
        from notmyfault.security import plugins as security_plugins
        monkeypatch.setattr(
            security_plugins, "_PLUGIN_MANIFEST_FILE", str(tmp_path / "manifest.json")
        )
        loader, registry, errors, sudo = make_loader(tmp_path, mode=SecurityMode.NORMAL)
        meta_store, func_store = {}, {}

        admin_code = (
            "from notmyfault.security import sudo\n"
            "def run(meta, params):\n"
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
        assert sudo.calls == [("authorize", "adminp")]

        # 用户覆盖版没有声明 admin，旧授权必须被撤销且不再重新授权
        write_plugin(tmp_path / "actions", "adminp", make_meta("adminp"), CLEAN_RUN)
        loaded, failed, meta_store, func_store = load_actions(
            loader, tmp_path, meta_store, func_store, origin="user"
        )
        assert (loaded, failed) == (1, 0)
        assert sudo.calls == [("authorize", "adminp"), ("deauthorize", "adminp")]
        assert meta_store["adminp"]["origin"] == "user"


class TestSecurityScanIntegration:
    def test_builtin_plugin_does_not_use_user_hash_cache(self, tmp_path, monkeypatch):
        from notmyfault.security import plugins as security_plugins
        manifest_path = tmp_path / "manifest.json"
        monkeypatch.setattr(
            security_plugins, "_PLUGIN_MANIFEST_FILE", str(manifest_path)
        )
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.NORMAL)
        write_plugin(tmp_path / "actions", "good", make_meta(), CLEAN_RUN)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        # builtin 插件依赖构建签名，不写入用户哈希清单
        assert (loaded, failed) == (1, 0)
        assert not manifest_path.exists()

    def test_event_v1_trigger_with_wrong_signature_is_rejected(self, tmp_path):
        loader, registry, errors, _ = make_loader(tmp_path)
        code = "def run(meta, config):\n    pass\n"
        write_plugin(
            tmp_path / "triggers", "badapi",
            make_meta("badapi", trigger_api="event-v1"),
            code, json_name="trigger.json", py_name="trigger.py",
        )
        loaded, failed, _, _ = load_triggers(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert any("event-v1 入口不兼容" in msg for _, _, msg in errors)

    def test_permissive_warns_but_loads(self, tmp_path, capsys):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.PERMISSIVE)
        code = "import subprocess\ndef run(meta, params):\n    return None\n"
        write_plugin(tmp_path / "actions", "wild", make_meta("wild"), code)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert "未在清单声明" in capsys.readouterr().err

    def test_strict_allows_declared_subprocess(self, tmp_path, monkeypatch):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        code = "import subprocess\ndef run(meta, params):\n    return None\n"
        folder = write_plugin(
            tmp_path / "actions", "declared",
            make_meta("declared", permissions=["external_binary"]), code,
        )
        sign_with_test_key(folder, monkeypatch)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (1, 0)
        assert errors == []

    def test_strict_rejects_undeclared_subprocess(self, tmp_path, monkeypatch):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        code = "import subprocess\ndef run(meta, params):\n    return None\n"
        folder = write_plugin(tmp_path / "actions", "sneaky", make_meta("sneaky"), code)
        sign_with_test_key(folder, monkeypatch)
        loaded, failed, _, func_store = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        assert "sneaky" not in func_store
        assert any("未声明能力" in msg for _, _, msg in errors)

    def test_strict_rejects_undeclared_os_alias(self, tmp_path, monkeypatch):
        loader, registry, errors, _ = make_loader(tmp_path, mode=SecurityMode.STRICT)
        code = (
            "import os as o\n"
            "def run(meta, params):\n"
            "    return o.system('dir')\n"
        )
        folder = write_plugin(tmp_path / "actions", "aliased", make_meta("aliased"), code)
        sign_with_test_key(folder, monkeypatch)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
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
            "def run(meta, params):\n"
            "    return None\n"
        )
        folder = write_plugin(tmp_path / "actions", "tampered", make_meta("tampered"), code)
        (folder / "signature.sig").write_bytes(b"\x00" * 64)
        loaded, failed, _, _ = load_actions(loader, tmp_path)
        assert (loaded, failed) == (0, 1)
        # 验签失败必须发生在模块导入之前
        assert not marker.exists()
        assert any("签名无效" in msg for _, _, msg in errors)


def test_engine_keeps_plugin_loader_compatibility_exports():
    from notmyfault.core import engine as engine_module

    for name in (
        "PluginKind",
        "PluginLoader",
        "PluginRegistry",
        "check_permissions_conform",
        "check_sudo_import",
        "is_known_permission",
        "scan_plugin_capabilities",
        "validate_plugin_meta",
        "verify_plugin_integrity",
        "verify_plugin_sig",
        "_check_sudo_import",
        "_validate_plugin_meta",
    ):
        assert hasattr(engine_module, name), f"engine 缺少兼容导出 {name}"


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
