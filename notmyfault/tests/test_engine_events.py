import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from notmyfault.core import engine_events
from notmyfault.core.event_stream import EventStream, EventType
from notmyfault.plugin_api import engine_events_api
from notmyfault.tests.api_support import create_test_engine


def test_frozen_envelope_and_order_across_publishers():
    stream = EventStream()
    received = []
    stream.subscribe(received.append, capacity=500)
    payload = {"run_id": "r1", "rule_id": "rule_a", "step_id": "step_a", "nested": {"values": [1]}}
    event = stream.publish(EventType.STEP_FAILED, {**payload, "error": {"code": "failed", "message": "执行失败", "location": "step_a"}})
    payload["nested"]["values"].append(2)
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda index: stream.publish("run.started", {"run_id": str(index)}), range(200)))
    assert stream.flush()
    assert [item.seq for item in received] == list(range(1, 202))
    assert event.to_dict() == {
        "v": 1, "seq": 1, "ts": event.ts, "type": "run.step.failed", "run_id": "r1",
        "rule_id": "rule_a", "step_id": "step_a", "data": {"nested": {"values": [1]}},
        "error": {"code": "failed", "message": "执行失败", "location": "step_a"},
    }
    with pytest.raises(TypeError):
        event.data["nested"]["values"] = ()
    with pytest.raises(ValueError):
        stream.publish("error", {})
    with pytest.raises(TypeError):
        stream.publish("run.failed", {"error": "失败"})
    stream.close()


@pytest.mark.parametrize("overflow", ["disconnect", "drop_oldest", "drop_newest"])
def test_slow_subscriber_is_bounded_and_does_not_block_publisher_or_other_subscribers(overflow):
    stream = EventStream()
    entered, release, delivered = threading.Event(), threading.Event(), threading.Event()
    seen = []

    def slow(event):
        seen.append(event.seq)
        entered.set()
        release.wait(5)

    subscription = stream.subscribe(slow, capacity=2, overflow=overflow)
    stream.subscribe(lambda event: delivered.set() if event.seq == 10 else None)
    try:
        stream.publish("run.started")
        assert entered.wait(2)
        for _ in range(9):
            stream.publish("run.started")
        assert delivered.wait(2)
        assert subscription.queue.qsize() <= 2
        assert subscription.dropped > 0
        assert subscription.closed == (overflow == "disconnect")
        release.set()
        assert stream.flush()
        if overflow == "disconnect":
            assert subscription.flush()
            assert seen == [1]
        elif overflow == "drop_oldest":
            assert seen == [1, 9, 10]
        else:
            assert seen == [1, 2, 3]
    finally:
        release.set()
        stream.close()


def test_subscriber_exception_and_unsubscribe_from_callback():
    stream = EventStream()
    seen = []
    stream.subscribe(lambda event: (_ for _ in ()).throw(RuntimeError("subscriber failed")))
    stream.subscribe(seen.append)
    stream.publish("run.failed")
    assert stream.flush()
    assert [event.type for event in seen] == [EventType.RUN_FAILED]
    ready = threading.Event()
    holder = {}

    def unsubscribe(event):
        stream.unsubscribe(holder["subscription"])
        ready.set()

    holder["subscription"] = stream.subscribe(unsubscribe)
    stream.publish("run.started")
    assert ready.wait(2)
    assert stream.flush()
    stream.close()


def test_plugin_api_captures_engine_stream_and_shutdown_closes_only_that_engine():
    first = create_test_engine({"rules": []})
    second = create_test_engine({"rules": []})
    seen = []
    with engine_events.use_stream(first.events):
        api = engine_events_api()
    subscription = api.subscribe(seen.append)
    first._safe_on_event("run.succeeded", {"run_id": "r1"})
    second._safe_on_event("run.succeeded", {"run_id": "r2"})
    assert first.events.flush()
    assert [event.run_id for event in seen] == ["r1"]
    assert "run.timed_out" in api.RULE_TERMINAL_EVENTS
    first._shutdown_plugins()
    assert subscription.closed
    assert second.events.publish("run.started") is not None
    second._shutdown_plugins()
