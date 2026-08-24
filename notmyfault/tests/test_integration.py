"""配置、引擎、日志与首次构建的跨模块集成路径"""

import json
import os
import hashlib
import hmac
import threading
import time
from types import SimpleNamespace

import pytest

from notmyfault.core.diagnostics import Diagnostics
from notmyfault.core import logging as englog
from notmyfault.tests.api_support import (
    create_test_engine,
    make_paths,
    make_store,
)
from notmyfault.core.hot_reloader import RulesHotReloader
from notmyfault.host import app as host_app
from notmyfault.security.security import SecurityMode


def make_engine(rules=None, on_event=None):
    engine = create_test_engine({"rules": rules or []}, on_event=on_event)
    engine._alert_user = lambda *a, **k: None
    return engine


@pytest.fixture
def isolated_config(tmp_path):
    paths = make_paths(tmp_path)
    return SimpleNamespace(paths=paths, store=make_store(paths))


class TestAPIConfigRoundtrip:
    def test_api_read_modify_verify(self, isolated_config):
        store = isolated_config.store
        assert store.load_rules() == []

        assert store.save_rules([{
            "name": "集成规则",
            "event": {"type": "hotkey", "params": {"key": "f9"}},
            "actions": [{"type": "noop", "params": {}}],
        }]) is True

        names = [rule["name"] for rule in store.load_verified_rules()]
        assert "集成规则" in names


