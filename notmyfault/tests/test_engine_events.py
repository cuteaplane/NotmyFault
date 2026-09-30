"""引擎事件订阅：插件可监听规则运行终态"""

import threading

from notmyfault.core import engine_events
from notmyfault.plugin_api import engine_events_api
from notmyfault.tests.api_support import create_test_engine


def setup_function():
    engine_events.clear()


def teardown_function():
    engine_events.clear()


def test_subscribe_receives_published_events():
    seen = []
    token = engine_events.subscribe(lambda kind, data: seen.append((kind, dict(data))))
    engine_events.publish("workflow_completed", {"run_id": "r1", "rule_id": "a"})
    engine_events.unsubscribe(token)
    engine_events.publish("workflow_completed", {"run_id": "r2", "rule_id": "a"})
    assert seen == [("workflow_completed", {"run_id": "r1", "rule_id": "a"})]


def test_callback_exception_does_not_block_others():
    seen = []

    def boom(_kind, _data):
        raise RuntimeError("subscriber failed")

    engine_events.subscribe(boom)
    engine_events.subscribe(lambda kind, data: seen.append(kind))
    engine_events.publish("workflow_failed", {"run_id": "x"})
    assert seen == ["workflow_failed"]


def test_plugin_api_exposes_terminal_events():
    api = engine_events_api()
    assert "workflow_completed" in api.RULE_TERMINAL_EVENTS
    assert "workflow_failed" in api.RULE_TERMINAL_EVENTS
    assert "run_dropped" in api.RULE_TERMINAL_EVENTS
    assert "run_replaced" in api.RULE_TERMINAL_EVENTS


def test_engine_shutdown_clears_subscribers():
    engine_events.subscribe(lambda kind, data: None)
    engine = create_test_engine({"rules": []})
    assert engine_events.subscriber_count() == 1
    engine._shutdown_plugins()
    assert engine_events.subscriber_count() == 0


def test_engine_safe_on_event_publishes_to_subscribers():
    seen = []
    engine_events.subscribe(lambda kind, data: seen.append((kind, data.get("run_id"))))
    engine = create_test_engine({"rules": []})
    engine._safe_on_event(
        "workflow_completed",
        {
            "run_id": "run_1",
            "rule_id": "rule_a",
            "rule_name": "示例",
            "status": "succeeded",
        },
    )
    assert ("workflow_completed", "run_1") in seen
