import threading
import time
from types import SimpleNamespace

from notmyfault.tests.api_support import create_test_engine
from notmyfault.tests.canvas_fixtures import trigger_rule
from notmyfault.core.workflow import build_context


def make_engine():
    engine = create_test_engine({"rules": []})
    engine._alert_user = lambda *a, **k: None
    return engine


def shorten_timeouts(engine, seconds=0.5):
    supervisor_stop = engine._trigger_host.stop
    engine._stop_trigger_threads = lambda timeout=30: supervisor_stop(seconds)
    wait_actions = engine._workflow_executor.wait_active_actions
    engine._wait_active_actions = lambda timeout=60: wait_actions(seconds)


def start_trigger(engine, trigger_id, runner):
    engine.triggers_funcs[trigger_id] = lambda meta, config, emit, stop: runner(stop)
    engine.triggers_meta[trigger_id] = {"trigger_api": "event-v2"}
    engine._generations.apply([trigger_rule("r_fuzz0001", "停止测试", trigger_id)], "test", validate=False)


class TestShutdownFuzz:
    def test_shutdown_with_running_trigger(self):
        engine = make_engine()
        shorten_timeouts(engine)
        polling = threading.Event()
        exited = threading.Event()

        def runner(stop_event):
            polling.set()
            stop_event.wait(timeout=10)
            exited.set()

        start_trigger(engine, "fuzz_poll", runner)
        assert polling.wait(timeout=5)
        engine.shutdown()
        assert exited.is_set()
        assert engine._shutdown_clean is True

    def test_shutdown_with_stuck_trigger(self):
        engine = make_engine()
        shorten_timeouts(engine)
        stuck = threading.Event()
        entered = threading.Event()

        def runner(stop_event):
            entered.set()
            stuck.wait(timeout=10)

        start_trigger(engine, "fuzz_stuck", runner)
        assert entered.wait(1)
        engine.shutdown()
        stuck.set()
        assert engine._shutdown_clean is False

    def test_shutdown_with_stuck_action(self):
        engine = make_engine()
        shorten_timeouts(engine)
        release = threading.Event()
        entered = threading.Event()
        teardown = []
        engine._plugin_modules["fuzz_slow"] = SimpleNamespace(teardown=lambda: teardown.append(True))

        def slow_action(meta, params):
            entered.set()
            release.wait(timeout=10)

        engine.actions_funcs["fuzz_slow"] = slow_action
        engine.actions_meta["fuzz_slow"] = {}
        context = build_context("规则", "fuzz", {}, [])
        thread = threading.Thread(
            target=lambda: engine._run_action(
                {"type": "fuzz_slow", "params": {}}, "规则", context
            ),
            daemon=True,
        )
        thread.start()
        assert entered.wait(5)
        engine.shutdown()
        assert thread.is_alive()
        assert not release.is_set()
        assert not teardown
        release.set()
        thread.join(timeout=5)
        assert engine._shutdown_clean is False
        engine.shutdown()
        assert engine._shutdown_clean is True
        assert teardown == [True]

    def test_repeated_shutdown_calls_are_safe(self):
        engine = make_engine()
        shorten_timeouts(engine)
        start_trigger(engine, "fuzz_twice", lambda stop_event: stop_event.wait(5))
        engine.shutdown()
        engine.shutdown()
        assert engine._shutdown_clean is True
