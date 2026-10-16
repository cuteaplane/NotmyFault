"""run 状态约束：一个 run 只走到一个终点，迟到的重复终态事件全部无效"""

import json
import threading
import uuid

from notmyfault.tests.api_support import create_test_engine, event_fields
from notmyfault.tests.canvas_fixtures import native_rule
from notmyfault.core.run_manager import RunManager
from notmyfault.core.run_history import RunHistory, build_runs
from notmyfault.core.run_lifecycle import (
    RUN_EVENT_SCHEMA_VERSION,
    status_after,
)
from notmyfault.core.workflow import build_context


def packets(*events):
    return [
        {"v": RUN_EVENT_SCHEMA_VERSION, "type": event, "data": data, "ts": index + 1}
        for index, (event, data) in enumerate(events)
    ]


class TestStatusAfter:
    def test_terminal_states_are_absorbing(self):
        for terminal in ("succeeded", "recovered", "failed", "cancelled", "dropped", "timed_out"):
            assert status_after("run.succeeded", {"status": "failed"}, terminal) is None

    def test_normal_progression(self):
        assert status_after("run.started", {}, "queued") == "running"
        assert status_after("run.queued", {}, "running") is None
        assert status_after("run.step.succeeded", {}, "queued") == "running"
        assert status_after("run.dropped", {}, "queued") == "dropped"


class TestReplayInvariants:
    def test_duplicate_complete_keeps_first_terminal(self):
        run_id = f"run_{uuid.uuid4().hex}"
        runs = build_runs(packets(
            ("run.triggered", {"run_id": run_id}),
            ("run.succeeded", {"run_id": run_id, "status": "succeeded"}),
            ("run.failed", {"run_id": run_id, "status": "failed"}),
        ))
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["failure_kind"] == ""

    def test_cancel_after_complete_is_ignored(self):
        run_id = f"run_{uuid.uuid4().hex}"
        runs = build_runs(packets(
            ("run.triggered", {"run_id": run_id}),
            ("run.succeeded", {"run_id": run_id, "status": "succeeded"}),
            ("run.dropped", {"run_id": run_id, "reason": "引擎关闭"}),
        ))
        assert runs[0]["status"] == "succeeded"
        assert "drop_reason" not in runs[0] or runs[0]["drop_reason"] is None

    def test_resume_after_terminal_is_ignored(self):
        run_id = f"run_{uuid.uuid4().hex}"
        runs = build_runs(packets(
            ("run.triggered", {"run_id": run_id}),
            ("run.cancelled", {"run_id": run_id, "status": "cancelled"}),
            ("run.step.succeeded", {"run_id": run_id, "step_id": "s1"}),
            ("run.queued", {"run_id": run_id, "reason": "later"}),
        ))
        assert runs[0]["status"] == "cancelled"
        assert runs[0]["steps"] == []

    def test_queued_run_starts_once(self, tmp_path):
        run_id = f"run_{uuid.uuid4().hex}"
        events = packets(
            ("run.triggered", {"run_id": run_id}),
            ("run.queued", {"run_id": run_id, "reason": "前置未满足"}),
        )
        path = tmp_path / "runs.jsonl"
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        history = RunHistory(str(path))
        assert history.get_run(run_id)["status"] == "queued"
        history.record_async({"v": RUN_EVENT_SCHEMA_VERSION, "type": "run.started", "data": {"run_id": run_id}, "ts": 3})
        history.record_async({"v": RUN_EVENT_SCHEMA_VERSION, "type": "run.step.succeeded", "data": {"run_id": run_id, "step_id": "s1"}, "ts": 4})
        assert history.get_run(run_id)["status"] == "running"
        assert history.get_run(run_id)["steps"][0]["status"] == "succeeded"
        assert history.flush()
        assert len(path.read_text(encoding="utf-8").splitlines()) == 4

    def test_late_steps_after_dropped_do_not_appear(self):
        run_id = f"run_{uuid.uuid4().hex}"
        runs = build_runs(packets(
            ("run.triggered", {"run_id": run_id}),
            ("run.dropped", {"run_id": run_id, "reason": "已有运行中的 run"}),
            ("run.step.succeeded", {"run_id": run_id, "step_id": "late"}),
        ))
        assert runs[0]["status"] == "dropped"
        assert runs[0]["steps"] == []


    def test_replayable_stays_false(self):
        run_id = f"run_{uuid.uuid4().hex}"
        runs = build_runs(packets(
            ("run.triggered", {"run_id": run_id}),
            ("run.succeeded", {"run_id": run_id, "status": "succeeded"}),
        ))
        assert runs[0]["replayable"] is False


