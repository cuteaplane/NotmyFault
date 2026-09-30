"""TriggerSupervisor 的注册、停止、健康状态与 engine 兼容属性"""

import threading
import time

import pytest

from notmyfault.core.trigger_supervisor import TriggerSupervisor


def _wait(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def _body_waits_on_stop(*args):
    # 停止事件是触发器入口的第六个参数
    args[5].wait(timeout=10)


class TestRegistration:
    def test_start_returns_count(self):
        sup = TriggerSupervisor()
        count = sup.start(
            {"hotkey": [{}], "usb_insert": [{}]},
            {"hotkey": lambda *a: None, "usb_insert": lambda *a: None},
            {"hotkey": {}, "usb_insert": {}},
            _body_waits_on_stop,
        )
        assert count == 2
        assert set(sup._threads) == {"hotkey", "usb_insert"}
        sup.stop(timeout=5)

    def test_start_registers_before_thread_starts(self):
        sup = TriggerSupervisor()
        observed = {}

        def cb(instance_id, event_type, func, meta, config, stop_event):
            observed["registered"] = instance_id in sup._threads
            stop_event.wait(timeout=5)

        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, cb)
        sup.stop(timeout=5)
        # 线程体开始执行时字典里必须已有该线程，stop() 才不会漏掉它
        assert observed["registered"] is True

    def test_start_skips_missing_trigger(self):
        sup = TriggerSupervisor(alert_cb=lambda *a: None)
        count = sup.start(
            {"hotkey": [{}], "ghost": [{}]},
            {"hotkey": lambda *a: None},
            {"hotkey": {}},
            _body_waits_on_stop,
        )
        assert count == 1
        assert set(sup._threads) == {"hotkey"}
        sup.stop(timeout=5)

    def test_stop_cannot_join_thread_before_start_finishes(self):
        sup = TriggerSupervisor()
        started = threading.Event()

        def do_start():
            sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop)
            started.set()

        t = threading.Thread(target=do_start)
        t.start()
        assert sup.stop(timeout=5) is True
        t.join(timeout=5)
        assert started.is_set()
        assert sup._threads == {}

    def test_thread_start_failure_rolls_back_registration(self, monkeypatch):
        sup = TriggerSupervisor()

        def boom(self):
            raise RuntimeError("cannot start thread")

        monkeypatch.setattr(threading.Thread, "start", boom)
        assert sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop) == 0
        assert sup._threads == {}
        assert sup._events == {}


class TestRequestStopAll:
    def test_sets_all_events(self):
        sup = TriggerSupervisor()
        sup.start(
            {"hotkey": [{}], "usb_insert": [{}]},
            {"hotkey": lambda *a: None, "usb_insert": lambda *a: None},
            {"hotkey": {}, "usb_insert": {}},
            _body_waits_on_stop,
        )
        sup.request_stop_all()
        assert all(evt.is_set() for evt in sup._events.values())
        sup.stop(timeout=5)

    def test_idempotent_empty(self):
        sup = TriggerSupervisor()
        sup.request_stop_all()
        sup.request_stop_all()

    def test_idempotent_multiple_calls(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop)
        sup.request_stop_all()
        sup.request_stop_all()
        sup.stop(timeout=5)

    def test_stop_after_request_stop_all(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop)
        sup.request_stop_all()
        assert sup.stop(timeout=5) is True
        assert sup._threads == {}


