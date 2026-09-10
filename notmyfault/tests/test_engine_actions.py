"""引擎动作执行、错误隔离、诊断与关闭行为"""

import importlib.util
import os
import threading
import time
import types
from decimal import Decimal

import pytest

from notmyfault.tests.api_support import create_test_engine
from notmyfault.core import workflow_executor as workflow_executor_module
from notmyfault.core.workflow import ActionCancellation, build_context
from notmyfault.core.run_history import RunHistory
from notmyfault.security.errors import AdminExecutionBlocked


def make_engine(rules=None, on_event=None):
    engine = create_test_engine({"rules": rules or []}, on_event=on_event)
    engine._alert_user = lambda *a, **k: None
    return engine


def register_action(engine, action_type, func, meta=None, module=None):
    engine.actions_funcs[action_type] = func
    engine.actions_meta[action_type] = meta or {}
    if module is not None:
        engine._plugin_modules[action_type] = module


def _context():
    return build_context("规则", "hotkey", {}, [])


def test_run_variables_follow_executed_branches_and_survive_failed_assignment(tmp_path):
    events, received = [], []
    engine = make_engine(on_event=lambda name, data: events.append((name, data)))

    def consume(meta, params, context):
        received.append(list(params["rows"]))
        params["rows"].append(99)
        context["variables"]["v_rows0001"].append(88)
        context["constants"]["c_rows0001"].append(77)
        return {"count": len(received[-1])}

    register_action(engine, "consume", lambda meta, params: None, meta={
        "execution_api": "context-v1",
        "params": [{"name": "rows", "type": "textarea", "value_type": {"type": "array", "items": "int"}}],
        "outputs": [{"name": "count", "type": "number", "value_type": "int"}],
    }, module=types.SimpleNamespace(run_with_context=consume))
    rule = {
        "constants": [{"id": "c_rows0001", "name": "起始值", "value_type": {"type": "array", "items": "int"}, "value": [1]}],
        "variables": [
            {"id": "v_rows0001", "name": "记录", "value_type": {"type": "array", "items": "int"}, "initial": {"$ref": {"scope": "constant", "node": "c_rows0001", "path": []}}},
            {"id": "v_count001", "name": "数量", "value_type": "int"},
        ],
        "actions": [
            {"type": "set_variable", "binding_id": "a_invalid1", "variable": "v_rows0001", "value": [False], "on_error": "continue"},
            {"type": "consume", "binding_id": "a_before01", "params": {"rows": {"$ref": {"scope": "variable", "node": "v_rows0001", "path": []}}}},
            {"type": "if", "binding_id": "a_branch01", "condition": {"op": "is_true", "left": True},
             "then": [{"type": "set_variable", "binding_id": "a_assign01", "variable": "v_rows0001", "value": [1, 2]}],
             "else": [{"type": "set_variable", "binding_id": "a_assign02", "variable": "v_rows0001", "value": [9]}]},
            {"type": "consume", "binding_id": "a_consume1", "params": {"rows": {"$ref": {"scope": "variable", "node": "v_rows0001", "path": []}}}},
            {"type": "set_variable", "binding_id": "a_count001", "variable": "v_count001", "value": {"$ref": {"scope": "step", "node": "a_consume1", "path": ["count"]}}},
        ],
    }
    context = build_context("规则", "hotkey", {}, [], run_id="run_variables")
    engine.execute_workflow("rule", rule, "规则", context)
    assert received == [[1], [1, 2]]
    assert context["constants"]["c_rows0001"] == [1]
    assert context["variables"] == {"v_rows0001": [1, 2], "v_count001": 2}
    assert context["steps"]["a_invalid1"]["status"] == "failed"
    assert "a_assign02" not in context["steps"]
    assert context["steps"]["a_count001"]["result"] == {"value": 2}
    assert next(data for name, data in events if name == "workflow_completed")["status"] == "failed"
    history = RunHistory(str(tmp_path / "runs.jsonl"))
    for timestamp, (name, data) in enumerate(events):
        history.record({"type": name, "data": data, "ts": timestamp})
    failed = next(step for step in history.get_run("run_variables")["steps"] if step["step_id"] == "a_invalid1")
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "type_mismatch"
    assert failed["error"]["message"]