class TestExecutorSingleTerminal:
    def _make_engine(self, events):
        engine = create_test_engine(
            {"rules": []}, on_event=lambda event: events.append((event.type, event_fields(event)))
        )
        engine._alert_user = lambda *a, **k: None
        return engine

    def test_binding_failure_emits_single_terminal(self):
        events = []
        engine = self._make_engine(events)
        engine.actions_funcs["noop"] = lambda meta, params, _run_context: {"ok": True}
        engine.actions_meta["noop"] = {}
        compensation_started = threading.Event()
        release = threading.Event()
        def compensate(meta, params, _run_context):
            compensation_started.set()
            release.wait(5)
        engine.actions_funcs["compensate"] = compensate
        engine.actions_meta["compensate"] = {}
        rule = {
            "rule_id": "r_recovery_fixture01",
            "name": "绑定失败规则",
            "workflow": {
                "entry": None,
                "nodes": [
                    {"binding_id": "a_source_recovery01", "type": "trigger",
                     "trigger": {"type": "hotkey", "params": {}, "binding_id": "t_test00001"}},
                    {"binding_id": "a_noop_recovery01", "type": "noop",
                     "params": {"x": {"$ref": {"scope": "event", "path": ["missing"]}}}},
                    {"binding_id": "a_handler_recovery01", "type": "action_group", "exports": {}},
                    {"binding_id": "a_compensate_recovery01", "type": "compensate", "parent": "a_handler_recovery01", "params": {}},
                ],
                "edges": [
                    {"source": "a_source_recovery01", "port": "event", "target": "a_noop_recovery01", "channel": "control"},
                    {"source": "a_noop_recovery01", "port": "failure", "target": "a_handler_recovery01", "channel": "control"},
                    {"source": "a_handler_recovery01", "port": "entry", "target": "a_compensate_recovery01", "channel": "control"},
                ],
            },
            "concurrency": {"mode": "single"},
        }
        engine.rules = [rule]
        engine.triggers_meta.setdefault("hotkey", {"semantic": "oneshot"})
        engine.emit_event("hotkey", {})
        assert compensation_started.wait(5)
        assert engine.events.flush()
        assert not [e for e in events if e[0] in ("run.failed", "run.succeeded", "run.recovered")]
        engine.emit_event("hotkey", {})
        assert engine.events.flush()
        assert any(e[0] == "run.dropped" for e in events)
        release.set()
        assert engine._run_manager.wait_for_idle(timeout=5)

        assert engine.events.flush()
        terminals = [e for e in events if e[0] in ("run.failed", "run.succeeded", "run.recovered")]
        assert len(terminals) == 1
        assert terminals[0][0] == "run.recovered"
        assert terminals[0][1]["status"] == "recovered"

    def test_cancel_run_twice_only_first_wins(self):
        events = []
        engine = self._make_engine(events)
        engine.actions_funcs["noop"] = lambda meta, params, _run_context: {"ok": True}
        engine.actions_meta["noop"] = {}
        rule = native_rule({"type": "hotkey", "params": {}}, [{"type": "noop", "params": {}}], name="正常规则")
        engine.rules = [rule]
        engine.triggers_meta.setdefault("hotkey", {"semantic": "oneshot"})
        engine.emit_event("hotkey", {})
        assert engine._run_manager.wait_for_idle(timeout=5)
        assert engine.events.flush()
        run_id = [e[1]["run_id"] for e in events if e[0] == "run.triggered"][0]

        assert engine.cancel_run(run_id) is False
        assert engine.events.flush()
        assert len([e for e in events if e[0] == "run.succeeded"]) == 1