class TestStopRetention:
    def test_stop_empty_returns_true(self):
        assert TriggerSupervisor().stop(timeout=1) is True

    def test_stop_removes_exited_thread(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop)
        assert sup.stop(timeout=5) is True
        assert sup._threads == {}
        assert sup._events == {}

    def _start_stuck(self, sup, hold):
        def stubborn(*args):
            # 无视停止信号，靠 hold 事件收尾
            hold.wait(timeout=10)

        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, stubborn)

    def test_stop_keeps_alive_thread(self):
        sup = TriggerSupervisor()
        hold = threading.Event()
        self._start_stuck(sup, hold)
        assert sup.stop(timeout=0.05) is False
        assert "hotkey" in sup._threads
        hold.set()
        sup.stop(timeout=5)

    def test_stop_mixed_threads(self):
        sup = TriggerSupervisor()
        hold = threading.Event()

        def stubborn(*args):
            hold.wait(timeout=10)

        sup.start(
            {"hotkey": [{}], "usb_insert": [{}]},
            {"hotkey": lambda *a: None, "usb_insert": lambda *a: None},
            {"hotkey": {}, "usb_insert": {}},
            lambda instance_id, *rest: (
                stubborn(*rest) if instance_id == "hotkey" else rest[4].wait(timeout=5)
            ),
        )
        assert sup.stop(timeout=0.2) is False
        assert set(sup._threads) == {"hotkey"}
        hold.set()
        assert sup.stop(timeout=5) is True

    def test_stop_real_thread_joins(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop)
        thread = sup._threads["hotkey"]
        assert sup.stop(timeout=5) is True
        assert not thread.is_alive()

    def test_repeated_stop_failures_keep_registration(self):
        sup = TriggerSupervisor()
        hold = threading.Event()
        self._start_stuck(sup, hold)
        assert sup.stop(timeout=0.05) is False
        assert sup.stop(timeout=0.05) is False
        assert "hotkey" in sup._threads
        hold.set()
        assert sup.stop(timeout=5) is True

    def test_stop_reports_all_exited_threads_once(self, capsys):
        sup = TriggerSupervisor()
        sup.start(
            {"hotkey": [{}], "usb_insert": [{}]},
            {"hotkey": lambda *a: None, "usb_insert": lambda *a: None},
            {"hotkey": {}, "usb_insert": {}},
            _body_waits_on_stop,
        )
        capsys.readouterr()
        assert sup.stop(timeout=5) is True
        out = capsys.readouterr().out
        assert out.count("已停止 2 个触发器线程") == 1


class TestHealth:
    def test_empty_health(self):
        assert TriggerSupervisor().health() == {}

    def test_healthy_thread(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {"k": 1}}, _body_waits_on_stop)
        entry = sup.health()["hotkey"]
        assert entry["alive"] is True
        assert entry["crashed"] is False
        assert entry["stuck"] is False
        assert entry["last_error"] is None
        # event-v1 每条线程共用整份配置列表
        assert entry["config"] == [{}]
        sup.stop(timeout=5)

    def test_crashed_thread(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, lambda *a: None)
        assert _wait(lambda: not sup._threads["hotkey"].is_alive())
        sup.mark_crashed("hotkey", "boom")
        entry = sup.health()["hotkey"]
        assert entry["alive"] is False
        assert entry["crashed"] is True
        assert entry["last_error"] == "boom"

    def test_dead_no_crash_not_flagged(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, lambda *a: None)
        assert _wait(lambda: not sup._threads["hotkey"].is_alive())
        entry = sup.health()["hotkey"]
        assert entry["alive"] is False
        assert entry["crashed"] is False

    def test_last_crash_takes_latest(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, lambda *a: None)
        assert _wait(lambda: not sup._threads["hotkey"].is_alive())
        sup.mark_crashed("hotkey", "first")
        sup.mark_crashed("hotkey", "second")
        assert sup.health()["hotkey"]["last_error"] == "second"

    def test_new_generation_clears_previous_crash(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, lambda *a: None)
        assert _wait(lambda: not sup._threads["hotkey"].is_alive())
        sup.mark_crashed("hotkey", "boom")
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop)
        entry = sup.health()["hotkey"]
        assert entry["alive"] is True
        assert entry["crashed"] is False
        assert entry["last_error"] is None
        sup.stop(timeout=5)

    def test_stuck_flag_after_repeated_stop_failures(self):
        sup = TriggerSupervisor()
        hold = threading.Event()

        def stubborn(*args):
            hold.wait(timeout=10)

        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, stubborn)
        sup.stop(timeout=0.05)
        assert sup.health()["hotkey"]["stuck"] is False
        sup.stop(timeout=0.05)
        assert sup.health()["hotkey"]["stuck"] is True
        hold.set()
        sup.stop(timeout=5)