def test_sensitive_variable_values_and_derived_action_outputs_stay_hidden():
    events = []
    engine = make_engine(on_event=lambda name, data: events.append((name, data)))
    register_action(engine, "derive", lambda meta, params: {"derived": params["value"] + "!"}, meta={
        "params": [{"name": "value", "type": "string"}],
        "outputs": [{"name": "derived", "type": "string"}],
    })
    rule = {
        "constants": [
            {"id": "c_value001", "name": "密钥", "value_type": "text", "value": "secret-input", "sensitive": True},
            {"id": "c_secret01", "name": "密钥引用", "value_type": "text", "value": {"$ref": {"scope": "constant", "node": "c_value001", "path": []}}},
        ],
        "variables": [{"id": "v_secret01", "name": "运行密钥", "value_type": "text"}],
        "actions": [
            {"type": "set_variable", "binding_id": "a_secret01", "variable": "v_secret01", "value": {"$ref": {"scope": "constant", "node": "c_secret01", "path": []}}},
            {"type": "derive", "binding_id": "a_derive01", "params": {"value": {"$ref": {"scope": "variable", "node": "v_secret01", "path": []}}}},
        ],
    }
    context = _context()
    engine.execute_workflow("rule", rule, "规则", context)
    assert context["steps"]["a_derive01"]["result"] == {"derived": "secret-input!"}
    assert "secret-input" not in repr(events)


