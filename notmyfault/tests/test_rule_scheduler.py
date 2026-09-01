import copy
import threading
import time
import uuid

from notmyfault.core.rule_scheduler import RuleScheduler


def make_run_id():
    return f"run_{uuid.uuid4().hex}"


def make_context(rule_id="r1", run_id=None):
    return {
        "run": {"id": run_id or make_run_id()},
        "rule": {"id": rule_id, "name": "规则"},
    }


class FakeRuntime:
    """记录执行、取消和 deferred 状态的可控执行端"""

    def __init__(self):
        self.lock = threading.Lock()
        self.executed = []          # (rule_key, 规则名, run_id)
        self.cancelled = []         # 被 cancel_run 的 run_id
        self.deferred = set()       # 处于 deferred 的 run_id
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

    def is_deferred(self, run_id):
        return run_id in self.deferred

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
    return RuleScheduler(
        execute_fn=runtime.execute,
        cancel_run_fn=runtime.cancel_run,
        is_deferred_fn=runtime.is_deferred,
        on_history_event=runtime.on_history,
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


class TestSingleMode:
    def test_started_run_does_not_block_submitter(self):
        entered = threading.Event()
        release = threading.Event()
        worker_thread = {}

        def execute(rule_key, rule, rule_name, context):
            worker_thread["id"] = threading.get_ident()
            entered.set()
            release.wait(timeout=10)

        scheduler = RuleScheduler(
            execute_fn=execute,
            cancel_run_fn=lambda run_id: True,
            is_deferred_fn=lambda run_id: False,
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
        assert len(runtime.history_of("run_dropped")) == 19

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
        assert len(runtime.history_of("run_queued")) == 19
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
        reasons = [item[2] for item in runtime.history_of("run_dropped")]
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

    def test_parallel_mode_bounds_workers_and_pending_runs(self):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "parallel", "queue_limit": 2}}
        scheduler = make_scheduler(rule, runtime, max_workers=2)

        decisions = [
            scheduler.submit("key", rule, "规则", make_context())
            for _ in range(5)
        ]

        assert decisions == ["started", "started", "queued", "queued", "dropped"]
        assert scheduler.stats()["key"] == {"running": 2, "queued": 2}
        runtime.block.set()
        wait_until(lambda: len(runtime.executed_ids()) == 4)
        wait_until(lambda: scheduler.stats()["key"] == {"running": 0, "queued": 0})
        scheduler.shutdown()


def test_execute_exception_emits_stable_terminal_event():
    events = []

    def fail(*args):
        raise RuntimeError("private detail")

    scheduler = RuleScheduler(
        execute_fn=fail,
        cancel_run_fn=lambda run_id: True,
        is_deferred_fn=lambda run_id: False,
        on_history_event=lambda kind, data: events.append((kind, data)),
    )
    context = make_context()
    assert scheduler.submit("key", {"rule_id": "r_rule001"}, "规则", context) == "started"
    wait_until(lambda: scheduler.stats()["key"]["running"] == 0)

    failures = [data for kind, data in events if kind == "workflow_failed"]
    assert failures == [{
        "run_id": context["run"]["id"],
        "rule_id": "r_rule001",
        "rule_name": "规则",
        "error": {
            "code": "scheduler_execution_failed",
            "message": "工作流执行异常",
        },
    }]
    assert "private detail" not in str(failures)
    scheduler.shutdown()


def submit_from_thread_with_context(scheduler, rule, rule_key, result, index):
    context = make_context()

    def run():
        result[index] = scheduler.submit(rule_key, rule, "规则", context)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, context


class TestReplaceMode:
    def test_replaces_running_run(self):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "replace"}}
        scheduler = make_scheduler(rule, runtime)
        first_thread, first_context = submit_async(scheduler, rule, "key", {}, 0)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 1)

        holder = {}
        second_thread, _ = submit_async(scheduler, rule, "key", holder, 1)
        wait_until(lambda: runtime.cancelled == [first_context["run"]["id"]],
                   message="replace 没有取消旧 run")
        replaced = runtime.history_of("run_replaced")
        assert replaced[0][1] == first_context["run"]["id"]

        # 旧 run 的终态事件晚到也不影响新 run
        scheduler.on_run_event("workflow_completed", first_context["run"]["id"])
        assert scheduler.stats()["key"]["running"] == 1

        runtime.block.set()
        second_thread.join(timeout=5)
        first_thread.join(timeout=5)
        assert holder[1][0] == "replaced"

    def test_replace_during_retry_loop(self):
        # 旧 run 卡在重试里（一直没退出），replace 仍然取消它并启动新 run
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "replace"}}
        scheduler = make_scheduler(rule, runtime)
        first_thread, first_context = submit_async(scheduler, rule, "key", {}, 0)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 1)

        holder = {}
        second_thread, _ = submit_async(scheduler, rule, "key", holder, 1)
        wait_until(lambda: runtime.cancelled == [first_context["run"]["id"]],
                   message="replace 没有取消卡在重试里的旧 run")
        # 第一个 run 还没退出，活跃集合里只剩新 run
        assert scheduler.stats()["key"]["running"] == 1
        runtime.block.set()
        second_thread.join(timeout=5)
        first_thread.join(timeout=5)
        assert holder[1][0] == "replaced"


