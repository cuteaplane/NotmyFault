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
    # args: instance_id, event_type, func, meta, config, stop_event
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
        assert set(sup.threads) == {"hotkey", "usb_insert"}
        sup.stop(timeout=5)

    def test_start_registers_before_thread_starts(self):
        sup = TriggerSupervisor()
        observed = {}

        def cb(instance_id, event_type, func, meta, config, stop_event):
            observed["registered"] = instance_id in sup.threads
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
        assert set(sup.threads) == {"hotkey"}
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
        assert sup.threads == {}

    def test_thread_start_failure_rolls_back_registration(self, monkeypatch):
        sup = TriggerSupervisor()

        def boom(self):
            raise RuntimeError("cannot start thread")

        monkeypatch.setattr(threading.Thread, "start", boom)
        with pytest.raises(RuntimeError):
            sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop)
        assert sup.threads == {}
        assert sup.events == {}


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
        assert all(evt.is_set() for evt in sup.events.values())
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
        assert sup.threads == {}


class TestStopRetention:
    def test_stop_empty_returns_true(self):
        assert TriggerSupervisor().stop(timeout=1) is True

    def test_stop_removes_exited_thread(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop)
        assert sup.stop(timeout=5) is True
        assert sup.threads == {}
        assert sup.events == {}

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
        assert "hotkey" in sup.threads
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
        assert set(sup.threads) == {"hotkey"}
        hold.set()
        assert sup.stop(timeout=5) is True

    def test_stop_real_thread_joins(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, _body_waits_on_stop)
        thread = sup.threads["hotkey"]
        assert sup.stop(timeout=5) is True
        assert not thread.is_alive()

    def test_repeated_stop_failures_keep_registration(self):
        sup = TriggerSupervisor()
        hold = threading.Event()
        self._start_stuck(sup, hold)
        assert sup.stop(timeout=0.05) is False
        assert sup.stop(timeout=0.05) is False
        assert "hotkey" in sup.threads
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
        assert _wait(lambda: not sup.threads["hotkey"].is_alive())
        sup.mark_crashed("hotkey", "boom")
        entry = sup.health()["hotkey"]
        assert entry["alive"] is False
        assert entry["crashed"] is True
        assert entry["last_error"] == "boom"

    def test_dead_no_crash_not_flagged(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, lambda *a: None)
        assert _wait(lambda: not sup.threads["hotkey"].is_alive())
        entry = sup.health()["hotkey"]
        assert entry["alive"] is False
        assert entry["crashed"] is False

    def test_last_crash_takes_latest(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, lambda *a: None)
        assert _wait(lambda: not sup.threads["hotkey"].is_alive())
        sup.mark_crashed("hotkey", "first")
        sup.mark_crashed("hotkey", "second")
        assert sup.health()["hotkey"]["last_error"] == "second"

    def test_new_generation_clears_previous_crash(self):
        sup = TriggerSupervisor()
        sup.start({"hotkey": [{}]}, {"hotkey": lambda *a: None}, {"hotkey": {}}, lambda *a: None)
        assert _wait(lambda: not sup.threads["hotkey"].is_alive())
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


class TestEngineCompat:
    def _engine(self):
        from notmyfault.tests.api_support import create_test_engine
        return create_test_engine({"rules": []})

    def test_property_proxy_same_object(self):
        engine = self._engine()
        assert engine._trigger_threads is engine._trigger_supervisor.threads
        assert engine._trigger_events is engine._trigger_supervisor.events
        assert engine._trigger_lock is engine._trigger_supervisor.lock

    def test_direct_assignment_propagates(self):
        engine = self._engine()
        new_threads = {"manual": object()}
        engine._trigger_threads = new_threads
        assert engine._trigger_supervisor.threads is new_threads
        new_events = {"manual": threading.Event()}
        engine._trigger_events = new_events
        assert engine._trigger_supervisor.events is new_events

    def test_get_diagnostics_has_triggers_health(self):
        engine = self._engine()
        diag = engine.get_diagnostics()
        assert "triggers" in diag
        assert diag["triggers"]["health"] == {}

    def test_get_diagnostics_health_reflects_crash(self):
        engine = self._engine()
        engine.triggers_funcs["hotkey"] = lambda meta, config, emit, stop_event: None
        engine.triggers_meta["hotkey"] = {}
        engine._start_trigger_threads([{"event": {"type": "hotkey", "params": {}}}])
        assert _wait(lambda: not engine._trigger_threads["hotkey"].is_alive())
        engine._trigger_supervisor.mark_crashed("hotkey", "boom")
        health = engine.get_diagnostics()["triggers"]["health"]
        assert health["hotkey"]["crashed"] is True

    def test_missing_trigger_alert_uses_latest_engine_callback(self):
        engine = self._engine()
        alerts = []
        # 构造后再替换告警回调，supervisor 仍应使用新回调
        engine._alert_user = lambda title, message, open_dashboard=False: alerts.append(title)
        engine._start_trigger_threads(
            [{"event": {"type": "ghost_trigger", "params": {}}}]
        )
        assert len(alerts) == 1

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
        engine._start_trigger_threads([{"event": {"type": "hotkey", "params": {}}}])
        assert "hotkey" in engine._trigger_threads
        assert engine._stop_trigger_threads(timeout=5) is True
        assert engine._trigger_threads == {}
