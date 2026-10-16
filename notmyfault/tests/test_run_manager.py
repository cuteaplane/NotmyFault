import json
import copy
import threading
import time
import uuid
import pytest

from notmyfault.core.run_manager import RunManager
from notmyfault.tests.api_support import event_fields
from notmyfault.tests.canvas_fixtures import native_rule


def make_run_id():
    return f"run_{uuid.uuid4().hex}"


def make_context(rule_id="r1", run_id=None):
    return {
        "run": {"id": run_id or make_run_id()},
        "rule": {"id": rule_id, "name": "规则"},
    }


class FakeRuntime:
    """记录执行和取消的可控执行端"""

    def __init__(self):
        self.lock = threading.Lock()
        self.executed = []          # (rule_key, 规则名, run_id)
        self.cancelled = []         # 被 cancel_run 的 run_id
        self.history = []           # (事件类型, run_id, reason)
        self.observed_rules = []
        self.block = threading.Event()
        self.blocking = True

    def execute(self, rule_key, rule, rule_name, context):
        run_id = context["run"]["id"]
        if self.blocking:
            self.block.wait(timeout=10)
        with self.lock:
            self.executed.append((rule_key, rule.get("name", ""), run_id))
            self.observed_rules.append((run_id, copy.deepcopy(rule)))

    def cancel_run(self, run_id):
        with self.lock:
            self.cancelled.append(run_id)
        return True

    def on_history(self, kind, data):
        with self.lock:
            self.history.append((kind, data.get("run_id"), data.get("reason")))

    def executed_ids(self):
        with self.lock:
            return [item[2] for item in self.executed]

    def history_of(self, kind):
        with self.lock:
            return [item for item in self.history if item[0] == kind]


def make_scheduler(rule, runtime, **kwargs):
    return RunManager(
        execute_fn=runtime.execute,
        on_event=runtime.on_history,
        **kwargs,
    )


def submit_async(scheduler, rule, rule_key, holder, index, context=None):
    context = context or make_context()

    def run():
        holder[index] = (scheduler.submit(rule_key, rule, "规则", context), context)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, context


def wait_until(predicate, timeout=5, message="条件超时"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), message


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled", "timed_out"])
def test_rule_status_counts_current_runs_and_keeps_latest_terminal(terminal):
    from notmyfault.core.run_manager import RunOutcome
    from notmyfault.core.predicates import evaluate_predicate

    entered, release = threading.Event(), threading.Event()
    def execute(key, rule, name, context):
        entered.set()
        assert release.wait(5)
        return RunOutcome(terminal)
    manager = RunManager(execute, max_workers=1)
    rule = {"rule_id": "r_target01", "concurrency": {"mode": "queue", "queue_limit": 1}}
    status = lambda expected: evaluate_predicate({"op": "run_status", "rule_id": "r_target01", "status": expected}, {}, status_lookup=manager.rule_status)
    try:
        assert status("never_run")
        assert manager.submit("key", rule, "规则", make_context(run_id="run_status1")) == "started"
        assert entered.wait(5)
        assert manager.submit("key", rule, "规则", make_context(run_id="run_status2")) == "queued"
        assert manager.rule_status("r_target01") == {"running": 1, "queued": 1, "last_status": None}
        assert status("running") and status("queued") and not status("never_run")
        assert manager.submit("key", rule, "规则", make_context(run_id="run_status3")) == "dropped"
        assert manager.rule_status("r_target01") == {"running": 1, "queued": 1, "last_status": "dropped"}
        assert status("dropped") and status("running")
        assert manager.cancel_run("run_status2")
        assert manager.rule_status("r_target01") == {"running": 1, "queued": 0, "last_status": "cancelled"}
        assert status("running") and status("cancelled")
        release.set()
        assert manager.wait_for_idle(5)
        assert manager.rule_status("r_target01") == {"running": 0, "queued": 0, "last_status": terminal}
        assert status(terminal) and not status("never_run")
    finally:
        release.set()
        manager.shutdown()
        manager.wait_for_idle(5)


