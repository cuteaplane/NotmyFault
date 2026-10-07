"""引擎动作执行、错误隔离、诊断与关闭行为"""

import importlib.util
import os
import threading
import time
import types
from decimal import Decimal

import pytest

from notmyfault.tests.api_support import create_test_engine, event_fields
from notmyfault.core import workflow_executor as workflow_executor_module
from notmyfault.core.workflow import ActionCancellation, build_context
from notmyfault.core.clock import FakeClock
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


def test_engine_action_timeout_uses_injected_clock():
    clock = FakeClock()
    engine = create_test_engine({"rules": []}, clock=clock)

    def advance(meta, params, context):
        cancellation = context["runtime"]["cancellation"]
        assert cancellation._clock is clock
        clock.advance(2)
        cancellation.raise_if_cancelled()

    register_action(engine, "advance", lambda *args: None,
                    {"execution_api": "context-v1", "cancellation_api": "runtime-v1"},
                    types.SimpleNamespace(run_with_context=advance))
    try:
        outcome = engine.execute_workflow("r_clock001", {"actions": [{
            "type": "advance", "binding_id": "a_clock001", "params": {}, "timeout_seconds": 1,
        }]}, "规则", _context())
        assert outcome.status == "timed_out"
    finally:
        engine.shutdown(2)


def test_engine_retry_wait_uses_injected_clock():
    class AdvancingClock(FakeClock):
        def wait(self, event, timeout):
            self.advance(timeout)
            return event.is_set()

    clock = AdvancingClock()
    engine = create_test_engine({"rules": []}, clock=clock)
    attempts = []

    def retry(meta, params):
        attempts.append(clock.monotonic())
        if len(attempts) == 1:
            raise ValueError("重试")

    register_action(engine, "retry", retry)
    try:
        outcome = engine.execute_workflow("r_clock002", {"actions": [{
            "type": "retry", "binding_id": "a_clock002", "params": {}, "retry": 1, "retry_delay_seconds": 2,
        }]}, "规则", _context())
        assert outcome.status == "succeeded"
        assert attempts[0] == 0 and attempts[1] >= 2
    finally:
        engine.shutdown(2)


def test_run_variables_follow_executed_branches_and_survive_failed_assignment(tmp_path):
    events, received = [], []
    engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))

    def consume(meta, params, context):
        received.append(list(params["rows"]))
        params["rows"].append(99)
        with pytest.raises(TypeError):
            context["variables"]["v_rows0001"].append(88)
        with pytest.raises(TypeError):
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
    assert engine.events.flush()
    assert next(data for name, data in events if name in ("run.succeeded", "run.failed", "run.cancelled", "run.timed_out"))["status"] == "failed"
    history = RunHistory(str(tmp_path / "runs.jsonl"))
    assert engine.events.flush()
    for timestamp, (name, data) in enumerate(events):
        history.record({"type": name, "data": data, "ts": timestamp})
    failed = next(step for step in history.get_run("run_variables")["steps"] if step["step_id"] == "a_invalid1")
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "type_mismatch"
    assert failed["error"]["message"]


