"""run 状态约束：一个 run 只走到一个终点，迟到的重复终态事件全部无效"""

import json
import threading
import uuid

from notmyfault.core.engine import AutomationEngine
from notmyfault.core.rule_scheduler import RuleScheduler
from notmyfault.core.run_history import RunHistory, build_runs
from notmyfault.core.run_lifecycle import (
    RUN_EVENT_SCHEMA_VERSION,
    status_after,
)


def packets(*events):
    return [
        {"type": event, "data": data, "ts": index + 1}
        for index, (event, data) in enumerate(events)
    ]


class TestStatusAfter:
    def test_terminal_states_are_absorbing(self):
        for terminal in ("succeeded", "failed", "cancelled", "dropped", "replaced"):
            assert status_after("workflow_completed", {"status": "failed"}, terminal) is None

    def test_normal_progression(self):
        assert status_after("rule_triggered", {}, "queued") == "running"
        assert status_after("workflow_deferred", {}, "running") == "deferred"
        assert status_after("action_executed", {}, "deferred") == "running"
        assert status_after("run_dropped", {}, "queued") == "dropped"


class TestReplayInvariants:
    def test_duplicate_complete_keeps_first_terminal(self):
        run_id = f"run_{uuid.uuid4().hex}"
        runs = build_runs(packets(
            ("rule_triggered", {"run_id": run_id}),
            ("workflow_completed", {"run_id": run_id, "status": "succeeded"}),
            ("workflow_completed", {"run_id": run_id, "status": "failed"}),
        ))
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["failure_kind"] == ""

    def test_cancel_after_complete_is_ignored(self):
        run_id = f"run_{uuid.uuid4().hex}"
        runs = build_runs(packets(
            ("rule_triggered", {"run_id": run_id}),
            ("workflow_completed", {"run_id": run_id, "status": "succeeded"}),
            ("run_dropped", {"run_id": run_id, "reason": "引擎关闭"}),
        ))
        assert runs[0]["status"] == "succeeded"
        assert "drop_reason" not in runs[0] or runs[0]["drop_reason"] is None

    def test_resume_after_terminal_is_ignored(self):
        run_id = f"run_{uuid.uuid4().hex}"
        runs = build_runs(packets(
            ("rule_triggered", {"run_id": run_id}),
            ("workflow_completed", {"run_id": run_id, "status": "cancelled"}),
            ("action_executed", {"run_id": run_id, "step_id": "s1"}),
            ("workflow_deferred", {"run_id": run_id, "reason": "later"}),
        ))
        assert runs[0]["status"] == "cancelled"
        assert runs[0]["steps"] == []

    def test_deferred_run_resumes_to_running(self):
        run_id = f"run_{uuid.uuid4().hex}"
        partial = build_runs(packets(
            ("rule_triggered", {"run_id": run_id}),
            ("workflow_deferred", {"run_id": run_id, "reason": "前置未满足"}),
            ("action_executed", {"run_id": run_id, "step_id": "s1", "duration_ms": 5}),
        ))
        assert partial[0]["status"] == "running"

    def test_late_steps_after_dropped_do_not_appear(self):
        run_id = f"run_{uuid.uuid4().hex}"
        runs = build_runs(packets(
            ("rule_triggered", {"run_id": run_id}),
            ("run_dropped", {"run_id": run_id, "reason": "已有运行中的 run"}),
            ("action_executed", {"run_id": run_id, "step_id": "late"}),
        ))
        assert runs[0]["status"] == "dropped"
        assert runs[0]["steps"] == []


class TestSchemaVersion:
    def test_record_stamps_schema_version(self, tmp_path):
        history = RunHistory(str(tmp_path / "runs.jsonl"))
        history.record({"type": "rule_triggered", "data": {"run_id": "r1"}, "ts": 1})
        with open(tmp_path / "runs.jsonl", encoding="utf-8") as file:
            saved = json.loads(file.readline())
        assert saved["schema"] == RUN_EVENT_SCHEMA_VERSION

    def test_explicit_schema_not_overwritten(self, tmp_path):
        history = RunHistory(str(tmp_path / "runs.jsonl"))
        history.record({
            "type": "rule_triggered",
            "data": {"run_id": "r1"},
            "ts": 1,
            "schema": RUN_EVENT_SCHEMA_VERSION,
        })
        with open(tmp_path / "runs.jsonl", encoding="utf-8") as file:
            saved = json.loads(file.readline())
        assert saved["schema"] == RUN_EVENT_SCHEMA_VERSION

    def test_replayable_stays_false(self):
        run_id = f"run_{uuid.uuid4().hex}"
        runs = build_runs(packets(
            ("rule_triggered", {"run_id": run_id}),
            ("workflow_completed", {"run_id": run_id, "status": "succeeded"}),
        ))
        assert runs[0]["replayable"] is False