class TestSingleMode:
    def test_started_run_does_not_block_submitter(self):
        entered = threading.Event()
        release = threading.Event()
        worker_thread = {}

        def execute(rule_key, rule, rule_name, context):
            worker_thread["id"] = threading.get_ident()
            entered.set()
            release.wait(timeout=10)

        scheduler = RunManager(
            execute_fn=execute,
        )
        caller_thread = threading.get_ident()
        started = time.perf_counter()
        try:
            assert scheduler.submit(
                "key", {"concurrency": {"mode": "single"}}, "规则", make_context()
            ) == "started"
            assert time.perf_counter() - started < 0.5
            assert entered.wait(timeout=5)
            assert worker_thread["id"] != caller_thread
        finally:
            release.set()
            wait_until(lambda: scheduler.stats()["key"]["running"] == 0)
            scheduler.shutdown()

    def test_drops_new_events_while_running(self):
        runtime = FakeRuntime()
        rule = {"name": "规则", "concurrency": {"mode": "single"}}
        scheduler = make_scheduler(rule, runtime)
        first_thread, _ = submit_async(scheduler, rule, "key", {}, 0)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 1,
                   message="第一个 run 没进入运行态")

        results = {}
        threads = [
            submit_from_thread(scheduler, rule, "key", results, i)
            for i in range(19)
        ]
        for thread in threads:
            thread.join(timeout=5)

        assert list(results.values()) == ["dropped"] * 19
        assert len(runtime.history_of("run.dropped")) == 19

        runtime.block.set()
        first_thread.join(timeout=5)
        wait_until(lambda: scheduler.stats()["key"]["running"] == 0,
                   message="run 结束后活跃集合没清空")

    def test_next_event_runs_after_finish(self):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "single"}}
        scheduler = make_scheduler(rule, runtime)
        thread, _ = submit_async(scheduler, rule, "key", {}, 0)
        runtime.block.set()
        thread.join(timeout=5)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 0)
        assert scheduler.submit("key", rule, "规则", make_context()) == "started"