def test_sensitive_variable_values_and_derived_action_outputs_stay_hidden():
    events = []
    engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))
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
    assert engine.events.flush()
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
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))
        register_action(engine, "noop", lambda meta, params: "done")
        context = build_context("规则A", "hotkey", {}, [], "r_rule001")

        engine._run_action(
            {"type": "noop", "binding_id": "a_step001", "params": {}},
            "规则A",
            context,
        )

        assert engine.events.flush()
        executed = [payload for name, payload in events if name == "run.step.succeeded"]
        assert executed[0]["rule_id"] == "r_rule001"
        assert executed[0]["step_id"] == "a_step001"

    def test_workflow_events_carry_run_status_and_duration(self):
        events = []
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))
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

        assert engine.events.flush()
        executed = [data for name, data in events if name == "run.step.succeeded"]
        assert engine.events.flush()
        completed = [data for name, data in events if name in ("run.succeeded", "run.failed", "run.cancelled", "run.timed_out")]
        assert executed[0]["run_id"] == "run_test001"
        assert executed[0]["duration_ms"] >= 0
        assert len(completed) == 1
        assert completed[0]["run_id"] == "run_test001"
        assert completed[0]["rule_id"] == "r_rule001"
        assert completed[0]["rule_name"] == "规则A"
        assert completed[0]["status"] == "succeeded"

    def test_manual_run_returns_the_same_run_id_used_by_events(self):
        events = []
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))
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
        assert engine._run_manager.wait_for_idle(timeout=5)

        assert ok is True
        assert message == "已开始执行"
        assert run_id.startswith("run_")
        assert engine.events.flush()
        run_events = [data for _name, data in events if data.get("run_id") == run_id]
        assert {data.get("rule_id") for data in run_events} == {"r_rule001"}
        assert engine.events.flush()
        assert any(name in ("run.succeeded", "run.failed", "run.cancelled", "run.timed_out") for name, data in events if data.get("run_id") == run_id)

    @pytest.mark.parametrize("count", [2, Decimal("2.0000000000000000001")])
    def test_manual_run_can_continue_from_step_with_supplied_upstream_result(self, count):
        events = []
        received = []
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))
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
        assert engine._run_manager.wait_for_idle(timeout=5)

        assert ok is True
        assert received == [{"value": "provided"}]
        assert engine.events.flush()
        triggered = [data for name, data in events if name == "run.triggered"]
        assert triggered[0]["action_count"] == 1
        assert engine.events.flush()
        assertion_event = [data for name, data in events if name == "run.assertions.completed"]
        assert assertion_event[0]["passed"] == 1
        assert engine.events.flush()
        completed = [data for name, data in events if name in ("run.succeeded", "run.failed", "run.cancelled", "run.timed_out")]
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
        assert engine._run_manager.wait_for_idle(timeout=5)

    @pytest.mark.parametrize("actual,expected,operator,passed", [
        ("actual-secret", "expected-secret", "equals", False),
        (True, 1, "equals", False),
        ([True], [1], "equals", False),
        ({"flag": [False]}, {"flag": [0]}, "equals", False),
        ([True], 1, "contains", False),
        ([1], True, "contains", False),
        ([[1, 2]], [1, 2], "contains", True),
        ([1, 2], [1, 2], "contains", False),
        ([{"flag": [True]}], {"flag": [1]}, "contains", False),
        ({"flag": True, "extra": "actual-secret"}, {"flag": 1}, "contains", False),
        ({"flag": [True]}, {"flag": [1]}, "contains", False),
        ({"flag": [False], "extra": "actual-secret"}, {"flag": [False]}, "contains", True),
        ([True], [True], "equals", True),
        ({"value": [Decimal("1"), 1.0]}, {"value": [1, 1]}, "equals", True),
        ("任务已完成", "已完成", "contains", True),
        (None, None, "exists", True),
    ])
    def test_manual_assertions_report_workflow_status_without_exposing_values(self, actual, expected, operator, passed):
        events = []
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))
        register_action(engine, "result", lambda meta, params: {"token": actual})
        context = build_context(
            "结果检查测试", "manual", {}, [], "r_assert001", "run_assert001"
        )
        context["manual_test"] = {
            "start_index": 0,
            "end_index": 0,
            "assertions": [{
                "step_id": "a_result001",
                "path": ["token"],
                "operator": operator,
                "expected": expected,
            }],
        }

        engine.execute_workflow(
            "r_assert001",
            {"actions": [{"type": "result", "binding_id": "a_result001", "params": {}}]},
            "结果检查测试",
            context,
        )

        assert engine.events.flush()
        assertion_event = [data for name, data in events if name == "run.assertions.completed"][0]
        assert engine.events.flush()
        completed = [data for name, data in events if name in ("run.succeeded", "run.failed", "run.cancelled", "run.timed_out")][0]
        assert assertion_event["passed"] == int(passed)
        assert assertion_event["results"][0]["passed"] is passed
        assert "actual-secret" not in repr(assertion_event)
        assert "expected-secret" not in repr(assertion_event)
        assert completed["status"] == ("succeeded" if passed else "failed")
        assert completed["failure_kind"] == ("" if passed else "assertion")

    def test_sensitive_action_result_is_masked_in_event(self):
        events = []
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))
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
        assert engine.events.flush()
        executed = [p for t, p in events if t == "run.step.succeeded"]
        assert executed[0]["result"] == {"token": "***", "ok": True}

    def test_sensitive_action_param_is_masked_in_failure_without_context_binding(self):
        events = []
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))

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

        assert engine.events.flush()
        event = next(data for name, data in events if name == "run.step.failed")
        assert ok is False
        assert "DIRECT_SECRET" not in result
        assert "DIRECT_SECRET" not in event["error"]
        assert event["input_summary"][0]["display"] == "敏感值已隐藏"

    def test_sensitive_value_is_masked_in_action_errors_and_logs(self, capsys):
        events = []
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))
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
        assert engine.events.flush()
        error_event = next(data for name, data in events if name == "run.step.failed")
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
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))

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
        assert engine.events.flush()
        completed = [data for name, data in events if name in ("run.succeeded", "run.failed", "run.cancelled", "run.timed_out")]
        assert completed[-1]["status"] == "cancelled"
        assert engine.cancel_run("run_cancel001") is False

    def test_action_timeout_uses_plugin_cancellation_contract(
        self, monkeypatch
    ):
        events = []
        attempts = []
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))

        def short_cancellation(run_event=None, shutdown_event=None, timeout_seconds=None, *, clock=None):
            return ActionCancellation(
                run_event,
                shutdown_event,
                0.01 if timeout_seconds is not None else None,
                clock=clock,
            )

        def run_with_context(meta, params, context):
            attempts.append(True)
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
                "retry": 3,
            }]},
            "规则",
            context,
        )

        assert context["steps"]["a_wait001"]["status"] == "timed_out"
        assert len(attempts) == 1
        assert engine.events.flush()
        assert any(name == "run.step.timed_out" for name, _data in events)

    def test_cancel_run_interrupts_retry_wait(self):
        events = []
        attempted = threading.Event()
        attempts = []
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))

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
        assert engine.events.flush()
        assert any(name == "run.step.cancelled" for name, _data in events)
        assert engine.events.flush()
        completed = [data for name, data in events if name in ("run.succeeded", "run.failed", "run.cancelled", "run.timed_out")]
        assert completed[-1]["status"] == "cancelled"

    def test_engine_shutdown_marks_cooperative_action_cancelled(self):
        events = []
        started = threading.Event()
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))
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
        assert engine.events.flush()
        completed = [data for name, data in events if name in ("run.succeeded", "run.failed", "run.cancelled", "run.timed_out")]
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
    @pytest.mark.parametrize("boolean_op", ["is_true", "is_false"])
    @pytest.mark.parametrize("value,present,expected", [
        (True, True, ["else", "after"]), (False, True, ["then", "after"]),
        (None, False, []), (None, True, []),
        (1, True, []), ("false", True, []), ([], True, []),
    ])
    def test_if_selects_one_branch_and_stops_on_invalid_data(
        self, source, boolean_op, value, present, expected, tmp_path
    ):
        events = []
        engine = make_engine(on_event=lambda event: events.append((event.type, event_fields(event))))
        called = []
        register_action(engine, "record", lambda meta, params: called.append(params["label"]))
        context = build_context("规则", "hotkey", {}, [], run_id="run_branch")
        if present:
            context["event"]["payload"] = {"ready": value}
        context["steps"]["a_source01"] = {"status": "ok" if present else "failed", "result": value}
        reference = {"scope": "event", "path": ["ready"]} if source == "event" else {"scope": "step", "node": "a_source01", "path": []}
        def action(label):
            return {"type": "record", "binding_id": f"a_{label}001", "params": {"label": label}}
        branch = {
            "type": "if", "binding_id": "a_branch01",
            "condition": {"op": "not", "children": [{
                "op": boolean_op, "left": {"$ref": reference},
            }]},
            "then": [action("then")], "else": [action("else")],
        }
        if boolean_op == "is_false":
            expected = [{"then": "else", "else": "then"}.get(label, label) for label in expected]
        succeeded = isinstance(value, bool)
        result = engine.execute_actions([branch, action("after")], "规则", context)
        assert result is succeeded
        assert called == expected
        assert context["steps"]["a_branch01"]["status"] == ("ok" if succeeded else "failed")
        history = RunHistory(str(tmp_path / "runs.jsonl"))
        assert engine.events.flush()
        for timestamp, (name, data) in enumerate(events):
            history.record({"type": name, "data": data, "ts": timestamp})
        saved = next(step for step in history.get_run("run_branch")["steps"] if step["step_id"] == "a_branch01")
        assert saved["status"] == ("succeeded" if succeeded else "failed")
        if not succeeded:
            assert saved["error"]["code"] == (
                "condition_evaluation_failed" if present else "missing_binding_value"
            )

    @pytest.mark.parametrize("wrapper", ["direct", "not", "all", "any"])
    @pytest.mark.parametrize("count,threshold,left,right,operator,matched", [
        (2, 1, 2, 2, "eq", True),
        (Decimal("2.0000000000000000001"), Decimal("2.0000000000000000000"), Decimal("2.0000000000000000001"), Decimal("2.0000000000000000001"), "eq", True),
        (2, 1, True, 1, "eq", False),
        (2, 1, [True], [1], "eq", False),
        (2, 1, [False], [0], "ne", True),
        (2, 1, {"ready": [True]}, {"ready": [1]}, "eq", False),
        (2, 1, {"ready": [False]}, {"ready": [0]}, "ne", True),
        (2, 1, [True], 1, "contains", False),
        (2, 1, [1], True, "contains", False),
        (2, 1, [{"ready": [True]}], {"ready": [1]}, "contains", False),
        (2, 1, [{"ready": [False]}], {"ready": [False]}, "contains", True),
        (2, 1, "任务已完成", "已完成", "contains", True),
        (2, 1, "任务未完成", "已完成", "contains", False),
        (2, 1, {"values": [Decimal("1"), 1.0]}, {"values": [1, 1]}, "eq", True),
    ])
    def test_if_nested_branch_uses_previous_results_and_propagates_failure(
        self, count, threshold, left, right, operator, matched, wrapper
    ):
        engine = make_engine()
        called = []
        register_action(engine, "query", lambda meta, params: {"count": count, "value": left})
        register_action(engine, "record", lambda meta, params: called.append(params["count"]))
        def fail(meta, params):
            raise RuntimeError("动作失败")
        register_action(engine, "fail", fail)
        source = {"$ref": {"scope": "step", "node": "a_query01", "path": ["count"]}}
        condition = {"op": operator, "left": {"$ref": {"scope": "step", "node": "a_query01", "path": ["value"]}}, "right": right}
        if wrapper == "not":
            condition = {"op": "not", "children": [condition]}
            matched = not matched
        elif wrapper in ("all", "any"):
            condition = {"op": wrapper, "children": [{"op": "is_true", "left": wrapper == "all"}, condition]}
        actions = [
            {"type": "query", "binding_id": "a_query01", "params": {}},
            {"type": "if", "binding_id": "a_outer01", "condition": {"op": "gt", "left": source, "right": threshold}, "then": [
                {"type": "if", "binding_id": "a_inner01", "condition": condition, "then": [
                    {"type": "record", "binding_id": "a_record01", "params": {"count": source}},
                    {"type": "fail", "binding_id": "a_failed01", "params": {}},
                ], "else": []},
            ], "else": []},
            {"type": "record", "params": {"count": 99}},
        ]
        context = _context()
        assert engine.execute_actions(actions, "规则", context) is (not matched)
        assert called == ([count] if matched else [99])
        assert context["steps"]["a_inner01"]["status"] == ("failed" if matched else "ok")

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


    @pytest.mark.parametrize("sample_failure", [None, "walk", "stat", "nan", "inf"] + [
        (field, value) for field in ("check_file_locks", "check_document_windows")
        for value in ("false", "true", "", 0, 1, None, [], {})
    ])
    def test_document_quiescent_requires_observation_then_allows(self, tmp_path, monkeypatch, sample_failure):
        tmp_path = tmp_path / "documents\u3000"
        tmp_path.mkdir()
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
        previous = dict(mod._observations)
        if sample_failure is not None:
            with monkeypatch.context() as patcher:
                if isinstance(sample_failure, tuple):
                    patcher.setitem(params, *sample_failure)
                    patcher.setattr(mod, "_can_open_exclusively", lambda path: False)
                    patcher.setattr(mod, "_visible_editing_windows", lambda: ["editing.docx"])
                elif sample_failure == "walk":
                    walk = mod.os.walk

                    def unreadable_directory(*args, **kwargs):
                        for row in walk(*args, **kwargs):
                            yield row
                            if kwargs.get("onerror") is not None:
                                kwargs["onerror"](PermissionError("子目录不可读"))
                            return

                    patcher.setattr(mod.os, "walk", unreadable_directory)
                elif sample_failure == "stat":
                    stat = mod.Path.stat

                    def unreadable_file(path, *args, **kwargs):
                        if path == tmp_path / "a.txt":
                            raise PermissionError("文件状态不可读")
                        return stat(path, *args, **kwargs)

                    patcher.setattr(mod.Path, "stat", unreadable_file)
                else:
                    patcher.setitem(params, "quiet_seconds", float(sample_failure))
                for _ in range(2):
                    failed = mod.check_precondition({}, params, {})
                    assert failed["ok"] is False
                    assert ("布尔" if isinstance(sample_failure, tuple) else "无法确认目录状态") in failed["reason"]
                assert mod._observations == previous
                if isinstance(sample_failure, tuple):
                    with pytest.raises(RuntimeError, match="布尔"):
                        mod.run({}, params)
        second = mod.check_precondition({}, params, {})
        assert second == {"ok": True}
        assert mod.run({}, params) == {"ok": True}
        if sample_failure is None:
            checked = []
            monkeypatch.setattr(mod, "_can_open_exclusively", lambda path: checked.append("file") or True)
            monkeypatch.setattr(mod, "_visible_editing_windows", lambda: checked.append("window") or [])
            for file_locks, document_windows in ((False, False), (False, True), (True, False), (True, True)):
                checked.clear()
                assert mod.check_precondition({}, {
                    **params, "check_file_locks": file_locks, "check_document_windows": document_windows,
                }, {}) == {"ok": True}
                assert checked == (["file"] if file_locks else []) + (["window"] if document_windows else [])
        mod._observations.clear()
        with pytest.raises(RuntimeError):
            mod.run({}, params)


