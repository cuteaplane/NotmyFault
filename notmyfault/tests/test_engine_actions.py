"""引擎动作执行、错误隔离、诊断与关闭行为"""

import importlib.util
import os
import threading
import time
import types

import pytest

from notmyfault.core.engine import AutomationEngine
from notmyfault.core.workflow import build_context


def make_engine(rules=None, on_event=None):
    engine = AutomationEngine({"rules": rules or []}, on_event=on_event)
    engine._alert_user = lambda *a, **k: None
    return engine


def register_action(engine, action_type, func, meta=None, module=None):
    engine.actions_funcs[action_type] = func
    engine.actions_meta[action_type] = meta or {}
    if module is not None:
        engine._plugin_modules[action_type] = module


def _context():
    return build_context("规则", "hotkey", {}, [])


class TestExecuteAction:
    def test_execute_valid_action(self):
        engine = make_engine()
        register_action(engine, "noop", lambda meta, params: {"ok": True})
        ok, result = engine._run_action({"type": "noop", "params": {}}, "规则", _context())
        assert ok is True
        assert result == {"ok": True}

    def test_execute_unknown_action(self):
        engine = make_engine()
        ok, result = engine._run_action({"type": "ghost", "params": {}}, "规则", _context())
        assert ok is False
        assert "ghost" in result

    def test_execute_during_shutdown(self):
        engine = make_engine()
        register_action(engine, "noop", lambda meta, params: None)
        engine._shutdown_flag = threading.Event()
        engine._shutdown_flag.set()
        ok, result = engine._run_action({"type": "noop", "params": {}}, "规则", _context())
        assert ok is False
        assert result == "引擎正在关闭"

    def test_action_success_tracking(self):
        engine = make_engine()
        register_action(engine, "noop", lambda meta, params: None)
        engine._run_action({"type": "noop", "params": {}}, "规则", _context())
        assert engine._diag_obj.data["action_ok"] == 1
        assert engine._diag_obj.data["action_fail"] == 0

    def test_action_failure_tracking(self):
        engine = make_engine()

        def boom(meta, params):
            raise RuntimeError("炸了")

        register_action(engine, "boom", boom)
        ok, _ = engine._run_action({"type": "boom", "params": {}}, "规则", _context())
        assert ok is False
        assert engine._diag_obj.data["action_fail"] == 1

    def test_active_actions_counter(self):
        engine = make_engine()
        release = threading.Event()
        started = threading.Event()

        def slow(meta, params):
            started.set()
            release.wait(timeout=5)

        register_action(engine, "slow", slow)
        thread = threading.Thread(
            target=engine._run_action,
            args=({"type": "slow", "params": {}}, "规则", _context()),
        )
        thread.start()
        started.wait(timeout=5)
        assert engine._active_actions == 1
        release.set()
        thread.join(timeout=5)
        assert engine._active_actions == 0

    def test_on_event_success_callback(self):
        events = []
        engine = make_engine(on_event=lambda t, p: events.append((t, p)))
        register_action(engine, "noop", lambda meta, params: "done")
        engine._run_action({"type": "noop", "params": {"x": 1}}, "规则A", _context())
        executed = [p for t, p in events if t == "action_executed"]
        assert len(executed) == 1
        assert executed[0]["status"] == "ok"
        assert executed[0]["result"] == "done"
        assert executed[0]["rule_name"] == "规则A"

    def test_on_event_error_callback(self):
        events = []
        engine = make_engine(on_event=lambda t, p: events.append((t, p)))

        def boom(meta, params):
            raise RuntimeError("炸了")

        register_action(engine, "boom", boom)
        engine._run_action({"type": "boom", "params": {}}, "规则A", _context())
        assert any(t == "error" for t, _ in events)

    def test_pipeline_passes_step_result_to_context_action(self):
        engine = make_engine()
        received = {}
        register_action(engine, "producer", lambda meta, params: {"url": "http://x"})
        register_action(
            engine,
            "consumer",
            lambda meta, params: received.update(params),
        )
        rule = {
            "actions": [
                {"type": "producer", "binding_id": "a_pro001", "params": {}},
                {
                    "type": "consumer",
                    "binding_id": "a_con001",
                    "params": {
                        "url": {"$ref": {"scope": "step", "node": "a_pro001", "path": ["url"]}}
                    },
                },
            ]
        }
        context = _context()
        engine.execute_workflow("wf", rule, "规则", context)
        assert received == {"url": "http://x"}
        assert context["steps"]["a_pro001"]["status"] == "ok"

    def test_pipeline_resolves_structured_trigger_reference(self):
        engine = make_engine()
        received = {}
        register_action(
            engine, "consumer", lambda meta, params: received.update(params)
        )
        rule = {
            "actions": [
                {
                    "type": "consumer",
                    "binding_id": "a_con001",
                    "params": {
                        "drive": {"$ref": {"scope": "trigger", "node": "t_usb001", "path": ["drive"]}}
                    },
                }
            ]
        }
        context = _context()
        context["triggers"]["t_usb001"] = {
            "type": "usb_insert",
            "payload": {"drive": "E:"},
            "config": {},
        }
        engine.execute_workflow("wf", rule, "规则", context)
        assert received == {"drive": "E:"}

    def test_pipeline_retries_and_stops_after_failure(self):
        engine = make_engine()
        attempts = []

        def flaky(meta, params):
            attempts.append(1)
            raise RuntimeError("boom")

        after = []
        register_action(engine, "flaky", flaky)
        register_action(engine, "after", lambda meta, params: after.append(1))
        rule = {
            "actions": [
                {"type": "flaky", "binding_id": "a_fla001", "params": {}, "retry": 2},
                {"type": "after", "binding_id": "a_aft001", "params": {}},
            ]
        }
        engine.execute_workflow("wf", rule, "规则", _context())
        assert len(attempts) == 3
        # 默认 on_error=stop，后续动作不再执行
        assert after == []

    def test_precondition_allows_ready_workflow(self):
        engine = make_engine()
        module = types.SimpleNamespace(
            check_precondition=lambda meta, params, context: True
        )
        register_action(
            engine,
            "doc_check",
            lambda meta, params: None,
            meta={"precondition_api": "context-v1"},
            module=module,
        )
        ran = []
        register_action(engine, "real", lambda meta, params: ran.append(1))
        rule = {
            "preconditions": [{"type": "doc_check", "binding_id": "p_doc001", "params": {}}],
            "actions": [{"type": "real", "binding_id": "a_rea001", "params": {}}],
        }
        engine.execute_workflow("wf", rule, "规则", _context())
        assert ran == [1]

    def test_precondition_defers_workflow_without_running_actions(self):
        events = []
        engine = make_engine(on_event=lambda t, p: events.append((t, p)))
        module = types.SimpleNamespace(
            check_precondition=lambda meta, params, context: {
                "ok": False,
                "reason": "目录仍在使用",
                "retry_after_seconds": 10,
            }
        )
        register_action(
            engine,
            "doc_check",
            lambda meta, params: None,
            meta={"precondition_api": "context-v1"},
            module=module,
        )
        ran = []
        register_action(engine, "real", lambda meta, params: ran.append(1))
        rule = {
            "preconditions": [{"type": "doc_check", "binding_id": "p_doc001", "params": {}}],
            "actions": [{"type": "real", "binding_id": "a_rea001", "params": {}}],
        }
        engine.execute_workflow("wf", rule, "规则", _context())
        assert ran == []
        assert "wf" in engine._deferred_workflows
        assert any(t == "workflow_deferred" for t, _ in events)
        engine._cancel_deferred_workflows()

    def test_validate_params_called(self):
        engine = make_engine()
        seen = []
        module = types.SimpleNamespace(
            validate_params=lambda meta, params: seen.append((meta, params)) or []
        )
        register_action(
            engine, "checked", lambda meta, params: None, meta={"params": []}, module=module
        )
        engine._run_action({"type": "checked", "params": {"a": 1}}, "规则", _context())
        assert len(seen) == 1
        assert seen[0][1] == {"a": 1}

    def test_validate_params_not_present(self):
        engine = make_engine()
        module = types.SimpleNamespace()
        register_action(engine, "plain", lambda meta, params: "ok", module=module)
        ok, result = engine._run_action({"type": "plain", "params": {}}, "规则", _context())
        assert ok and result == "ok"

    def test_validate_params_warnings_printed(self, capsys):
        engine = make_engine()
        module = types.SimpleNamespace(
            validate_params=lambda meta, params: ["参数不对"]
        )
        register_action(engine, "warned", lambda meta, params: None, module=module)
        engine._run_action({"type": "warned", "params": {}}, "规则", _context())
        err = capsys.readouterr().err
        assert "参数不对" in err

    def test_validate_params_exception_handled(self):
        engine = make_engine()

        def broken(meta, params):
            raise RuntimeError("validate 炸了")

        module = types.SimpleNamespace(validate_params=broken)
        register_action(engine, "warned", lambda meta, params: "done", module=module)
        ok, result = engine._run_action({"type": "warned", "params": {}}, "规则", _context())
        assert ok and result == "done"

    def test_document_quiescent_requires_observation_then_allows(self, tmp_path):
        doc_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "actions", "document_quiescent", "action.py",
        )
        spec = importlib.util.spec_from_file_location("doc_quiescent_under_test", doc_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod._observations.clear()

        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        params = {
            "source_folder": str(tmp_path),
            "quiet_seconds": 0,
            "check_file_locks": False,
            "check_document_windows": False,
        }
        # 首次检查只能建立观察基线，不允许直接放行
        first = mod.check_precondition({}, params, {})
        assert first["ok"] is False
        second = mod.check_precondition({}, params, {})
        assert second == {"ok": True}
        mod._observations.clear()


class TestErrorIsolation:
    def test_run_trigger_isolates_crash(self):
        engine = make_engine()
        alerts = []
        engine._alert_user = lambda title, message, open_dashboard=False: alerts.append(title)

        def broken(meta, config, emit, stop_event):
            raise RuntimeError("触发器炸了")

        engine._run_trigger("hotkey", "hotkey", broken, {}, {}, threading.Event())
        assert engine._diag_obj.data["trigger_crashes"] == 1
        assert len(alerts) == 1

    def test_safe_on_event_isolates_raising_callback(self):
        engine = make_engine(on_event=lambda t, p: (_ for _ in ()).throw(RuntimeError("回调炸了")))
        engine._safe_on_event("rule_triggered", {})

    def test_safe_on_event_no_callback_is_noop(self):
        engine = make_engine(on_event=None)
        engine._safe_on_event("rule_triggered", {"x": 1})

    def test_stop_trigger_threads_keeps_unstoppable_thread_registered(self):
        engine = make_engine()
        hold = threading.Event()

        def stubborn(meta, config, emit, stop_event):
            hold.wait(timeout=10)

        engine.triggers_funcs["hotkey"] = stubborn
        engine.triggers_meta["hotkey"] = {}
        engine._start_trigger_threads([{"event": {"type": "hotkey", "params": {}}}])
        assert engine._stop_trigger_threads(timeout=0.1) is False
        assert "hotkey" in engine._trigger_threads
        hold.set()
        engine._stop_trigger_threads(timeout=5)


class TestGetDiagnostics:
    def test_initial_diagnostics(self):
        engine = make_engine(rules=[{"name": "a"}])
        diag = engine.get_diagnostics()
        assert diag["uptime_seconds"] == 0
        assert diag["actions"] == {"ok": 0, "fail": 0, "total": 0}
        assert diag["rules"]["total"] == 1
        assert diag["trigger_crashes"] == 0
        assert diag["errors"] == []

    def test_uptime_calculated(self):
        engine = make_engine()
        engine._start_time = time.time() - 5
        assert engine.get_diagnostics()["uptime_seconds"] >= 4.5

    def test_diagnostics_after_action(self):
        engine = make_engine()
        register_action(engine, "noop", lambda meta, params: None)
        engine._run_action({"type": "noop", "params": {}}, "规则", _context())
        diag = engine.get_diagnostics()
        assert diag["actions"]["ok"] == 1
        assert diag["actions"]["total"] == 1

    def test_diagnostics_truncates_errors(self):
        engine = make_engine()
        for i in range(30):
            engine._diag_obj.record_error("test", f"错误{i}")
        diag = engine.get_diagnostics()
        assert len(diag["errors"]) == 20
        assert diag["errors"][-1][1] == "错误29"


class TestShutdown:
    def test_shutdown_sets_flag(self):
        engine = make_engine()
        flag = threading.Event()
        engine._shutdown_flag = flag
        engine.shutdown()
        assert flag.is_set()

    def test_shutdown_no_plugins(self):
        engine = make_engine()
        engine.shutdown()

    def test_shutdown_calls_teardowns(self):
        engine = make_engine()
        calls = []
        engine._plugin_modules["plug"] = types.SimpleNamespace(teardown=lambda: calls.append(1))
        engine.shutdown()
        assert calls == [1]

    def test_teardown_exception_handling(self):
        engine = make_engine()

        def broken_teardown():
            raise RuntimeError("teardown 炸了")

        engine._plugin_modules["plug"] = types.SimpleNamespace(teardown=broken_teardown)
        engine.shutdown()
        errors = engine._diag_obj.data["plugin_errors"]
        assert any(store == "teardown" for store, _, _ in errors)

    def test_shutdown_idempotent(self):
        engine = make_engine()
        calls = []
        engine._plugin_modules["plug"] = types.SimpleNamespace(teardown=lambda: calls.append(1))
        engine.shutdown()
        engine.shutdown()
        assert calls == [1]

    def test_concurrent_shutdown_tears_down_plugin_once(self):
        engine = make_engine()
        calls = []
        lock = threading.Lock()

        def teardown():
            with lock:
                calls.append(1)
            time.sleep(0.05)

        engine._plugin_modules["plug"] = types.SimpleNamespace(teardown=teardown)
        threads = [threading.Thread(target=engine._shutdown_plugins) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert calls == [1]

    def test_start_revokes_privilege_session_when_runtime_fails(self, monkeypatch):
        from notmyfault.platform import platform_support
        monkeypatch.setattr(platform_support, "show_notification", lambda *a, **k: None)
        engine = make_engine()
        # 没有任何触发器时 _run 直接返回，start() 仍应撤销权限会话
        engine.start(shutdown_event=threading.Event())
        assert engine._privilege_session_closed is True

    def test_wait_active_actions_completes(self):
        engine = make_engine()
        release = threading.Event()
        started = threading.Event()

        def slow(meta, params):
            started.set()
            release.wait(timeout=5)

        register_action(engine, "slow", slow)
        thread = threading.Thread(
            target=engine._run_action,
            args=({"type": "slow", "params": {}}, "规则", _context()),
        )
        thread.start()
        started.wait(timeout=5)
        release.set()
        assert engine._wait_active_actions(timeout=5) is True
        thread.join(timeout=5)


def _load_display_module(monkeypatch=None):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "actions", "display_control", "action.py",
    )
    spec = importlib.util.spec_from_file_location("display_control_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_display_brightness_accepts_one_verified_backend(monkeypatch):
    mod = _load_display_module()
    monkeypatch.setattr(mod, "_set_wmi_brightness", lambda level: 2)

    def broken(level):
        raise RuntimeError("DDC 不可用")

    monkeypatch.setattr(mod, "_set_ddc_brightness", broken)
    result = mod._set_brightness(50)
    assert result["brightness"] == 50
    assert result["methods"] == ["WMI 2 台"]
    assert result["warnings"] == ["DDC/CI: DDC 不可用"]


def test_display_brightness_fails_without_verified_backend(monkeypatch):
    mod = _load_display_module()

    def broken(level):
        raise RuntimeError("不支持")

    monkeypatch.setattr(mod, "_set_wmi_brightness", broken)
    monkeypatch.setattr(mod, "_set_ddc_brightness", broken)
    with pytest.raises(RuntimeError) as excinfo:
        mod._set_brightness(50)
    assert "不支持可验证的亮度控制" in str(excinfo.value)


def test_display_action_propagates_brightness_failure(monkeypatch):
    mod = _load_display_module()

    def broken(level):
        raise RuntimeError("不支持")

    monkeypatch.setattr(mod, "_set_wmi_brightness", broken)
    monkeypatch.setattr(mod, "_set_ddc_brightness", broken)
    with pytest.raises(RuntimeError):
        mod.run({}, {"action": "set_brightness", "brightness": 40})


def test_excel_append_rows_consumes_pipeline_output():
    engine = make_engine()
    appended = {}
    register_action(
        engine, "collect_rows", lambda meta, params: {"rows": [["a", 1], ["b", 2]]}
    )
    register_action(
        engine,
        "excel_append_rows",
        lambda meta, params: appended.update(params),
    )
    rule = {
        "actions": [
            {"type": "collect_rows", "binding_id": "a_col001", "params": {}},
            {
                "type": "excel_append_rows",
                "binding_id": "a_exc001",
                "params": {
                    "rows": {"$ref": {"scope": "step", "node": "a_col001", "path": ["rows"]}}
                },
            },
        ]
    }
    engine.execute_workflow("wf", rule, "规则", _context())
    assert appended["rows"] == [["a", 1], ["b", 2]]


def test_tianyi_upload_emits_only_new_files():
    engine = make_engine()
    uploaded = {}
    register_action(
        engine,
        "folder_diff",
        lambda meta, params: {"new_files": ["doc1.txt"]},
    )
    register_action(
        engine,
        "tianyi_upload",
        lambda meta, params: uploaded.update(params),
    )
    rule = {
        "actions": [
            {"type": "folder_diff", "binding_id": "a_dif001", "params": {}},
            {
                "type": "tianyi_upload",
                "binding_id": "a_tia001",
                "params": {
                    "files": {
                        "$ref": {"scope": "step", "node": "a_dif001", "path": ["new_files"]}
                    }
                },
            },
        ]
    }
    engine.execute_workflow("wf", rule, "规则", _context())
    # 上传动作只拿到增量文件列表
    assert uploaded["files"] == ["doc1.txt"]