def submit_from_thread(scheduler, rule, rule_key, result, index):
    context = make_context()

    def run():
        result[index] = scheduler.submit(rule_key, rule, "规则", context)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class TestQueueMode:
    def test_buffers_and_drains(self):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "queue"}}
        scheduler = make_scheduler(rule, runtime)
        first_thread, _ = submit_async(scheduler, rule, "key", {}, 0)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 1,
                   message="队首 run 没进入运行态")

        results = {}
        threads = [
            submit_from_thread(scheduler, rule, "key", results, i)
            for i in range(19)
        ]
        for thread in threads:
            thread.join(timeout=5)

        counts = {}
        for value in results.values():
            counts[value] = counts.get(value, 0) + 1
        assert counts == {"queued": 19}
        assert len(runtime.history_of("run.queued")) == 20
        assert scheduler.stats()["key"] == {"running": 1, "queued": 19}

        runtime.block.set()
        first_thread.join(timeout=5)
        wait_until(lambda: len(runtime.executed_ids()) == 20,
                   timeout=10, message="排队 run 没有全部执行")
        wait_until(lambda: scheduler.stats()["key"] == {"running": 0, "queued": 0},
                   message="排空后统计没归零")

    def test_queue_freezes_rule_snapshot(self):
        runtime = FakeRuntime()
        rule = {
            "name": "旧名字",
            "concurrency": {"mode": "queue"},
            "actions": [{"type": "notify", "params": {"message": "旧值"}}],
        }
        scheduler = make_scheduler(rule, runtime)
        first_thread, _ = submit_async(scheduler, rule, "key", {}, 0)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 1)

        results = {}
        queued_thread, queued_context = submit_from_thread_with_context(
            scheduler, rule, "key", results, 0
        )
        queued_thread.join(timeout=5)
        rule["name"] = "新名字"
        rule["actions"][0]["params"]["message"] = "新值"

        runtime.block.set()
        first_thread.join(timeout=5)
        wait_until(lambda: len(runtime.executed) == 2, message="排队 run 没执行")

        names_by_run = {item[2]: item[1] for item in runtime.executed}
        assert names_by_run[queued_context["run"]["id"]] == "旧名字"
        rules_by_run = dict(runtime.observed_rules)
        assert (
            rules_by_run[queued_context["run"]["id"]]["actions"][0]["params"]["message"]
            == "旧值"
        )

    def test_queue_limit_drops_overflow(self):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "queue", "queue_limit": 3}}
        scheduler = make_scheduler(rule, runtime)
        first_thread, _ = submit_async(scheduler, rule, "key", {}, 0)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 1)

        results = {}
        threads = [
            submit_from_thread(scheduler, rule, "key", results, i)
            for i in range(5)
        ]
        for thread in threads:
            thread.join(timeout=5)

        counts = {}
        for value in results.values():
            counts[value] = counts.get(value, 0) + 1
        assert counts == {"queued": 3, "dropped": 2}
        reasons = [item[2] for item in runtime.history_of("run.dropped")]
        assert reasons == ["排队已满", "排队已满"]
        runtime.block.set()
        first_thread.join(timeout=5)

    def test_max_concurrency_allows_parallel_runs(self):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "queue", "max_concurrency": 2}}
        scheduler = make_scheduler(rule, runtime)
        first, _ = submit_async(scheduler, rule, "key", {}, 0)
        second, _ = submit_async(scheduler, rule, "key", {}, 1)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 2,
                   message="两个并发位没有同时占满")
        assert scheduler.submit("key", rule, "规则", make_context()) == "queued"
        runtime.block.set()
        first.join(timeout=5)
        second.join(timeout=5)

    @pytest.mark.parametrize("mode", ["parallel", "single", "replace"])
    def test_parallel_mode_bounds_workers_and_pending_runs(self, mode):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": mode, "queue_limit": 2}}
        scheduler = make_scheduler(rule, runtime, max_workers=2, max_pending_runs=2)

        decisions = [
            scheduler.submit("key" if mode == "parallel" else str(index), rule, "规则", make_context())
            for index in range(5)
        ]

        started = "started"
        assert decisions == [started, started, "queued", "queued", "dropped"]
        assert sum(row["running"] for row in scheduler.stats().values()) == 2
        assert sum(row["queued"] for row in scheduler.stats().values()) == 2
        runtime.block.set()
        wait_until(lambda: len(runtime.executed_ids()) == 4)
        assert scheduler.wait_for_idle(timeout=5)
        assert all(row == {"running": 0, "queued": 0} for row in scheduler.stats().values())
        for key in list(scheduler.stats()):
            scheduler.drop_rule(key)
        assert scheduler.stats() == {}
        scheduler.shutdown()


def test_execute_exception_emits_stable_terminal_event(capsys):
    events = []

    def fail(*args):
        raise RuntimeError("private detail")

    scheduler = RunManager(
        execute_fn=fail,
        on_event=lambda kind, data: events.append((kind, data)),
    )
    context = make_context()
    assert scheduler.submit("key", {"rule_id": "r_rule001"}, "规则", context) == "started"
    assert scheduler.wait_for_idle(5)

    failures = [data for kind, data in events if kind == "run.failed"]
    assert failures == [{
        "run_id": context["run"]["id"],
        "rule_id": "r_rule001",
        "rule_name": "规则",
        "status": "failed",
        "error": {
            "code": "execution_failed",
            "message": "工作流执行异常（RuntimeError）",
        },
    }]
    assert "private detail" not in str(failures)
    error_log = next(line for line in capsys.readouterr().out.splitlines() if '"event": "workflow_execution_failed"' in line)
    logged = json.loads(error_log.partition("] ")[2])
    assert logged["error_type"] == "RuntimeError"
    assert "RuntimeError: private detail" in logged["traceback"]
    scheduler.shutdown()
def test_event_callback_failure_does_not_leave_worker_accounted_as_running():
    def callback(kind, _data):
        if kind == "run.succeeded":
            raise RuntimeError("subscriber failed")

    manager = RunManager(lambda *_args: None, on_event=callback)
    try:
        assert manager.submit("key", {"rule_id": "r_callback"}, "规则", make_context()) == "started"
        assert manager.wait_for_idle(5)
        assert manager.rule_status("r_callback") == {
            "running": 0, "queued": 0, "last_status": "succeeded",
        }
    finally:
        manager.shutdown()