class TestErrorIsolation:


    def test_stop_trigger_threads_keeps_unstoppable_thread_registered(self):
        engine = make_engine()
        hold = threading.Event()
        entered = threading.Event()

        def stubborn(meta, config, emit, stop_event):
            entered.set()
            hold.wait(timeout=10)

        engine.triggers_funcs["hotkey"] = stubborn
        engine.triggers_meta["hotkey"] = {"trigger_api": "event-v2"}
        register_action(engine, "noop", lambda meta, params: None)
        snapshot = engine._prepare_rules([{
            "name": "等待停止", "rule_id": "r_stop0001", "condition": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }])
        engine._start_trigger_threads(snapshot)
        assert entered.wait(1)
        assert engine._stop_trigger_threads(timeout=0.1) is False
        assert any(item["trigger_id"] == "hotkey" for item in engine._trigger_host.health().values())
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

    def test_start_revokes_privilege_session_after_empty_runtime_stops(self, monkeypatch):
        from notmyfault.platform import platform_support
        monkeypatch.setattr(platform_support, "show_notification", lambda *a, **k: None)
        engine = make_engine()
        engine._security_mode = type(engine._security_mode).PERMISSIVE
        shutdown = threading.Event()
        shutdown.set()
        engine.start(shutdown_event=shutdown)
        assert engine._privilege_session_closed is True

    def test_partial_trigger_start_failure_cleans_runtime(self, monkeypatch):
        from notmyfault.platform import platform_support

        monkeypatch.setattr(platform_support, "show_notification", lambda *a, **k: None)
        started = threading.Event()
        stopped = threading.Event()
        rule = {
            "name": "启动失败清理",
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
        engine.triggers_meta["hotkey"] = {"trigger_api": "event-v2"}
        start_triggers = engine._start_trigger_threads

        def fail_after_start(snapshot):
            assert start_triggers(snapshot) == 1
            assert started.wait(timeout=1)
            raise RuntimeError("startup failed")

        monkeypatch.setattr(engine, "_start_trigger_threads", fail_after_start)
        with pytest.raises(RuntimeError, match="startup failed"):
            engine.start(shutdown_event=threading.Event())

        assert stopped.wait(timeout=1)
        assert engine._trigger_host.health() == {}
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
        monkeypatch.setattr(engine, "_start_trigger_threads", lambda snapshot: 1)
        monkeypatch.setattr(
            engine._rule_watcher,
            "start",
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


@pytest.mark.parametrize("partial", [False, True])
def test_manual_rule_with_trigger_constants_runs_selected_actions(partial):
    received = []
    engine = make_engine()
    engine.triggers_meta["demo"] = {"params": [{"name": "path", "type": "text"}]}
    engine.triggers_funcs["demo"] = lambda *args: None
    register_action(engine, "record", lambda *args: None, meta={
        "execution_api": "context-v1", "params": [{"name": "value", "type": "text"}],
    }, module=types.SimpleNamespace(run_with_context=lambda meta, params, context: received.append(
        (params["value"], context["triggers"]["t_manual01"]["config"]["path"]))))
    rule = {"rule_id": "r_manual01", "name": "手动运行", "constants": [
        {"id": "c_folder01", "name": "目录", "value_type": "text", "value": "folder"}],
        "condition": {"type": "demo", "binding_id": "t_manual01", "params": {"path": {
            "$ref": {"scope": "constant", "node": "c_folder01", "path": []}}}},
        "actions": [
            {"type": "record", "binding_id": "a_first001", "params": {"value": "first"}},
            {"type": "record", "binding_id": "a_last0001", "params": {"value": {
                "$ref": {"scope": "trigger_config", "node": "t_manual01", "path": ["path"]}}}},
        ]}
    try:
        ok, _, run_id = engine.run_manual_rule_snapshot(rule, start_step_id="a_last0001" if partial else "")
        assert ok
        assert engine._run_manager.wait_for_idle(2)
        assert engine._run_manager.state(run_id) == "succeeded"
        assert received == ([("folder", "folder")] if partial else [("first", "folder"), ("folder", "folder")])
        assert "$ref" in rule["condition"]["params"]["path"]
    finally:
        engine.shutdown(2)


@pytest.mark.parametrize("change", ["rename", "modify", "delete"])
def test_saved_manual_queue_obeys_rule_generation_changes(change):
    import copy

    entered, release = threading.Event(), threading.Event()
    received = []
    source = {"rule_id": "r_manual02", "name": "排队运行", "condition": {
        "type": "demo", "binding_id": "t_manual02", "params": {}},
        "concurrency": {"mode": "queue"}, "actions": [{
            "type": "record", "binding_id": "a_record01", "params": {"value": "old"}}]}
    engine = make_engine([source])
    engine.triggers_meta["demo"] = {}
    engine.triggers_funcs["demo"] = lambda *args: None

    def record(meta, params):
        received.append(params["value"])
        if len(received) == 1:
            entered.set()
            release.wait(3)

    register_action(engine, "record", record, meta={"params": [{"name": "value", "type": "text"}]})
    generation = engine._get_runtime_snapshot().generation
    try:
        assert engine.run_manual_rule(0)[0]
        assert entered.wait(2)
        ok, _, queued_id = engine.run_manual_rule(0)
        assert ok and engine._run_manager.state(queued_id) == "queued"
        changed = copy.deepcopy(source)
        changed["name"] = "新名称"
        if change == "modify":
            changed["actions"][0]["params"]["value"] = "new"
        engine._generations.apply([] if change == "delete" else [changed], "test", activate=False, validate=False)
        assert engine._run_manager.state(queued_id) == ("queued" if change == "rename" else "dropped")
        release.set()
        assert engine._run_manager.wait_for_idle(2)
        assert received == (["old", "old"] if change == "rename" else ["old"])
        assert generation.references.count == 0
    finally:
        release.set()
        engine.shutdown(2)


def test_manual_admission_drops_rule_deleted_during_preparation(monkeypatch):
    source = {"rule_id": "r_manual03", "name": "手动运行", "condition": {"type": "demo", "params": {}},
              "actions": [{"type": "record", "params": {}}]}
    engine = make_engine([source])
    engine.triggers_meta["demo"] = {}
    engine.triggers_funcs["demo"] = lambda *args: None
    received = []
    register_action(engine, "record", lambda *args: received.append("ran"))
    generation = engine._get_runtime_snapshot().generation
    prepare = engine._generations.prepare

    def prepare_then_delete(document, **kwargs):
        candidate = prepare(document, **kwargs)
        monkeypatch.setattr(engine._generations, "prepare", prepare)
        engine._generations.apply([], "test", activate=False, validate=False)
        return candidate

    monkeypatch.setattr(engine._generations, "prepare", prepare_then_delete)
    try:
        ok, _, run_id = engine.run_manual_rule(0)
        assert not ok
        assert engine._run_manager.state(run_id) == "dropped"
        assert received == []
        assert generation.references.count == 0
    finally:
        engine.shutdown(2)