class TestDeferredInteraction:
    def test_deferred_counts_as_active(self):
        runtime = FakeRuntime()
        rule = {"concurrency": {"mode": "single"}}
        scheduler = make_scheduler(rule, runtime)
        first_thread, first_context = submit_async(scheduler, rule, "key", {}, 0)
        wait_until(lambda: scheduler.stats().get("key", {}).get("running") == 1)

        runtime.deferred.add(first_context["run"]["id"])
        runtime.block.set()
        first_thread.join(timeout=5)
        wait_until(lambda: scheduler.stats()["key"]["running"] == 1,
                   message="deferred run 不应退出活跃集合")

        # run1 在 deferred 等待重试，仍算活跃，新事件被丢弃
        assert scheduler.submit("key", rule, "规则", make_context()) == "dropped"

        # deferred 的 run 收到终态事件后退场
        scheduler.on_run_event("workflow_completed", first_context["run"]["id"])
        wait_until(lambda: scheduler.stats()["key"]["running"] == 0)
        runtime.blocking = False
        assert scheduler.submit("key", rule, "规则", make_context()) == "started"


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
        dropped = runtime.history_of("run_dropped")
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
        dropped = runtime.history_of("run_dropped")
        assert all(item[2] == "规则已删除" for item in dropped)
        runtime.block.set()
        first_thread.join(timeout=5)


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

        def slow_action(meta, params):
            release.wait(timeout=10)
            return {"ok": True}

        rule = {
            "rule_id": "rule-single-1",
            "name": "单实例规则",
            "event": {"type": "hotkey", "params": {"key": "f1"}},
            "concurrency": {"mode": "single"},
            "actions": [{"type": "noop", "params": {}}],
        }
        engine = self._make_engine(rule, lambda kind, data: events.append((kind, data)))
        engine.actions_funcs["noop"] = slow_action
        engine.actions_meta["noop"] = {}

        first = threading.Thread(
            target=lambda: engine.emit_event("hotkey", {"key": "f1"}), daemon=True
        )
        first.start()

        def running_count():
            return engine.scheduler_stats().get(
                "rule-single-1", {"running": 0}
            )["running"]

        deadline = time.time() + 5
        while running_count() < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert running_count() == 1

        engine.emit_event("hotkey", {"key": "f1"})
        dropped = [e for e in events if e[0] == "run_dropped"]
        assert len(dropped) == 1
        assert dropped[0][1]["reason"] == "已有运行中的 run"

        release.set()
        first.join(timeout=5)
        deadline = time.time() + 5
        while running_count() and time.time() < deadline:
            time.sleep(0.01)
        assert running_count() == 0

    def test_hot_reload_drops_queue_of_removed_rule(self):
        import threading

        rule = {
            "rule_id": "rule-q-1",
            "name": "排队规则",
            "event": {"type": "hotkey", "params": {"key": "f2"}},
            "concurrency": {"mode": "queue"},
            "actions": [{"type": "noop", "params": {}}],
        }
        events = []
        release = threading.Event()

        def slow_action(meta, params):
            release.wait(timeout=10)
            return {"ok": True}

        engine = self._make_engine(rule, lambda kind, data: events.append((kind, data)))
        engine.actions_funcs["noop"] = slow_action
        engine.actions_meta["noop"] = {}

        first = threading.Thread(
            target=lambda: engine.emit_event("hotkey", {"key": "f2"}), daemon=True
        )
        first.start()

        def queued_count():
            return engine.scheduler_stats().get(
                "rule-q-1", {"queued": 0}
            )["queued"]

        deadline = time.time() + 5
        while engine.scheduler_stats().get("rule-q-1", {}).get("running", 0) < 1 \
                and time.time() < deadline:
            time.sleep(0.01)

        for _ in range(3):
            engine.emit_event("hotkey", {"key": "f2"})
        assert queued_count() == 3

        # 热重载把规则删掉，排队中的 run 一起丢弃
        engine._apply_hot_reload_rules([])
        assert queued_count() == 0
        dropped = [e for e in events if e[0] == "run_dropped"]
        assert len(dropped) == 3
        assert all(d[1]["reason"] == "规则已删除" for d in dropped)

        release.set()
        first.join(timeout=5)