class TestMissingAlert:
    def test_alert_called_for_missing(self):
        alerts = []
        sup = TriggerSupervisor(alert_cb=lambda title, msg: alerts.append((title, msg)))
        sup.start({"ghost": [{}]}, {}, {}, _body_waits_on_stop)
        assert len(alerts) == 1
        assert "ghost" in alerts[0][1]

    def test_no_alert_when_all_present(self):
        alerts = []
        sup = TriggerSupervisor(alert_cb=lambda title, msg: alerts.append((title, msg)))
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop)
        assert alerts == []
        sup.stop(timeout=5)

    def test_no_alert_cb_does_not_crash(self):
        sup = TriggerSupervisor(alert_cb=None)
        assert sup.start({"ghost": [{}]}, {}, {}, _body_waits_on_stop) == 0


class TestEngineIntegration:
    def _engine(self):
        from notmyfault.tests.api_support import create_test_engine
        engine = create_test_engine({"rules": []})
        engine._alert_user = lambda *args, **kwargs: None
        engine.actions_meta["noop"] = {}
        engine.actions_funcs["noop"] = lambda meta, params: None
        return engine

    def test_get_diagnostics_has_triggers_health(self):
        engine = self._engine()
        diag = engine.get_diagnostics()
        assert "triggers" in diag
        assert diag["triggers"]["health"] == {}

    def test_get_diagnostics_health_reflects_crash(self):
        engine = self._engine()
        engine.triggers_funcs["hotkey"] = lambda meta, config, emit, stop_event: None
        engine.triggers_meta["hotkey"] = {}
        snapshot = engine._prepare_rules([{
            "name": "触发器状态", "condition": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }])
        engine._start_trigger_threads(snapshot)
        assert _wait(
            lambda: not engine._trigger_supervisor._threads["hotkey"].is_alive()
        )
        engine._trigger_supervisor.mark_crashed("hotkey", "boom")
        health = engine.get_diagnostics()["triggers"]["health"]
        assert health["hotkey"]["crashed"] is True

    def test_run_trigger_still_works(self):
        engine = self._engine()
        calls = []

        def tfunc(meta, config, emit, stop_event):
            calls.append((meta, config))

        engine._run_trigger("hotkey", "hotkey", tfunc, {"x": 1}, {"k": 2}, threading.Event())
        assert calls == [({"x": 1}, {"k": 2})]

    def test_shutdown_uses_request_stop_all(self):
        engine = self._engine()
        called = []
        engine._trigger_supervisor.request_stop_all = lambda: called.append(True)
        engine.shutdown()
        assert called == [True]

    def test_stop_via_engine_delegates(self):
        engine = self._engine()
        engine._trigger_supervisor.stop = lambda timeout=30.0: "delegated"
        assert engine._stop_trigger_threads(timeout=1) == "delegated"

    def test_stop_removes_exited_via_engine(self):
        engine = self._engine()
        engine.triggers_funcs["hotkey"] = lambda meta, config, emit, stop_event: stop_event.wait(timeout=5)
        engine.triggers_meta["hotkey"] = {}
        snapshot = engine._prepare_rules([{
            "name": "触发器状态", "condition": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }])
        engine._start_trigger_threads(snapshot)
        assert "hotkey" in engine._trigger_supervisor._threads
        assert engine._stop_trigger_threads(timeout=5) is True
        assert engine._trigger_supervisor._threads == {}

    def test_main_loop_restarts_crashed_trigger(self):
        engine = self._engine()
        engine.rules = [{
            "name": "崩溃重启",
            "condition": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }]
        runs = []

        def flaky(meta, config, emit, stop_event):
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("第一次就崩")
            stop_event.wait(timeout=10)

        engine.triggers_funcs["hotkey"] = flaky
        engine.triggers_meta["hotkey"] = {}

        shutdown = threading.Event()
        thread = threading.Thread(target=engine.start, kwargs={"shutdown_event": shutdown})
        thread.start()
        try:
            assert _wait(lambda: runs == [1], timeout=5)
            assert _wait(lambda: len(runs) >= 2, timeout=10), "崩溃的触发器没有被主循环拉起来"
            assert engine._trigger_supervisor.health()["hotkey"]["alive"] is True
        finally:
            shutdown.set()
            thread.join(timeout=10)
        assert not thread.is_alive()