def submit_from_thread_with_context(scheduler, rule, rule_key, result, index):
    context = make_context()

    def run():
        result[index] = scheduler.submit(rule_key, rule, "规则", context)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, context


class TestReplaceMode:
    def test_new_run_waits_for_cancelled_worker_to_return(self):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "replace"}}
        manager = make_scheduler(rule, runtime)
        first, second, third = make_context(), make_context(), make_context()
        try:
            assert manager.submit("key", rule, "规则", first) == "started"
            assert manager.submit("key", rule, "规则", second) == "queued"
            assert first["_run_cancel_event"].is_set()
            assert manager.stats()["key"] == {"running": 1, "queued": 1}
            assert manager.submit("key", rule, "规则", third) == "queued"
            assert manager.state(second["run"]["id"]) == "dropped"
            assert manager.state(first["run"]["id"]) == "running"
            runtime.block.set()
            assert manager.wait_for_idle(5)
            assert manager.state(first["run"]["id"]) == "cancelled"
            assert manager.state(third["run"]["id"]) == "succeeded"
            assert runtime.executed_ids() == [first["run"]["id"], third["run"]["id"]]
        finally:
            runtime.block.set()
            manager.shutdown()


class TestShutdownAndReload:
    def test_shutdown_drops_queue_with_history(self):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "queue"}}
        scheduler = make_scheduler(rule, runtime)
        first_thread, _ = submit_async(scheduler, rule, "key", {}, 0)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 1)
        results = {}
        threads = [
            submit_from_thread(scheduler, rule, "key", results, i)
            for i in range(5)
        ]
        for thread in threads:
            thread.join(timeout=5)
        assert set(results.values()) == {"queued"}

        scheduler.shutdown()
        assert scheduler.stats()["key"]["queued"] == 0
        dropped = runtime.history_of("run.dropped")
        assert len(dropped) == 5
        assert all(item[2] == "引擎关闭" for item in dropped)
        # 关闭后新事件直接丢弃
        assert scheduler.submit("key", rule, "规则", make_context()) == "dropped"
        runtime.block.set()
        first_thread.join(timeout=5)

    def test_drop_rule_discards_queue(self):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "queue"}}
        scheduler = make_scheduler(rule, runtime)
        first_thread, _ = submit_async(scheduler, rule, "key", {}, 0)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 1)
        results = {}
        threads = [
            submit_from_thread(scheduler, rule, "key", results, i)
            for i in range(3)
        ]
        for thread in threads:
            thread.join(timeout=5)

        assert scheduler.drop_rule("key") == 3
        dropped = runtime.history_of("run.dropped")
        assert all(item[2] == "规则已删除" for item in dropped)
        runtime.block.set()
        first_thread.join(timeout=5)


def test_deleted_rule_late_completion_cannot_replace_recreated_rule_status():
    from notmyfault.core.run_manager import RunOutcome

    jobs = []
    manager = RunManager(
        lambda _key, _rule, _name, context: RunOutcome(
            "failed" if context["run"]["id"] == "run_old" else "succeeded"
        ),
        max_workers=2,
        spawn_thread_fn=jobs.append,
    )
    rule = {"rule_id": "r_recreated", "concurrency": {"mode": "parallel", "max_concurrency": 2}}
    try:
        assert manager.submit("r_recreated", rule, "规则", make_context(run_id="run_old")) == "started"
        assert manager.drop_rule("r_recreated") == 0
        assert manager.rule_status("r_recreated") == {
            "running": 1, "queued": 0, "last_status": None,
        }
        assert manager.submit("r_recreated", rule, "规则", make_context(run_id="run_new")) == "started"
        assert len(jobs) == 2
        jobs[1]()
        assert manager.rule_status("r_recreated") == {
            "running": 1, "queued": 0, "last_status": "succeeded",
        }
        jobs[0]()
        assert manager.wait_for_idle(5)
        assert manager.rule_status("r_recreated") == {
            "running": 0, "queued": 0, "last_status": "succeeded",
        }
        assert manager.stats()["r_recreated"] == {"running": 0, "queued": 0}
    finally:
        manager.shutdown()
