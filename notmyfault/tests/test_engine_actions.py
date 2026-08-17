"""引擎动作执行、错误隔离、诊断与关闭行为"""

import importlib.util
import os
import threading
import time
import types

import pytest

from notmyfault.core.engine import AutomationEngine
from notmyfault.core import workflow_executor as workflow_executor_module
from notmyfault.core.workflow import ActionCancellation, build_context
from notmyfault.security.errors import AdminExecutionBlocked


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

    def test_launch_program_rejects_dynamic_path(self):
        engine = make_engine()
        register_action(engine, "launch_program", lambda meta, params: None)
        ok, result = engine._run_action(
            {
                "type": "launch_program",
                "params": {
                    "path": {
                        "$ref": {"scope": "event", "path": ["program"]}
                    }
                },
            },
            "规则",
            _context(),
        )
        assert ok is False
        assert result == "程序路径不允许来自运行时数据"

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

    def test_action_event_carries_stable_rule_and_step_ids(self):
        events = []
        engine = make_engine(on_event=lambda t, p: events.append((t, p)))
        register_action(engine, "noop", lambda meta, params: "done")
        context = build_context("规则A", "hotkey", {}, [], "r_rule001")

        engine._run_action(
            {"type": "noop", "binding_id": "a_step001", "params": {}},
            "规则A",
            context,
        )

        executed = [payload for name, payload in events if name == "action_executed"]
        assert executed[0]["rule_id"] == "r_rule001"
        assert executed[0]["step_id"] == "a_step001"

    def test_workflow_events_carry_run_status_and_duration(self):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        register_action(engine, "noop", lambda meta, params: "done")
        context = build_context(
            "规则A", "manual", {}, [], "r_rule001", "run_test001"
        )

        engine.execute_workflow(
            "r_rule001",
            {
                "actions": [
                    {
                        "type": "noop",
                        "binding_id": "a_step001",
                        "params": {},
                    }
                ]
            },
            "规则A",
            context,
        )

        executed = [data for name, data in events if name == "action_executed"]
        completed = [data for name, data in events if name == "workflow_completed"]
        assert executed[0]["run_id"] == "run_test001"
        assert executed[0]["duration_ms"] >= 0
        assert len(completed) == 1
        assert completed[0]["run_id"] == "run_test001"
        assert completed[0]["rule_id"] == "r_rule001"
        assert completed[0]["rule_name"] == "规则A"
        assert completed[0]["status"] == "succeeded"

    def test_manual_run_returns_the_same_run_id_used_by_events(self):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        register_action(engine, "noop", lambda meta, params: "done")

        ok, message, run_id = engine.run_manual_rule_snapshot(
            {
                "rule_id": "r_rule001",
                "name": "规则A",
                "actions": [
                    {
                        "type": "noop",
                        "binding_id": "a_step001",
                        "params": {},
                    }
                ],
            },
            0,
        )
        for thread in engine._manual_threads:
            thread.join(timeout=5)

        assert ok is True
        assert message == "已开始执行"
        assert run_id.startswith("run_")
        run_events = [data for _name, data in events if data.get("run_id") == run_id]
        assert {data.get("rule_id") for data in run_events} == {"r_rule001"}
        assert any(name == "workflow_completed" for name, data in events if data.get("run_id") == run_id)

    def test_manual_run_can_continue_from_step_with_supplied_upstream_result(self):
        events = []
        received = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        register_action(engine, "first", lambda meta, params: pytest.fail("skipped action ran"))
        register_action(
            engine,
            "second",
            lambda meta, params: received.append(params) or {"count": 2},
        )
        rule = {
            "rule_id": "r_partial001",
            "name": "局部运行",
            "actions": [
                {"type": "first", "binding_id": "a_first001", "params": {}},
                {
                    "type": "second",
                    "binding_id": "a_second001",
                    "params": {
                        "value": {
                            "$ref": {
                                "scope": "step",
                                "node": "a_first001",
                                "path": ["value"],
                            }
                        }
                    },
                },
            ],
        }

        ok, _message, run_id = engine.run_manual_rule_snapshot(
            rule,
            0,
            step_outputs={"a_first001": {"value": "provided"}},
            start_step_id="a_second001",
            test_assertions=[{
                "step_id": "a_second001",
                "path": ["count"],
                "operator": "gte",
                "expected": 2,
            }],
        )
        for thread in engine._manual_threads:
            thread.join(timeout=5)

        assert ok is True
        assert received == [{"value": "provided"}]
        triggered = [data for name, data in events if name == "rule_triggered"]
        assert triggered[0]["action_count"] == 1
        assertion_event = [data for name, data in events if name == "test_assertions_completed"]
        assert assertion_event[0]["passed"] == 1
        completed = [data for name, data in events if name == "workflow_completed"]
        assert completed[0]["run_id"] == run_id
        assert completed[0]["status"] == "succeeded"

    def test_manual_run_exposes_trigger_configuration_without_fake_payload(self):
        received = []
        engine = make_engine()
        register_action(engine, "consume", lambda meta, params: received.append(params))
        rule = {
            "rule_id": "r_config001",
            "name": "触发配置测试",
            "event": {
                "type": "hotkey",
                "binding_id": "t_hotkey001",
                "params": {"hotkey": "ctrl+alt+t"},
            },
            "actions": [{
                "type": "consume",
                "binding_id": "a_consume001",
                "params": {
                    "value": {
                        "$ref": {
                            "scope": "trigger_config",
                            "node": "t_hotkey001",
                            "path": ["hotkey"],
                        }
                    }
                },
            }],
        }

        engine.run_manual_rule_snapshot(rule, 0)
        for thread in engine._manual_threads:
            thread.join(timeout=5)

        assert received == [{"value": "ctrl+alt+t"}]

    def test_failed_manual_assertion_marks_workflow_failed_without_exposing_values(self):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        register_action(engine, "result", lambda meta, params: {"token": "actual-secret"})
        context = build_context(
            "结果检查测试", "manual", {}, [], "r_assert001", "run_assert001"
        )
        context["manual_test"] = {
            "start_index": 0,
            "end_index": 0,
            "assertions": [{
                "step_id": "a_result001",
                "path": ["token"],
                "operator": "equals",
                "expected": "expected-secret",
            }],
        }

        engine.execute_workflow(
            "r_assert001",
            {"actions": [{"type": "result", "binding_id": "a_result001", "params": {}}]},
            "结果检查测试",
            context,
        )

        assertion_event = [data for name, data in events if name == "test_assertions_completed"][0]
        completed = [data for name, data in events if name == "workflow_completed"][0]
        assert assertion_event["passed"] == 0
        assert "actual-secret" not in repr(assertion_event)
        assert "expected-secret" not in repr(assertion_event)
        assert completed["status"] == "failed"
        assert completed["failure_kind"] == "assertion"

    def test_contains_assertion_handles_text_lists_and_object_subsets(self):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        register_action(
            engine,
            "result",
            lambda meta, params: {
                "text": "NotmyFault automation",
                "items": ["one", "two", "three"],
                "meta": {"ready": True, "count": 3},
            },
        )
        context = build_context("包含检查", "manual", {}, [], "r_contains001", "run_contains001")
        context["manual_test"] = {
            "start_index": 0,
            "end_index": 0,
            "assertions": [
                {"step_id": "a_result001", "path": ["text"], "operator": "contains", "expected": "automation"},
                {"step_id": "a_result001", "path": ["items"], "operator": "contains", "expected": ["one", "three"]},
                {"step_id": "a_result001", "path": ["meta"], "operator": "contains", "expected": {"ready": True}},
            ],
        }

        engine.execute_workflow(
            "r_contains001",
            {"actions": [{"type": "result", "binding_id": "a_result001", "params": {}}]},
            "包含检查",
            context,
        )

        assertion_event = [data for name, data in events if name == "test_assertions_completed"][0]
        assert assertion_event["passed"] == 3
        assert assertion_event["total"] == 3

    def test_sensitive_action_result_is_masked_in_event(self):
        events = []
        engine = make_engine(on_event=lambda t, p: events.append((t, p)))
        register_action(
            engine,
            "secret",
            lambda meta, params: {"token": "TOP_SECRET", "ok": True},
            meta={
                "outputs": [
                    {"name": "token", "type": "string", "sensitive": True},
                    {"name": "ok", "type": "bool"},
                ],
            },
        )

        ok, result = engine._run_action(
            {"type": "secret", "params": {}}, "规则A", _context()
        )

        assert ok is True
        assert result["token"] == "TOP_SECRET"
        executed = [p for t, p in events if t == "action_executed"]
        assert executed[0]["result"] == {"token": "***", "ok": True}

    def test_action_event_contains_plugin_controlled_safe_summaries(self):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        register_action(
            engine,
            "summarize",
            lambda meta, params: {
                "count": 3,
                "body": "PRIVATE_RESULT",
                "secret": "OUTPUT_SECRET",
            },
            meta={
                "params": [
                    {"name": "message", "label": "消息", "type": "string"},
                    {"name": "mode", "label": "模式", "type": "select", "summary": "value"},
                    {"name": "token", "label": "令牌", "type": "string", "summary": "value", "sensitive": True},
                    {"name": "hidden", "label": "隐藏", "type": "string", "summary": "hidden"},
                ],
                "outputs": [
                    {"name": "count", "label": "数量", "type": "number", "summary": "value"},
                    {"name": "body", "label": "正文", "type": "string"},
                    {"name": "secret", "label": "密文", "type": "string", "summary": "value", "sensitive": True},
                ],
            },
        )

        engine._run_action(
            {
                "type": "summarize",
                "params": {
                    "message": "PRIVATE_INPUT",
                    "mode": "safe",
                    "token": "INPUT_SECRET",
                    "hidden": "HIDDEN_INPUT",
                },
            },
            "规则A",
            _context(),
        )

        event = next(data for name, data in events if name == "action_executed")
        assert [item["display"] for item in event["input_summary"]] == [
            "文本 · 13 字符", "safe", "敏感值已隐藏",
        ]
        assert [item["display"] for item in event["output_summary"]] == [
            "3", "文本 · 14 字符", "敏感值已隐藏",
        ]
        assert "PRIVATE_INPUT" not in repr(event["input_summary"])
        assert "PRIVATE_RESULT" not in repr(event["output_summary"])
        assert event["params"]["token"] == "***"
        assert event["result"]["secret"] == "***"

    def test_sensitive_action_param_is_masked_in_failure_without_context_binding(self):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))

        def fail(meta, params):
            raise RuntimeError(f"拒绝令牌 {params['token']}")

        register_action(
            engine,
            "secret_param",
            fail,
            meta={
                "params": [{
                    "name": "token",
                    "label": "令牌",
                    "type": "string",
                    "sensitive": True,
                }],
            },
        )

        ok, result = engine._run_action(
            {"type": "secret_param", "params": {"token": "DIRECT_SECRET"}},
            "规则A",
            _context(),
        )

        event = next(data for name, data in events if name == "error")
        assert ok is False
        assert "DIRECT_SECRET" not in result
        assert "DIRECT_SECRET" not in event["error"]
        assert event["input_summary"][0]["display"] == "敏感值已隐藏"

    def test_sensitive_value_is_masked_inside_action_parameter(self):
        events = []
        engine = make_engine(on_event=lambda t, p: events.append((t, p)))
        engine.triggers_meta["clipboard"] = {
            "outputs": [
                {"name": "text", "type": "string", "sensitive": True},
            ],
        }
        register_action(engine, "consume", lambda meta, params: None)
        context = build_context(
            "规则A", "clipboard", {"text": "TOP_SECRET"}, []
        )

        engine._run_action(
            {
                "type": "consume",
                "params": {"message": "Bearer {{ event.payload.text }}"},
            },
            "规则A",
            context,
        )

        executed = [p for t, p in events if t == "action_executed"]
        assert executed[0]["params"]["message"] == "Bearer ***"

    def test_sensitive_value_is_masked_in_action_errors_and_logs(self, capsys):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        engine.triggers_meta["clipboard"] = {
            "outputs": [
                {"name": "text", "type": "string", "sensitive": True},
            ],
        }

        def fail_with_input(meta, params):
            raise RuntimeError(f"不能处理 {params['message']}")

        register_action(engine, "consume", fail_with_input)
        context = build_context(
            "规则A", "clipboard", {"text": "TOP_SECRET"}, []
        )

        ok, result = engine._run_action(
            {
                "type": "consume",
                "params": {
                    "message": {
                        "$ref": {
                            "scope": "event",
                            "path": ["text"],
                        }
                    }
                },
            },
            "规则A",
            context,
        )

        output = capsys.readouterr()
        error_event = next(data for name, data in events if name == "error")
        assert ok is False
        assert "TOP_SECRET" not in str(result)
        assert "TOP_SECRET" not in error_event["error"]
        assert "TOP_SECRET" not in output.out + output.err
        assert "***" in error_event["error"]

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

    def test_pipeline_continues_after_failure_when_selected(self):
        engine = make_engine()
        after = []
        register_action(engine, "broken", lambda meta, params: 1 / 0)
        register_action(engine, "after", lambda meta, params: after.append(1))

        engine.execute_workflow(
            "wf",
            {"actions": [
                {"type": "broken", "binding_id": "a_bad001", "params": {}, "on_error": "continue"},
                {"type": "after", "binding_id": "a_after001", "params": {}},
            ]},
            "规则",
            _context(),
        )

        assert after == [1]

    def test_pipeline_runs_failure_actions_before_continuing_main_flow(self):
        engine = make_engine()
        calls = []
        register_action(engine, "broken", lambda meta, params: 1 / 0)
        register_action(engine, "recover", lambda meta, params: calls.append("recover"))
        register_action(engine, "after", lambda meta, params: calls.append("after"))
        context = _context()

        engine.execute_workflow(
            "wf",
            {"actions": [
                {
                    "type": "broken",
                    "binding_id": "a_bad001",
                    "params": {},
                    "on_error": "continue",
                    "failure_actions": [{
                        "type": "recover",
                        "binding_id": "a_rec001",
                        "params": {},
                    }],
                },
                {"type": "after", "binding_id": "a_after001", "params": {}},
            ]},
            "规则",
            context,
        )

        assert calls == ["recover", "after"]
        assert context["steps"]["a_bad001"]["status"] == "failed"
        assert context["steps"]["a_rec001"]["status"] == "ok"

    def test_pipeline_does_not_run_failure_actions_after_success(self):
        engine = make_engine()
        calls = []
        register_action(engine, "works", lambda meta, params: calls.append("main"))
        register_action(engine, "recover", lambda meta, params: calls.append("recover"))

        engine.execute_workflow(
            "wf",
            {"actions": [{
                "type": "works",
                "binding_id": "a_main001",
                "params": {},
                "failure_actions": [{
                    "type": "recover",
                    "binding_id": "a_rec001",
                    "params": {},
                }],
            }]},
            "规则",
            _context(),
        )

        assert calls == ["main"]

    def test_failed_recovery_stops_remaining_failure_actions(self):
        engine = make_engine()
        calls = []
        register_action(engine, "broken", lambda meta, params: 1 / 0)
        register_action(engine, "recover_broken", lambda meta, params: 1 / 0)
        register_action(engine, "recover_after", lambda meta, params: calls.append("recover_after"))

        engine.execute_workflow(
            "wf",
            {"actions": [{
                "type": "broken",
                "binding_id": "a_bad001",
                "params": {},
                "failure_actions": [
                    {"type": "recover_broken", "binding_id": "a_rec001", "params": {}},
                    {"type": "recover_after", "binding_id": "a_rec002", "params": {}},
                ],
            }]},
            "规则",
            _context(),
        )

        assert calls == []

    def test_cancel_run_interrupts_context_action_without_recovery(self):
        events = []
        started = threading.Event()
        recovered = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))

        def run_with_context(meta, params, context):
            started.set()
            cancellation = context["runtime"]["cancellation"]
            cancellation.wait(5)
            cancellation.raise_if_cancelled()

        register_action(
            engine,
            "waitable",
            lambda meta, params: None,
            meta={
                "execution_api": "context-v1",
                "cancellation_api": "runtime-v1",
            },
            module=types.SimpleNamespace(run_with_context=run_with_context),
        )
        register_action(
            engine, "recover", lambda meta, params: recovered.append(1)
        )
        context = build_context(
            "规则", "manual", {}, [], run_id="run_cancel001"
        )
        thread = threading.Thread(
            target=engine.execute_workflow,
            args=(
                "wf",
                {"actions": [{
                    "type": "waitable",
                    "binding_id": "a_wait001",
                    "params": {},
                    "failure_actions": [{
                        "type": "recover",
                        "binding_id": "a_recover001",
                        "params": {},
                    }],
                }]},
                "规则",
                context,
            ),
        )

        thread.start()
        assert started.wait(timeout=2)
        assert engine.cancel_run("run_cancel001") is True
        thread.join(timeout=2)

        assert thread.is_alive() is False
        assert recovered == []
        assert context["steps"]["a_wait001"]["status"] == "cancelled"
        completed = [data for name, data in events if name == "workflow_completed"]
        assert completed[-1]["status"] == "cancelled"
        assert engine.cancel_run("run_cancel001") is False

    def test_action_timeout_uses_plugin_cancellation_contract(
        self, monkeypatch
    ):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))

        def short_cancellation(run_event=None, shutdown_event=None, timeout_seconds=None):
            return ActionCancellation(
                run_event,
                shutdown_event,
                0.01 if timeout_seconds is not None else None,
            )

        def run_with_context(meta, params, context):
            cancellation = context["runtime"]["cancellation"]
            cancellation.wait(1)
            cancellation.raise_if_cancelled()

        monkeypatch.setattr(
            workflow_executor_module, "ActionCancellation", short_cancellation
        )
        register_action(
            engine,
            "waitable",
            lambda meta, params: None,
            meta={
                "execution_api": "context-v1",
                "cancellation_api": "runtime-v1",
            },
            module=types.SimpleNamespace(run_with_context=run_with_context),
        )
        context = _context()

        engine.execute_workflow(
            "wf",
            {"actions": [{
                "type": "waitable",
                "binding_id": "a_wait001",
                "params": {},
                "timeout_seconds": 1,
            }]},
            "规则",
            context,
        )

        assert context["steps"]["a_wait001"]["status"] == "timed_out"
        assert any(name == "action_timed_out" for name, _data in events)

    def test_cancel_run_interrupts_retry_wait(self):
        events = []
        attempted = threading.Event()
        attempts = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))

        def broken(meta, params):
            attempts.append(1)
            attempted.set()
            raise RuntimeError("boom")

        register_action(engine, "broken", broken)
        context = build_context(
            "规则", "manual", {}, [], run_id="run_retry001"
        )
        thread = threading.Thread(
            target=engine.execute_workflow,
            args=(
                "wf",
                {"actions": [{
                    "type": "broken",
                    "binding_id": "a_broken001",
                    "params": {},
                    "retry": 1,
                    "retry_delay_seconds": 30,
                }]},
                "规则",
                context,
            ),
        )

        thread.start()
        assert attempted.wait(timeout=2)
        assert engine.cancel_run("run_retry001") is True
        thread.join(timeout=2)

        assert thread.is_alive() is False
        assert attempts == [1]
        assert any(name == "action_cancelled" for name, _data in events)
        completed = [data for name, data in events if name == "workflow_completed"]
        assert completed[-1]["status"] == "cancelled"

    def test_engine_shutdown_marks_cooperative_action_cancelled(self):
        events = []
        started = threading.Event()
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        engine._shutdown_flag = threading.Event()

        def run_with_context(meta, params, context):
            started.set()
            cancellation = context["runtime"]["cancellation"]
            cancellation.wait(5)
            cancellation.raise_if_cancelled()

        register_action(
            engine,
            "waitable",
            lambda meta, params: None,
            meta={
                "execution_api": "context-v1",
                "cancellation_api": "runtime-v1",
            },
            module=types.SimpleNamespace(run_with_context=run_with_context),
        )
        thread = threading.Thread(
            target=engine.execute_workflow,
            args=(
                "wf",
                {"actions": [{
                    "type": "waitable",
                    "binding_id": "a_wait001",
                    "params": {},
                }]},
                "规则",
                build_context(
                    "规则", "manual", {}, [], run_id="run_shutdown001"
                ),
            ),
        )

        thread.start()
        assert started.wait(timeout=2)
        engine._shutdown_flag.set()
        thread.join(timeout=2)

        assert thread.is_alive() is False
        completed = [data for name, data in events if name == "workflow_completed"]
        assert completed[-1]["status"] == "cancelled"

    def test_exponential_retry_waits_longer_after_each_failure(self, monkeypatch):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        waits = []
        attempts = []

        def flaky(meta, params):
            attempts.append(1)
            raise RuntimeError("boom")

        monkeypatch.setattr(
            workflow_executor_module.ActionCancellation,
            "wait",
            lambda self, seconds: waits.append(seconds) or False,
        )
        register_action(engine, "flaky", flaky)

        engine._run_action(
            {
                "type": "flaky",
                "params": {},
                "retry": 3,
                "retry_delay_seconds": 2,
                "retry_backoff": "exponential",
            },
            "规则",
            _context(),
        )

        assert len(attempts) == 4
        assert waits == [2, 4, 8]
        error = next(data for name, data in events if name == "error")
        assert error["attempt"] == 4

    def test_admin_rejection_is_not_retried(self):
        engine = make_engine()
        attempts = []

        def rejected(meta, params):
            attempts.append(1)
            raise AdminExecutionBlocked("用户拒绝")

        register_action(engine, "admin_action", rejected)
        ok, _result = engine._run_action(
            {"type": "admin_action", "params": {}, "retry": 3},
            "规则",
            _context(),
        )

        assert ok is False
        assert attempts == [1]

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

    def test_cancel_run_removes_deferred_timer(self):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        module = types.SimpleNamespace(
            check_precondition=lambda meta, params, context: {
                "ok": False,
                "reason": "目录仍在使用",
                "retry_after_seconds": 30,
            }
        )
        register_action(
            engine,
            "doc_check",
            lambda meta, params: None,
            meta={"precondition_api": "context-v1"},
            module=module,
        )
        context = build_context(
            "规则", "manual", {}, [], run_id="run_deferred001"
        )

        engine.execute_workflow(
            "wf",
            {"preconditions": [{
                "type": "doc_check",
                "binding_id": "p_doc001",
                "params": {},
            }], "actions": []},
            "规则",
            context,
        )

        assert "wf" in engine._deferred_workflows
        assert engine.cancel_run("run_deferred001") is True
        assert "wf" not in engine._deferred_workflows
        completed = [data for name, data in events if name == "workflow_completed"]
        assert completed[-1]["status"] == "cancelled"

    def test_repeated_deferred_workflow_completes_coalesced_run(self):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        module = types.SimpleNamespace(
            check_precondition=lambda meta, params, context: {
                "ok": False,
                "reason": "目录仍在使用",
                "retry_after_seconds": 30,
            }
        )
        register_action(
            engine,
            "doc_check",
            lambda meta, params: None,
            meta={"precondition_api": "context-v1"},
            module=module,
        )
        rule = {
            "preconditions": [{
                "type": "doc_check",
                "binding_id": "p_doc001",
                "params": {},
            }],
            "actions": [],
        }
        first = build_context("规则", "manual", {}, [], run_id="run_deferred001")
        second = build_context("规则", "manual", {}, [], run_id="run_deferred002")

        engine.execute_workflow("wf", rule, "规则", first)
        engine.execute_workflow("wf", rule, "规则", second)

        completed = [data for name, data in events if name == "workflow_completed"]
        assert completed[-1]["run_id"] == "run_deferred002"
        assert completed[-1]["status"] == "cancelled"
        assert "run_deferred002" not in engine._workflow_executor._run_cancel_events
        engine._cancel_deferred_workflows()

    def test_cancel_deferred_workflows_completes_runs(self):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        module = types.SimpleNamespace(
            check_precondition=lambda meta, params, context: {
                "ok": False,
                "reason": "目录仍在使用",
                "retry_after_seconds": 30,
            }
        )
        register_action(
            engine,
            "doc_check",
            lambda meta, params: None,
            meta={"precondition_api": "context-v1"},
            module=module,
        )
        context = build_context("规则", "manual", {}, [], run_id="run_deferred003")
        engine.execute_workflow(
            "wf",
            {"preconditions": [{
                "type": "doc_check",
                "binding_id": "p_doc001",
                "params": {},
            }], "actions": []},
            "规则",
            context,
        )

        engine._cancel_deferred_workflows()

        completed = [data for name, data in events if name == "workflow_completed"]
        assert completed[-1]["run_id"] == "run_deferred003"
        assert completed[-1]["status"] == "cancelled"
        assert not engine._deferred_workflows
        assert "run_deferred003" not in engine._workflow_executor._run_cancel_events

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

    def test_shutdown_drains_actions_before_plugin_teardown(self):
        engine = make_engine()
        calls = []
        engine._wait_active_actions = lambda timeout=60.0: calls.append("drain") or True
        engine._plugin_modules["plug"] = types.SimpleNamespace(
            teardown=lambda: calls.append("teardown")
        )

        engine.shutdown()

        assert calls == ["drain", "teardown"]
        assert engine._plugin_modules == {}

    def test_start_revokes_privilege_session_when_runtime_fails(self, monkeypatch):
        from notmyfault.platform import platform_support
        monkeypatch.setattr(platform_support, "show_notification", lambda *a, **k: None)
        engine = make_engine()
        # 没有任何触发器时 _run 直接返回，start() 仍应撤销权限会话
        engine.start(shutdown_event=threading.Event())
        assert engine._privilege_session_closed is True

    def test_close_can_retry_after_unclean_shutdown(self):
        engine = make_engine()
        calls = []
        engine._sudo = types.SimpleNamespace(
            end_engine_session=lambda token: calls.append(token)
        )
        engine._shutdown_clean = False

        engine.close()

        assert engine._privilege_session_closed is False
        assert calls == []

        engine._shutdown_clean = True
        engine.close()

        assert engine._privilege_session_closed is True
        assert calls == [engine._engine_token]

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