class TestConfigEnginePipeline:
    def test_config_written_engine_reads_rules(self, isolated_config):
        store = isolated_config.store
        store.save_config({})
        store.save_rules([{
            "name": "r1",
            "event": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }])
        config = store.load_verified_config()
        config["rules"] = store.load_verified_rules()
        engine = create_test_engine(config, rules_store=store)
        assert len(engine.rules) == 1
        assert engine.rules[0]["name"] == "r1"

    def test_config_modification_picked_up(self, isolated_config):
        store = isolated_config.store
        store.save_config({})
        store.save_rules([{
            "name": "旧规则",
            "event": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }])
        rules = store.load_verified_rules()
        rules[0]["name"] = "新规则"
        store.save_rules(rules)
        assert store.load_verified_rules()[0]["name"] == "新规则"

    def test_legacy_config_to_engine(self, isolated_config):
        # 旧格式 config.json 直接落盘，模拟升级前的真实文件
        legacy = {"processes": [{
            "process_name": "WeChat.exe",
            "software_name": "微信",
            "volume_action": "max",
            "notification": {"title": "微信运行", "message": "音量最大"},
        }]}
        paths = isolated_config.paths
        store = isolated_config.store
        secret = paths.config_secret_file.read_bytes()
        content = json.dumps(legacy, sort_keys=True, ensure_ascii=False, default=str)
        legacy["_signature"] = hmac.new(
            secret,
            content.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        paths.config_file.write_text(
            json.dumps(legacy, ensure_ascii=False),
            encoding="utf-8",
        )
        paths.rules_file.unlink()
        # 加载设置时自动把旧进程列表拆分到 rules.json
        config = store.load_config()
        config["rules"] = store.load_rules()
        engine = create_test_engine(config, rules_store=store)
        rule = engine.rules[0]
        assert rule["event"]["type"] == "process_state"
        assert rule["event"]["params"]["process_name"] == "WeChat.exe"
        assert rule["actions"][0]["type"] == "set_volume"

    def test_legacy_trigger_config_normalized(self, isolated_config):
        store = isolated_config.store
        store.save_config({})
        store.save_rules([{
            "name": "r1",
            "trigger": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }])
        rule = store.load_verified_rules()[0]
        assert "trigger" not in rule
        assert rule["event"]["type"] == "hotkey"


class TestEngineEventPipeline:
    def test_full_event_to_action_pipeline(self):
        events = []
        received = {}
        engine = make_engine(
            rules=[{
                "name": "USB 规则",
                "event": {"type": "usb_insert", "params": {}},
                "actions": [{
                    "type": "consume",
                    "binding_id": "a_con001",
                    "params": {
                        "drive": {"$ref": {"scope": "event", "path": ["drive"]}}
                    },
                }],
            }],
            on_event=lambda t, p: events.append((t, p)),
        )
        engine.triggers_meta["usb_insert"] = {"outputs": [{"name": "drive", "type": "string"}]}
        engine.actions_funcs["consume"] = lambda meta, params: received.update(params)
        engine.actions_meta["consume"] = {}

        engine.emit_event("usb_insert", {"drive": "E:"})
        assert engine._rule_scheduler.wait_for_idle(timeout=5)
        assert received == {"drive": "E:"}
        types = [t for t, _ in events]
        assert "rule_triggered" in types
        assert "action_executed" in types

    def test_shutdown_during_action(self):
        engine = make_engine(rules=[{
            "name": "r",
            "event": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }])
        ran = []
        engine.actions_funcs["noop"] = lambda meta, params: ran.append(1)
        engine.actions_meta["noop"] = {}
        engine._shutdown_flag = threading.Event()
        engine._shutdown_flag.set()
        engine.emit_event("hotkey", {})
        assert engine._rule_scheduler.wait_for_idle(timeout=5)
        assert ran == []


class TestHotReloadIntegration:
    def test_hot_reload_detects_timestamp_moving_backwards(self, monkeypatch):
        active_rules = [{"name": "旧规则"}]
        new_rules = [{"name": "新规则"}]
        alerts = []

        def apply_rules(rules):
            previous = list(active_rules)
            active_rules[:] = rules
            return previous

        reloader = RulesHotReloader(
            rules_path_fn=lambda: "rules.json",
            load_rules_fn=lambda: new_rules,
            stop_triggers_fn=lambda timeout: True,
            apply_rules_fn=apply_rules,
            cancel_deferred_fn=lambda: None,
            validate_rules_fn=lambda: None,
            start_triggers_fn=lambda rules: 1,
            recheck_admin_fn=lambda: None,
            diagnostics=Diagnostics(),
            alert_cb=lambda title, message: alerts.append((title, message)),
        )
        reloader._rules_mtime = 2.0
        monkeypatch.setattr(reloader, "current_mtime", lambda: 1.0)

        reloader.check_once()

        assert active_rules == new_rules
        assert reloader._rules_mtime == 1.0
        assert alerts == []

    def test_hot_reload_reports_restore_failure(self, monkeypatch):
        old_rules = [{"name": "旧规则"}]
        new_rules = [{"name": "新规则"}]
        active_rules = list(old_rules)
        alerts = []

        def apply_rules(rules):
            previous = list(active_rules)
            active_rules[:] = rules
            return previous

        def start_triggers(_rules):
            raise RuntimeError("触发器启动失败")

        reloader = RulesHotReloader(
            rules_path_fn=lambda: "rules.json",
            load_rules_fn=lambda: new_rules,
            stop_triggers_fn=lambda timeout: True,
            apply_rules_fn=apply_rules,
            cancel_deferred_fn=lambda: None,
            validate_rules_fn=lambda: None,
            start_triggers_fn=start_triggers,
            recheck_admin_fn=lambda: None,
            diagnostics=Diagnostics(),
            alert_cb=lambda title, message: alerts.append((title, message)),
        )
        reloader._rules_mtime = 1.0
        monkeypatch.setattr(reloader, "current_mtime", lambda: 2.0)

        reloader.check_once()

        assert active_rules == old_rules
        assert alerts == [
            ("规则恢复失败", "热加载失败且原有规则恢复失败，请重启引擎")
        ]

    def test_hot_reload_picks_up_changes(self, monkeypatch, isolated_config):
        from notmyfault.platform import platform_support
        monkeypatch.setattr(platform_support, "show_notification", lambda *a, **k: None)

        def rule(name):
            return {
                "name": name,
                "event": {"type": "hotkey", "params": {}},
                "actions": [{"type": "noop", "params": {}}],
            }

        store = isolated_config.store
        store.save_config({})
        store.save_rules([rule("r1")])
        config = store.load_verified_config()
        config["rules"] = store.load_verified_rules()
        engine = create_test_engine(config, rules_store=store)
        engine._alert_user = lambda *a, **k: None
        engine._security_mode = SecurityMode.PERMISSIVE
        engine.triggers_funcs["hotkey"] = lambda meta, config, emit, stop: stop.wait(30)
        engine.triggers_meta["hotkey"] = {}
        engine.actions_funcs["noop"] = lambda meta, params: None
        engine.actions_meta["noop"] = {}

        shutdown = threading.Event()
        thread = threading.Thread(target=engine.start, kwargs={"shutdown_event": shutdown})
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while "hotkey" not in engine._trigger_threads:
                if time.monotonic() > deadline:
                    raise AssertionError("触发器线程未启动")
                time.sleep(0.05)

            # 触发 mtime 变化后引擎应在轮询中应用新规则
            time.sleep(0.01)
            store.save_rules([rule("r1"), rule("r2")])
            deadline = time.monotonic() + 8
            while len(engine.rules) < 2:
                if time.monotonic() > deadline:
                    raise AssertionError("热重载未生效")
                time.sleep(0.1)
            assert [r["name"] for r in engine.rules] == ["r1", "r2"]
        finally:
            shutdown.set()
            thread.join(timeout=10)
        assert not thread.is_alive()

    def test_hot_reload_restores_old_rules_after_trigger_start_failure(self, monkeypatch):
        old_rules = [{"name": "旧规则"}]
        new_rules = [{"name": "新规则"}]
        active_rules = list(old_rules)
        stopped = []
        started = []
        alerts = []

        def apply_rules(rules):
            previous = list(active_rules)
            active_rules[:] = rules
            return previous

        def start_triggers(rules):
            started.append(list(rules))
            if rules == new_rules:
                raise RuntimeError("新触发器启动失败")
            return 1

        reloader = RulesHotReloader(
            rules_path_fn=lambda: "rules.json",
            load_rules_fn=lambda: new_rules,
            stop_triggers_fn=lambda timeout: stopped.append(timeout) or True,
            apply_rules_fn=apply_rules,
            cancel_deferred_fn=lambda: None,
            validate_rules_fn=lambda: None,
            start_triggers_fn=start_triggers,
            recheck_admin_fn=lambda: None,
            diagnostics=Diagnostics(),
            alert_cb=lambda title, message: alerts.append((title, message)),
        )
        reloader._rules_mtime = 1.0
        monkeypatch.setattr(reloader, "current_mtime", lambda: 2.0)

        reloader.check_once()

        assert active_rules == old_rules
        assert started == [new_rules, old_rules]
        assert len(stopped) == 2
        assert reloader._rules_mtime == 2.0
        assert alerts == [("热加载失败", "新规则未能启动，已尝试恢复原有规则")]


class TestLoggingPipeline:
    def test_write_and_parse_roundtrip(self, tmp_path, monkeypatch):
        log_path = englog.init_session_log(str(tmp_path))

        class TimestampRedirect:
            """模拟宿主 stdout 重定向：每行日志前补时间戳"""

            def __init__(self, fp):
                self.fp = fp

            def write(self, text):
                # print 会先写内容再单独写换行，换行符直接透传
                if text == "\n":
                    self.fp.write(text)
                elif text:
                    self.fp.write(f"[2025-01-01 10:00:00] {text}")
                return len(text)

            def flush(self):
                self.fp.flush()

        with open(log_path, "w", encoding="utf-8") as log_file:
            monkeypatch.setattr("sys.stdout", TimestampRedirect(log_file))
            englog.engine_info("装载Trigger: hotkey")
            englog.engine_error("plugin_load_failed", plugin="p1", reason="bad")

        entries = englog.read_log_entries(log_path)
        assert len(entries) == 2
        assert entries[0]["level"] == "INFO"
        assert entries[0]["text"] == "装载Trigger: hotkey"
        assert entries[1]["level"] == "ERROR"
        assert entries[1]["data"]["plugin"] == "p1"

    def test_diagnostics_from_real_logs(self, tmp_path):
        log_path = tmp_path / "engine-20250101-000000.log"
        payload = json.dumps(
            {"event": "plugin_load_failed", "plugin": "p1", "reason": "bad"},
            ensure_ascii=False,
        )
        log_path.write_text(
            "[2025-01-01 10:00:00] [INFO] 引擎启动\n"
            "[2025-01-01 10:00:01] [WARN] 签名缺失降级\n"
            f"[2025-01-01 10:00:02] [ERROR] {payload}\n",
            encoding="utf-8",
        )
        entries = englog.read_log_entries(str(log_path))
        diag = englog.build_diagnostics(entries)
        assert diag["error_count"] == 1
        assert diag["warn_count"] == 1
        assert diag["plugin_errors"] == [
            {"plugin": "p1", "type": "?", "reason": "bad"}
        ]


@pytest.fixture
def first_run_env(tmp_path, monkeypatch):
    """把首次构建指向空目录并截获子进程调用"""
    from notmyfault.host import alert

    pkg_root = tmp_path / "pkg"
    (pkg_root / "actions").mkdir(parents=True)
    (pkg_root / "triggers").mkdir(parents=True)
    monkeypatch.setattr(host_app, "_PKG_ROOT", str(pkg_root))
    monkeypatch.setattr(alert, "alert_user", lambda *a, **k: None)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(host_app._sp, "run", fake_run)
    return calls


def test_first_run_build_is_noninteractive(first_run_env):
    host_app._ensure_first_run_build()
    assert len(first_run_env) == 1
    call = first_run_env[0]
    assert "--security-mode=permissive" in call["cmd"]
    # 构建输出全部被捕获且不向子进程提供输入，GUI 启动不会卡在交互提示
    assert call["kwargs"].get("capture_output") is True
    assert call["kwargs"].get("input") is None


def test_first_run_build_uses_utf8_subprocess_io(first_run_env):
    host_app._ensure_first_run_build()
    kwargs = first_run_env[0]["kwargs"]
    assert kwargs.get("text") is True
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("errors") == "replace"
    assert kwargs["env"]["PYTHONUTF8"] == "1"
