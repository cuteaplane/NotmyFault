import threading

import pytest

from notmyfault.tests.api_support import create_test_engine
from notmyfault.core.workflow import build_context


def make_engine():
    engine = create_test_engine({"rules": []})
    engine._alert_user = lambda *a, **k: None
    return engine


def shorten_timeouts(engine, seconds=0.5):
    supervisor_stop = engine._trigger_supervisor.stop
    engine._stop_trigger_threads = lambda timeout=30: supervisor_stop(seconds)
    wait_actions = engine._workflow_executor.wait_active_actions
    engine._wait_active_actions = lambda timeout=60: wait_actions(seconds)


def start_trigger(engine, trigger_id, runner):
    def run_trigger_cb(instance_id, event_type, func, meta, config, stop_event):
        runner(stop_event)

    engine._trigger_supervisor.start(
        {trigger_id: [{}]},
        {trigger_id: lambda meta, config, emit, stop: None},
        {trigger_id: {}},
        run_trigger_cb,
    )


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

        def runner(stop_event):
            stuck.wait(timeout=10)

        start_trigger(engine, "fuzz_stuck", runner)
        engine.shutdown()
        stuck.set()
        assert engine._shutdown_clean is False

    def test_shutdown_with_stuck_action(self):
        engine = make_engine()
        shorten_timeouts(engine)
        release = threading.Event()

        def slow_action(meta, params):
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
        engine.shutdown()
        release.set()
        thread.join(timeout=5)
        assert engine._shutdown_clean is False

    def test_shutdown_cancels_deferred_workflow(self):
        engine = make_engine()
        shorten_timeouts(engine)
        rule = {"name": "延迟规则", "actions": []}
        context = build_context(rule["name"], "fuzz", {}, [])
        engine._defer_workflow("fuzz_key", rule, rule["name"], context, 60)
        assert "fuzz_key" in engine._deferred_workflows
        engine.shutdown()
        assert "fuzz_key" not in engine._deferred_workflows
        assert not engine._deferred_workflows

    def test_repeated_shutdown_calls_are_safe(self):
        engine = make_engine()
        shorten_timeouts(engine)
        start_trigger(engine, "fuzz_twice", lambda stop_event: stop_event.wait(5))
        engine.shutdown()
        engine.shutdown()
        assert engine._shutdown_clean is True