class TestExecutorSingleTerminal:
    def _make_engine(self, events):
        engine = AutomationEngine(
            {"rules": []}, on_event=lambda kind, data: events.append((kind, data))
        )
        engine._alert_user = lambda *a, **k: None
        return engine

    def test_binding_failure_emits_single_terminal(self):
        events = []
        engine = self._make_engine(events)
        engine.actions_funcs["noop"] = lambda meta, params: {"ok": True}
        engine.actions_meta["noop"] = {}
        rule = {
            "name": "绑定失败规则",
            "event": {"type": "hotkey", "params": {}},
            "actions": [{
                "type": "noop",
                "params": {"x": {"$ref": {"scope": "event", "path": ["missing"]}}},
            }],
        }
        engine.rules = [rule]
        engine.triggers_meta.setdefault("hotkey", {"semantic": "oneshot"})
        engine.emit_event("hotkey", {})

        terminals = [e for e in events if e[0] in ("workflow_failed", "workflow_completed")]
        assert len(terminals) == 1
        assert terminals[0][0] == "workflow_failed"

    def test_cancel_run_twice_only_first_wins(self):
        events = []
        engine = self._make_engine(events)
        engine.actions_funcs["noop"] = lambda meta, params: {"ok": True}
        engine.actions_meta["noop"] = {}
        rule = {
            "name": "正常规则",
            "event": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }
        engine.rules = [rule]
        engine.triggers_meta.setdefault("hotkey", {"semantic": "oneshot"})
        engine.emit_event("hotkey", {})
        run_id = [e[1]["run_id"] for e in events if e[0] == "rule_triggered"][0]

        assert engine.cancel_run(run_id) is False
        assert len([e for e in events if e[0] == "workflow_completed"]) == 1


class TestSchemaVersionReadSide:
    def test_newer_schema_event_is_rejected(self, tmp_path):
        history = RunHistory(str(tmp_path / "runs.jsonl"))
        history.record({
            "type": "rule_triggered",
            "data": {"run_id": "r1"},
            "ts": 1,
            "schema": RUN_EVENT_SCHEMA_VERSION + 1,
        })
        history.record({"type": "rule_triggered", "data": {"run_id": "r2"}, "ts": 2})
        runs = history.list_runs()
        assert [run["run_id"] for run in runs] == ["r2"]


class TestStopPathDropsQueue:
    def test_run_loop_exit_drops_queue_with_history(self):
        # 生产停止链路走 _run 循环退出清理，不走 shutdown()，队列也要丢并写 history
        import time as time_mod

        release = threading.Event()
        events = []

        def slow_action(meta, params):
            release.wait(timeout=10)
            return {"ok": True}

        rule = {
            "rule_id": "rule-stop-1",
            "name": "停止路径规则",
            "event": {"type": "hotkey", "params": {"key": "f3"}},
            "concurrency": {"mode": "queue"},
            "actions": [{"type": "noop", "params": {}}],
        }
        engine = AutomationEngine(
            {"rules": [rule]}, on_event=lambda kind, data: events.append((kind, data))
        )
        engine._alert_user = lambda *a, **k: None
        engine.triggers_meta.setdefault("hotkey", {"semantic": "oneshot"})
        engine.actions_funcs["noop"] = slow_action
        engine.actions_meta["noop"] = {}
        engine._shutdown_flag = threading.Event()

        from notmyfault.core.rule_scheduler import RuleScheduler

        scheduler = engine._rule_scheduler
        context1 = {"run": {"id": "run_stop_a"}, "rule": {"id": "rule-stop-1"}}
        context2 = {"run": {"id": "run_stop_b"}, "rule": {"id": "rule-stop-1"}}
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.__setitem__(
                0, scheduler.submit("rule-stop-1", rule, "停止路径规则", context1)
            ),
            daemon=True,
        )
        thread.start()
        deadline = time_mod.time() + 5
        while scheduler.stats().get("rule-stop-1", {}).get("running", 0) < 1 \
                and time_mod.time() < deadline:
            time_mod.sleep(0.01)
        assert scheduler.submit("rule-stop-1", rule, "停止路径规则", context2) == "queued"

        # 模拟 _run 循环退出后的清理序列
        engine._cancel_deferred_workflows()
        engine._rule_scheduler.shutdown()
        assert scheduler.stats()["rule-stop-1"]["queued"] == 0
        dropped = [e for e in events if e[0] == "run_dropped"]
        assert len(dropped) == 1
        assert dropped[0][1]["reason"] == "引擎关闭"
        release.set()
        thread.join(timeout=5)


class TestReplaceCancelWindow:
    def test_replace_retries_until_cancel_registered(self):
        # 旧 run 刚起跑、取消事件还没登记进 executor 时，replace 要重试到能取消为止
        import time as time_mod

        runtime_calls = {"registered": False}

        def cancel_run(run_id):
            if not runtime_calls["registered"]:
                return False
            runtime_calls["run_id"] = run_id
            return True

        def execute(rule_key, rule, rule_name, context):
            time_mod.sleep(0.05)

        def is_deferred(run_id):
            return False

        scheduler = RuleScheduler(
            execute_fn=execute,
            cancel_run_fn=cancel_run,
            is_deferred_fn=is_deferred,
        )
        rule = {"concurrency": {"mode": "replace"}}
        first = {"run": {"id": "run_w1"}, "rule": {}}
        thread = threading.Thread(
            target=lambda: scheduler.submit("k", rule, "r", first), daemon=True
        )
        thread.start()
        deadline = time_mod.time() + 5
        while scheduler.stats().get("k", {}).get("running", 0) < 1 \
                and time_mod.time() < deadline:
            time_mod.sleep(0.005)

        def register_later():
            time_mod.sleep(0.02)
            runtime_calls["registered"] = True

        reg = threading.Thread(target=register_later, daemon=True)
        reg.start()
        second = {"run": {"id": "run_w2"}, "rule": {}}
        assert scheduler.submit("k", rule, "r", second) == "replaced"
        assert runtime_calls["run_id"] == "run_w1"
        thread.join(timeout=5)
        reg.join(timeout=5)