class TestSchemaVersionReadSide:
    def test_newer_schema_event_is_rejected(self, tmp_path):
        history = RunHistory(str(tmp_path / "runs.jsonl"))
        history.record_async({
            "type": "run.triggered",
            "data": {"run_id": "r1"},
            "ts": 1,
            "v": RUN_EVENT_SCHEMA_VERSION + 1,
        })
        history.record_async({"v": RUN_EVENT_SCHEMA_VERSION, "type": "run.triggered", "data": {"run_id": "r2"}, "ts": 2})
        runs = history.list_runs()
        assert [run["run_id"] for run in runs] == ["r2"]


class TestStopPathDropsQueue:
    def test_run_loop_exit_drops_queue_with_history(self):
        # 生产停止链路走 _run 循环退出清理，不走 shutdown()，队列也要丢并写 history
        import time as time_mod

        release = threading.Event()
        events = []

        def slow_action(meta, params, _run_context):
            release.wait(timeout=10)
            return {"ok": True}

        rule = native_rule({"type": "hotkey", "params": {"key": "f3"}}, [{"type": "noop", "params": {}}], rule_id="r_stop0001", name="停止路径规则", concurrency={"mode": "queue"})
        engine = create_test_engine(
            {"rules": [rule]}, on_event=lambda event: events.append((event.type, event_fields(event)))
        )
        engine._alert_user = lambda *a, **k: None
        engine.triggers_meta.setdefault("hotkey", {"semantic": "oneshot"})
        engine.actions_funcs["noop"] = slow_action
        engine.actions_meta["noop"] = {}
        engine._shutdown_flag = threading.Event()

        from notmyfault.core.run_manager import RunManager

        scheduler = engine._run_manager
        context1 = build_context(
            "停止路径规则", "hotkey", {"key": "f3"}, [], "r_stop0001", "run_stop_a"
        )
        context2 = build_context(
            "停止路径规则", "hotkey", {"key": "f3"}, [], "r_stop0001", "run_stop_b"
        )
        runtime_rule = rule
        supplied = {node["trigger"]["binding_id"] for node in rule["workflow"]["nodes"] if node["type"] == "trigger"}
        context1["_event_occurrences"] = supplied
        context2["_event_occurrences"] = supplied
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.__setitem__(
                0, scheduler.submit("r_stop0001", runtime_rule, "停止路径规则", context1)
            ),
            daemon=True,
        )
        thread.start()
        deadline = time_mod.time() + 5
        while scheduler.stats().get("r_stop0001", {}).get("running", 0) < 1 \
                and time_mod.time() < deadline:
            time_mod.sleep(0.01)
        assert scheduler.submit("r_stop0001", runtime_rule, "停止路径规则", context2) == "queued"

        # 模拟 _run 循环退出后的清理序列
        engine._run_manager.shutdown()
        assert scheduler.stats()["r_stop0001"]["queued"] == 0
        assert engine.events.flush()
        dropped = [e for e in events if e[0] == "run.dropped"]
        assert len(dropped) == 1
        assert dropped[0][1]["reason"] == "引擎关闭"
        release.set()
        thread.join(timeout=5)
        assert scheduler.wait_for_idle(timeout=5)


class TestAdmissionCancellation:
    def test_cancel_before_worker_enters_executor(self):
        jobs, calls = [], []
        manager = RunManager(execute_fn=lambda *args: calls.append(args), spawn_thread_fn=jobs.append)
        context = {"run": {"id": "run_waiting"}}
        assert manager.submit("r", {}, "规则", context) == "started"
        assert manager.cancel_run("run_waiting")
        assert manager.stats()["r"]["running"] == 1
        jobs.pop()()
        assert calls == []
        assert manager.state("run_waiting") == "cancelled"
        assert manager.cancel_run("run_waiting") is False
        assert manager.wait_for_idle(1)
        manager.shutdown()