class TestEngineWiring:
    def _make_engine(self, rule, on_event):
        from notmyfault.tests.api_support import create_test_engine

        engine = create_test_engine({"rules": [rule]}, on_event=on_event)
        engine._alert_user = lambda *a, **k: None
        engine.triggers_meta.setdefault("hotkey", {"semantic": "oneshot"})
        return engine

    def test_single_mode_drops_second_event_end_to_end(self):
        import threading

        events = []
        release = threading.Event()

        def slow_action(meta, params, _run_context):
            release.wait(timeout=10)
            return {"ok": True}

        rule = native_rule({"type": "hotkey", "params": {"key": "f1"}}, [{"type": "noop", "params": {}}], rule_id="r_single01", name="单实例规则", concurrency={"mode": "single"})
        engine = self._make_engine(rule, lambda event: events.append((event.type, event_fields(event))))
        engine.actions_funcs["noop"] = slow_action
        engine.actions_meta["noop"] = {}

        first = threading.Thread(
            target=lambda: engine.emit_event("hotkey", {"key": "f1"}), daemon=True
        )
        first.start()

        def running_count():
            return engine.scheduler_stats().get(
                "r_single01", {"running": 0}
            )["running"]

        deadline = time.time() + 5
        while running_count() < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert running_count() == 1

        engine.emit_event("hotkey", {"key": "f1"})
        assert engine.events.flush()
        dropped = [e for e in events if e[0] == "run.dropped"]
        assert len(dropped) == 1
        assert dropped[0][1]["reason"] == "已有运行中的 run"

        release.set()
        first.join(timeout=5)
        deadline = time.time() + 5
        while running_count() and time.time() < deadline:
            time.sleep(0.01)
        assert running_count() == 0

    def test_hot_reload_drops_queue_of_removed_rule(self, monkeypatch):
        import threading

        rule = native_rule({"op": "all", "children": [
                {"type": "hotkey", "params": {"key": "f2"}},
                {"type": "ready", "params": {}},
            ]}, [{"type": "noop", "params": {}}], rule_id="r_queue001", name="排队规则", concurrency={"mode": "queue"})
        events = []
        release = threading.Event()

        def slow_action(meta, params, _run_context):
            release.wait(timeout=10)
            return {"ok": True}

        engine = self._make_engine(rule, lambda event: events.append((event.type, event_fields(event))))
        engine.actions_funcs["noop"] = slow_action
        engine.actions_meta["noop"] = {}
        engine.triggers_meta["ready"] = {}

        def trigger_rule():
            engine.emit_event("hotkey", {"key": "f2"})
            engine.emit_event("ready", {})

        first = threading.Thread(target=trigger_rule, daemon=True)
        first.start()

        def queued_count():
            return engine.scheduler_stats().get(
                "r_queue001", {"queued": 0}
            )["queued"]

        deadline = time.time() + 5
        while engine.scheduler_stats().get("r_queue001", {}).get("running", 0) < 1 \
                and time.time() < deadline:
            time.sleep(0.01)

        for _ in range(3):
            trigger_rule()
        assert queued_count() == 3
        engine.emit_event("hotkey", {"key": "f2"})

        snapshot = engine._get_runtime_snapshot()
        with pytest.raises(ValueError):
            engine.apply_rules([native_rule({"op": "invalid", "children": [{"type": "hotkey"}]}, [{"type": "noop", "params": {}}], rule_id="r_invalid1", name="无效规则")])
        assert engine._get_runtime_snapshot() is snapshot
        assert queued_count() == 3

        def fail_reconcile(candidate):
            raise RuntimeError("启动失败")
        reconcile = engine._generations._reconcile
        monkeypatch.setattr(engine._generations, "_reconcile", fail_reconcile)
        with pytest.raises(RuntimeError, match="启动失败"):
            engine.apply_rules([])
        assert engine._get_runtime_snapshot() is snapshot
        assert queued_count() == 3
        assert engine.events.flush()
        assert not [event for event in events if event[0] == "run.dropped"]
        engine.emit_event("ready", {})
        assert queued_count() == 4

        monkeypatch.setattr(engine._generations, "_reconcile", reconcile)
        engine.apply_rules([])
        assert queued_count() == 0
        assert engine.events.flush()
        dropped = [e for e in events if e[0] == "run.dropped"]
        assert len(dropped) == 4
        assert all(d[1]["reason"] == "规则已删除" for d in dropped)

        release.set()
        first.join(timeout=5)
        assert engine._run_manager.wait_for_idle(timeout=5)
        assert engine._run_manager.rule_status("r_queue001")["last_status"] is None
        for plugin_id in ("hotkey", "ready"):
            engine.triggers_meta[plugin_id]["trigger_api"] = "event-v2"
            engine.triggers_funcs[plugin_id] = lambda meta, config, emit, stop: None
        engine.triggers_meta["hotkey"]["params"] = [{"name": "key", "type": "text"}]
        engine.apply_rules([rule])
        trigger_rule()
        assert engine._run_manager.wait_for_idle(timeout=5)
        assert engine._run_manager.rule_status("r_queue001")["last_status"] == "succeeded"