class TestExecuteAction:
    def test_launch_program_rejects_dynamic_path(self):
        engine = make_engine()
        register_action(
            engine,
            "launch_program",
            lambda meta, params: None,
            meta={"security": {"literal_only_params": ["path"]}},
        )
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
        assert result == "参数 'path' 只允许使用固定值"

    def test_execute_during_shutdown(self):
        engine = make_engine()
        register_action(engine, "noop", lambda meta, params: None)
        engine._shutdown_flag = threading.Event()
        engine._shutdown_flag.set()
        ok, result = engine._run_action({"type": "noop", "params": {}}, "规则", _context())
        assert ok is False
        assert result == "引擎正在关闭"

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
        assert engine._rule_scheduler.wait_for_idle(timeout=5)

        assert ok is True
        assert message == "已开始执行"
        assert run_id.startswith("run_")
        run_events = [data for _name, data in events if data.get("run_id") == run_id]
        assert {data.get("rule_id") for data in run_events} == {"r_rule001"}
        assert any(name == "workflow_completed" for name, data in events if data.get("run_id") == run_id)

    @pytest.mark.parametrize("count", [2, Decimal("2.0000000000000000001")])
    def test_manual_run_can_continue_from_step_with_supplied_upstream_result(self, count):
        events = []
        received = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        register_action(engine, "first", lambda meta, params: pytest.fail("skipped action ran"))
        register_action(
            engine,
            "second",
            lambda meta, params: received.append(params) or {"count": count},
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
        assert engine._rule_scheduler.wait_for_idle(timeout=5)

        assert ok is True
        assert received == [{"value": "provided"}]
        triggered = [data for name, data in events if name == "rule_triggered"]
        assert triggered[0]["action_count"] == 1
        assertion_event = [data for name, data in events if name == "test_assertions_completed"]
        assert assertion_event[0]["passed"] == 1
        completed = [data for name, data in events if name == "workflow_completed"]
        assert completed[0]["run_id"] == run_id
        assert completed[0]["status"] == "succeeded"

    def test_manual_run_obeys_single_concurrency(self):
        engine = make_engine()
        started = threading.Event()
        release = threading.Event()

        def blocking_action(meta, params):
            started.set()
            release.wait(timeout=5)

        register_action(engine, "blocking", blocking_action)
        rule = {
            "rule_id": "r_manual_single",
            "name": "单运行规则",
            "concurrency": {"mode": "single"},
            "actions": [
                {"type": "blocking", "binding_id": "a_block001", "params": {}}
            ],
        }

        first = engine.run_manual_rule_snapshot(rule, 0)
        assert started.wait(timeout=2)
        second = engine.run_manual_rule_snapshot(rule, 0)

        assert first[0] is True
        assert second[0] is False
        release.set()
        assert engine._rule_scheduler.wait_for_idle(timeout=5)

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
        assert "TOP_SECRET" not in str(error_event["error"])
        assert "TOP_SECRET" not in output.out + output.err
        assert error_event["error"] == {
            "code": "action_execution_failed",
            "message": "动作 consume 执行失败",
        }

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

    @pytest.mark.parametrize("source", ["event", "step"])
    @pytest.mark.parametrize("value, expected", [(True, ["else", "after"]), (False, ["then", "after"]), (None, [])])
    def test_if_selects_one_branch_and_stops_on_missing_data(self, source, value, expected, tmp_path):
        events = []
        engine = make_engine(on_event=lambda name, data: events.append((name, data)))
        called = []
        register_action(engine, "record", lambda meta, params: called.append(params["label"]))
        context = build_context("规则", "hotkey", {}, [], run_id="run_branch")
        if value is not None:
            context["event"]["payload"]["ready"] = value
        context["steps"]["a_source01"] = {"status": "failed" if value is None else "ok", "result": value}
        reference = {"scope": "event", "path": ["ready"]} if source == "event" else {"scope": "step", "node": "a_source01", "path": []}
        def action(label):
            return {"type": "record", "binding_id": f"a_{label}001", "params": {"label": label}}
        branch = {
            "type": "if", "binding_id": "a_branch01",
            "condition": {"op": "not", "children": [{
                "op": "is_true", "left": {"$ref": reference},
            }]},
            "then": [action("then")], "else": [action("else")],
        }
        result = engine.execute_actions([branch, action("after")], "规则", context)
        assert result is (value is not None)
        assert called == expected
        assert context["steps"]["a_branch01"]["status"] == ("failed" if value is None else "ok")
        history = RunHistory(str(tmp_path / "runs.jsonl"))
        for timestamp, (name, data) in enumerate(events):
            history.record({"type": name, "data": data, "ts": timestamp})
        saved = next(step for step in history.get_run("run_branch")["steps"] if step["step_id"] == "a_branch01")
        assert saved["status"] == ("failed" if value is None else "succeeded")
        if value is None:
            assert saved["error"]["code"] == "missing_binding_value"

    @pytest.mark.parametrize("count, threshold", [(2, 1), (Decimal("2.0000000000000000001"), Decimal("2.0000000000000000000"))])
    def test_if_nested_branch_uses_previous_results_and_propagates_failure(self, count, threshold):
        engine = make_engine()
        called = []
        register_action(engine, "query", lambda meta, params: {"count": count})
        register_action(engine, "record", lambda meta, params: called.append(params["count"]))
        def fail(meta, params):
            raise RuntimeError("动作失败")
        register_action(engine, "fail", fail)
        source = {"$ref": {"scope": "step", "node": "a_query01", "path": ["count"]}}
        actions = [
            {"type": "query", "binding_id": "a_query01", "params": {}},
            {"type": "if", "binding_id": "a_outer01", "condition": {"op": "gt", "left": source, "right": threshold}, "then": [
                {"type": "if", "binding_id": "a_inner01", "condition": {"op": "eq", "left": source, "right": count}, "then": [
                    {"type": "record", "binding_id": "a_record01", "params": {"count": source}},
                    {"type": "fail", "binding_id": "a_failed01", "params": {}},
                ], "else": []},
            ], "else": []},
            {"type": "record", "params": {"count": 99}},
        ]
        assert engine.execute_actions(actions, "规则", _context()) is False
        assert called == [count]

    @pytest.mark.parametrize("validation", [[], ["参数无效"], RuntimeError("校验异常")])
    def test_validate_params_called(self, validation):
        engine = make_engine()
        seen = []
        called = []
        def validate(meta, params):
            seen.append((meta, params))
            if isinstance(validation, Exception):
                raise validation
            return validation
        module = types.SimpleNamespace(validate_params=validate)
        register_action(
            engine, "checked", lambda meta, params: called.append(params), meta={"params": []}, module=module
        )
        engine._run_action({"type": "checked", "params": {"a": 1}}, "规则", _context())
        assert len(seen) == 1
        assert seen[0][1] == {"a": 1}
        assert bool(called) is (validation == [])


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
        # 第一次检查只记录当前状态，不触发动作
        first = mod.check_precondition({}, params, {})
        assert first["ok"] is False
        second = mod.check_precondition({}, params, {})
        assert second == {"ok": True}
        assert mod.run({}, params) == {"ok": True}
        mod._observations.clear()
        with pytest.raises(RuntimeError):
            mod.run({}, params)


class TestErrorIsolation:
    @pytest.mark.parametrize("exit_mode", ["crash", "return", "stopped"])
    def test_run_trigger_isolates_crash(self, exit_mode):
        engine = make_engine()
        alerts = []
        engine._alert_user = lambda title, message, open_dashboard=False: alerts.append(title)

        def broken(meta, config, emit, stop_event):
            if exit_mode == "crash":
                raise RuntimeError("触发器炸了")
            if exit_mode == "stopped":
                stop_event.set()

        engine._run_trigger("hotkey", "hotkey", broken, {}, {}, threading.Event())
        assert engine._diag_obj.snapshot()["trigger_crashes"] == (exit_mode != "stopped")
        assert len(alerts) == (exit_mode != "stopped")


    def test_stop_trigger_threads_keeps_unstoppable_thread_registered(self):
        engine = make_engine()
        hold = threading.Event()

        def stubborn(meta, config, emit, stop_event):
            hold.wait(timeout=10)

        engine.triggers_funcs["hotkey"] = stubborn
        engine.triggers_meta["hotkey"] = {}
        engine._start_trigger_threads([{"event": {"type": "hotkey", "params": {}}}])
        assert engine._stop_trigger_threads(timeout=0.1) is False
        assert "hotkey" in engine._trigger_supervisor._threads
        hold.set()
        engine._stop_trigger_threads(timeout=5)


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
        engine._security_mode = type(engine._security_mode).PERMISSIVE
        # 没有任何触发器时 _run 直接返回，start() 仍应撤销权限会话
        engine.start(shutdown_event=threading.Event())
        assert engine._privilege_session_closed is True

    def test_partial_trigger_start_failure_cleans_runtime(self, monkeypatch):
        from notmyfault.platform import platform_support

        monkeypatch.setattr(platform_support, "show_notification", lambda *a, **k: None)
        started = threading.Event()
        stopped = threading.Event()
        rule = {
            "event": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }
        engine = make_engine([rule])
        engine._security_mode = type(engine._security_mode).PERMISSIVE
        register_action(engine, "noop", lambda meta, params: None)

        def trigger(meta, config, emit, stop_event):
            started.set()
            stop_event.wait(timeout=2)
            stopped.set()

        engine.triggers_funcs["hotkey"] = trigger
        engine.triggers_meta["hotkey"] = {}
        start_triggers = engine._start_trigger_threads

        def fail_after_start():
            assert start_triggers() == 1
            assert started.wait(timeout=1)
            raise RuntimeError("startup failed")

        monkeypatch.setattr(engine, "_start_trigger_threads", fail_after_start)
        with pytest.raises(RuntimeError, match="startup failed"):
            engine.start(shutdown_event=threading.Event())

        assert stopped.wait(timeout=1)
        assert engine._trigger_supervisor.health() == {}
        assert engine._runtime_cleaned is True

    def test_hot_reload_start_failure_cleans_plugins(self, monkeypatch):
        from notmyfault.platform import platform_support

        monkeypatch.setattr(platform_support, "show_notification", lambda *a, **k: None)
        engine = make_engine()
        engine._security_mode = type(engine._security_mode).PERMISSIVE
        cleaned = []
        engine._plugin_modules["sample"] = types.SimpleNamespace(
            teardown=lambda: cleaned.append(True)
        )
        monkeypatch.setattr(engine, "_start_trigger_threads", lambda: 1)
        monkeypatch.setattr(
            engine._hot_reloader,
            "begin",
            lambda: (_ for _ in ()).throw(RuntimeError("reload failed")),
        )

        with pytest.raises(RuntimeError, match="reload failed"):
            engine.start(shutdown_event=threading.Event())

        assert cleaned == [True]
        assert engine._runtime_cleaned is True

    def test_close_revokes_privilege_session_after_unclean_shutdown(self):
        engine = make_engine()
        calls = []
        engine._sudo = types.SimpleNamespace(
            end_engine_session=lambda token: calls.append(token)
        )
        engine._shutdown_clean = False

        engine.close()

        assert engine._privilege_session_closed is True
        assert calls == [engine._engine_token]

        engine.close()
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