class TestDifferentialStart:
    """按配置指纹决定线程去留：没变的保留，变了或被删的停掉，新增的启动"""

    def test_unchanged_config_keeps_running_thread(self):
        sup = TriggerSupervisor()
        started = []

        def cb(instance_id, event_type, func, meta, config, stop_event):
            started.append(instance_id)
            stop_event.wait(timeout=10)

        funcs = {"demo": lambda *a: None}
        meta = {"demo": {"trigger_api": "event-v2"}}
        configs = {"demo": [{"n": 1}]}
        assert sup.start(configs, funcs, meta, cb) == 1
        assert _wait(lambda: started == ["demo"])
        thread = sup._threads["demo"]

        assert sup.start(configs, funcs, meta, cb) == 0
        assert sup._threads["demo"] is thread
        assert started == ["demo"]
        sup.stop(timeout=5)

    def test_changed_config_replaces_thread(self):
        sup = TriggerSupervisor()
        started = []

        def cb(instance_id, event_type, func, meta, config, stop_event):
            started.append((instance_id, config))
            stop_event.wait(timeout=10)

        funcs = {"demo": lambda *a: None}
        meta = {"demo": {"trigger_api": "event-v2"}}
        assert sup.start({"demo": [{"n": 1}]}, funcs, meta, cb) == 1
        first = sup._threads["demo"]

        assert sup.start({"demo": [{"n": 2}]}, funcs, meta, cb) == 1
        assert sup._threads["demo"] is not first
        assert _wait(lambda: len(started) == 2)
        assert started == [("demo", {"n": 1}), ("demo", {"n": 2})]
        sup.stop(timeout=5)

    def test_stuck_thread_does_not_block_new_config(self):
        sup = TriggerSupervisor()
        hold = threading.Event()
        started = []

        def cb(instance_id, event_type, func, meta, config, stop_event):
            started.append((instance_id, config))
            hold.wait(timeout=10)  # 不看停止事件，模拟卡住的线程

        funcs = {"demo": lambda *a: None}
        meta = {"demo": {"trigger_api": "event-v2"}}
        assert sup.start({"demo": [{"n": 1}]}, funcs, meta, cb) == 1
        assert sup.start({"demo": [{"n": 2}]}, funcs, meta, cb, stop_timeout=0.05) == 1
        # 旧配置的线程还占着 demo，新配置换一个 ID 起来
        assert _wait(lambda: len(started) == 2)
        assert started == [("demo", {"n": 1}), ("demo:1", {"n": 2})]

        sup.start({"demo": [{"n": 2}]}, funcs, meta, cb, stop_timeout=0.05)
        health = sup.health()
        assert health["demo"]["alive"] is True
        assert health["demo"]["stuck"] is True
        assert health["demo"]["config"] == {"n": 1}
        assert health["demo:1"]["config"] == {"n": 2}

        hold.set()
        sup.stop(timeout=5)


class TestCrashRestart:
    """崩溃退出的实例按退避自动重启"""

    def _sup(self, runs):
        def cb(instance_id, event_type, func, meta, config, stop_event):
            runs.append(instance_id)
            # 直接返回，模拟立刻崩掉的触发器

        sup = TriggerSupervisor()
        funcs = {"demo": lambda *a: None}
        meta = {"demo": {"trigger_api": "event-v2"}}
        assert sup.start({"demo": [{"n": 1}]}, funcs, meta, cb) == 1
        assert _wait(lambda: not sup._threads["demo"].is_alive())
        return sup

    def test_restarts_only_after_backoff(self):
        runs = []
        sup = self._sup(runs)
        assert sup.mark_crashed("demo", "boom") == 1
        moment = time.monotonic()

        assert sup.revive_crashed(now=moment) == 0
        assert len(runs) == 1

        assert sup.revive_crashed(now=moment + 2.5) == 1
        assert _wait(lambda: len(runs) == 2)
        sup.stop(timeout=5)

    def test_stops_restarting_after_max_attempts(self):
        runs = []
        sup = self._sup(runs)
        for attempt in range(1, 6):
            assert sup.mark_crashed("demo", "boom") == attempt

        assert sup.revive_crashed(now=time.monotonic() + 3600) == 0
        assert runs == ["demo"]
        assert sup.health()["demo"]["crashed"] is True
        sup.stop(timeout=5)