def test_queued_cancellation_never_enters_executor_and_has_one_terminal():
    jobs, executed, events = [], [], []
    manager = RunManager(lambda key, rule, name, context: executed.append(context["run"]["id"]),
                         on_event=lambda kind, data: events.append((kind, data["run_id"])),
                         max_workers=1, spawn_thread_fn=jobs.append)
    first, queued = make_context(), make_context()
    rule = {"concurrency": {"mode": "queue"}}
    manager.submit("r", rule, "规则", first)
    assert manager.submit("r", rule, "规则", queued) == "queued"
    run_id = queued["run"]["id"]
    assert manager.cancel_run(run_id)
    assert queued["_run_cancel_event"].is_set()
    assert manager.cancel_run(run_id) is False
    jobs.pop(0)()
    assert manager.wait_for_idle(1)
    assert executed == [first["run"]["id"]]
    assert [kind for kind, value in events if value == run_id] == ["run.queued", "run.cancelled"]
    manager.shutdown()


def test_pending_rules_take_turns_and_callbacks_can_read_from_another_thread():
    jobs, executed, blocked_callbacks = [], [], []
    manager = None

    def callback(kind, data):
        completed = threading.Event()
        thread = threading.Thread(target=lambda: (manager.stats(), completed.set()))
        thread.start()
        if not completed.wait(2):
            blocked_callbacks.append(kind)
        thread.join()

    manager = RunManager(lambda key, rule, name, context: executed.append(context["run"]["id"]),
                         on_event=callback, max_workers=1, spawn_thread_fn=jobs.append)
    rule = {"concurrency": {"mode": "queue"}}
    for key, run_id in [("a", "a1"), ("a", "a2"), ("a", "a3"), ("b", "b1"), ("b", "b2")]:
        manager.submit(key, rule, key, make_context(key, run_id))
    while jobs:
        jobs.pop(0)()
    assert executed == ["a1", "a2", "b1", "a3", "b2"]
    assert blocked_callbacks == []
    assert manager.wait_for_idle(1)
    manager.shutdown()



@pytest.mark.parametrize("terminal", ["succeeded", "recovered", "failed", "cancelled", "timed_out", "dropped"])
def test_terminal_runs_stop_accepting_events(terminal):
    from types import SimpleNamespace
    from notmyfault.core.condition_program import build_matcher
    from notmyfault.core.event_observation import RunEventObservation
    from notmyfault.core.run_manager import RunOutcome

    condition = {"type": "hotkey", "binding_id": "t_hot001", "params": {}}
    source = native_rule(condition, [])
    compiled = SimpleNamespace(source=source, matcher_spec=build_matcher(source))
    observation = RunEventObservation(compiled)
    context = make_context()
    context["_event_observation"] = observation
    manager = RunManager(lambda *args: RunOutcome(terminal))
    try:
        if terminal == "dropped":
            manager.shutdown()
        manager.submit("r1", {"name": "规则"}, "规则", context)
        assert manager.wait_for_idle(3)
        assert manager.state(context["run"]["id"]) == terminal
        observation.record("hotkey", {})
        assert observation.snapshot() == frozenset()
    finally:
        manager.shutdown()
