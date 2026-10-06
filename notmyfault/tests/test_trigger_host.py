import threading
import time

import pytest

from notmyfault.core.clock import FakeClock
from notmyfault.core.rules import config_fingerprint
from notmyfault.tests.api_support import create_test_engine


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    raise AssertionError("等待触发器状态超时")


def rule(identity="r_host0001", trigger="demo", config=None):
    return {"rule_id": identity, "name": identity, "condition": {"type": trigger, "params": config or {}}, "actions": []}


@pytest.fixture
def engine():
    engine = create_test_engine({"rules": []})
    engine.triggers_meta["demo"] = {"trigger_api": "event-v2", "outputs": [{"name": "value", "type": "number"}]}
    engine.triggers_funcs["demo"] = lambda meta, config, emit, stop: stop.wait()
    engine._alert_user = lambda *args, **kwargs: None
    yield engine
    engine.shutdown(timeout=3)


def apply(engine, rules):
    return engine._generations.apply(rules, "test", validate=False)


def instance(engine, config=None):
    return engine._trigger_host._instances[("demo", config_fingerprint(config or {}))]


def test_identity_shared_references_and_rename_keep_thread(engine):
    apply(engine, [rule(), rule("r_host0002")])
    original = instance(engine)
    wait_for(lambda: original.status == "running")
    renamed = rule()
    renamed["name"] = "重命名"
    generation = apply(engine, [renamed])
    assert instance(engine) is original
    assert original.generations == {generation.id: 1}
    assert original.status == "running"
    assert len(engine._trigger_host.health()) == 1
    assert next(iter(engine._trigger_host.health().values()))["references"] == 1


def test_partial_start_failure_rolls_back_only_added_threads(engine, monkeypatch):
    original_generation = apply(engine, [rule()])
    original = instance(engine)
    host = engine._trigger_host
    start = host._start_instance
    created = []

    def fail_third(item):
        start(item)
        created.append(item)
        if item.config.get("value") == 3:
            raise RuntimeError("不能启动线程")

    monkeypatch.setattr(host, "_start_instance", fail_third)
    with pytest.raises(RuntimeError, match="不能启动线程"):
        apply(engine, [rule(), rule("r_host0002", config={"value": 2}), rule("r_host0003", config={"value": 3})])
    assert engine._generations.current is original_generation
    assert instance(engine) is original
    assert not original.stop.is_set()
    assert created and all(not item.thread.is_alive() for item in created)
    assert len(host.health()) == 1


def test_payload_contract_and_nonblocking_bounded_emit(engine):
    entered, release = threading.Event(), threading.Event()
    received = []

    def dispatch(event_type, payload, instance):
        entered.set()
        release.wait(3)
        received.append((payload["value"], instance["seq"], instance["ts"]))

    host = engine._trigger_host
    host._dispatch = dispatch
    host._capacity = 2
    apply(engine, [rule()])
    item = instance(engine)
    wait_for(lambda: item.status == "running")
    try:
        assert not host.emit(item, {"value": "bad"})
        assert host.emit(item, {"value": 0})
        assert entered.wait(1)
        producer = threading.Thread(target=lambda: [host.emit(item, {"value": number}) for number in range(1, 6)])
        producer.start()
        producer.join(1)
        assert not producer.is_alive()
        assert item.dropped == 3
        assert len(item.queue) == 2
    finally:
        release.set()
    wait_for(lambda: len(received) == 3)
    assert [value for value, _, _ in received] == [0, 4, 5]
    assert [seq for _, seq, _ in received] == [1, 5, 6]


def test_coalesce_replaces_matching_queued_key(engine):
    entered, release = threading.Event(), threading.Event()
    host = engine._trigger_host
    host._dispatch = lambda *args, **kwargs: (entered.set(), release.wait(3))
    engine.triggers_meta["demo"].update(overflow="coalesce", coalesce_key="value")
    apply(engine, [rule()])
    item = instance(engine)
    wait_for(lambda: item.status == "running")
    try:
        host.emit(item, {"value": 0})
        assert entered.wait(1)
        host.emit(item, {"value": 1})
        host.emit(item, {"value": 2})
        host.emit(item, {"value": 1})
        assert len(item.queue) == 2
        assert item.dropped == 1
    finally:
        release.set()


def test_zombie_rejects_emits_and_blocks_same_identity(engine):
    entered, release = threading.Event(), threading.Event()
    engine.triggers_funcs["demo"] = lambda *args: (entered.set(), release.wait(3))
    apply(engine, [rule()])
    assert entered.wait(1)
    host = engine._trigger_host
    item = instance(engine)
    try:
        assert host.stop(0.01) is False
        assert item.status == "zombie"
        assert not host.emit(item, {"value": 1})
        assert item.rejected == 1
        with pytest.raises(RuntimeError, match="尚未退出"):
            apply(engine, [rule()])
        assert instance(engine) is item
    finally:
        release.set()
        item.thread.join(1)
    assert host.stop(1)


def test_return_is_failure_breaker_alerts_once_and_old_token_is_rejected(engine, monkeypatch):
    clock = FakeClock()
    host = engine._trigger_host
    host.clock = clock
    host._jitter = lambda delay: 0
    monkeypatch.setattr(host, "start", lambda: None)
    alerts, contexts = [], []
    host._alert = lambda *args: alerts.append(args)

    def returns(meta, config, emit, stop):
        contexts.append((emit, stop))

    engine.triggers_funcs["demo"] = returns
    apply(engine, [rule()])
    item = instance(engine)
    for index, delay in enumerate((1, 2, 4, 8)):
        wait_for(lambda: item.status == "backoff" and not item.thread.is_alive())
        assert item.retry_at == clock.monotonic() + delay
        clock.advance(delay)
        host.service_timers()
        wait_for(lambda: len(contexts) == index + 2)
        assert contexts[0][0]({"value": 1}) is False
    wait_for(lambda: item.status == "failed")
    assert len(item.failures) == 5
    assert len(alerts) == 1
    clock.advance(100)
    host.service_timers()
    assert len(contexts) == 5


def test_one_shot_return_completes_and_reload_keeps_completed_instance(engine):
    engine.triggers_meta["demo"]["lifecycle"] = "one_shot"
    engine.triggers_funcs["demo"] = lambda *args: None
    apply(engine, [rule()])
    item = instance(engine)
    wait_for(lambda: item.status == "stopped")
    apply(engine, [rule()])
    assert instance(engine) is item
    assert not item.failures


def test_state_baseline_and_debounce_are_persisted(engine):
    host = engine._trigger_host
    host.clock = FakeClock()
    engine.triggers_meta["demo"].update(semantic="state", state_key="value", debounce_seconds=1)
    seen = []
    host._dispatch = lambda _, payload, instance: seen.append(payload["value"])
    apply(engine, [rule()])
    item = instance(engine)
    wait_for(lambda: item.status == "running")
    host.emit(item, {"value": 0})
    host.emit(item, {"value": 1})
    host.clock.advance(0.5)
    host.emit(item, {"value": 1})
    host.clock.advance(0.5)
    host.service_timers()
    wait_for(lambda: seen == [1])
    assert item.state.get("baselines") == {"": 1}
    host.emit(item, {"value": 2})
    host.emit(item, {"value": 1})
    host.clock.advance(1)
    host.service_timers()
    assert seen == [1]


def test_running_generation_retains_removed_instance_until_work_finishes(engine):
    entered, release = threading.Event(), threading.Event()
    source = rule()
    source["actions"] = [{"type": "wait", "params": {}}]
    engine.actions_meta["wait"] = {}

    def action(*args):
        entered.set()
        release.wait(3)

    engine.actions_funcs["wait"] = action
    generation = apply(engine, [source])
    item = instance(engine)
    engine.emit_event("demo", {"value": 1}, instance={"config": {}})
    assert entered.wait(1)
    try:
        apply(engine, [])
        assert item.generations == {generation.id: 1}
        assert not item.stop.is_set()
        assert item.accepting is False
    finally:
        release.set()
    assert engine._run_manager.wait_for_idle(2)
    wait_for(lambda: item.stop.is_set() and not item.thread.is_alive())
    assert not item.generations


def test_dispatch_callback_failure_does_not_stop_producer_or_dispatcher(engine):
    host = engine._trigger_host
    received = []

    def dispatch(_, payload, instance):
        if payload["value"] == 0:
            raise RuntimeError("订阅者失败")
        received.append(payload["value"])

    host._dispatch = dispatch
    apply(engine, [rule()])
    item = instance(engine)
    wait_for(lambda: item.status == "running")
    assert host.emit(item, {"value": 0})
    assert host.emit(item, {"value": 1})
    assert host.flush(1)
    assert received == [1]
    assert item.thread.is_alive()


def test_system_startup_keeps_boot_marker_across_engine_restart():
    from notmyfault.tests.plugin_support import load_plugin
    import json
    from pathlib import Path

    module = load_plugin("triggers", "system_startup")
    meta = json.loads(Path(module.__file__).with_name("trigger.json").read_text())
    first = create_test_engine({"rules": []})
    second = None
    events = []
    source = rule(trigger="system_startup", config={"delay_seconds": 0})
    try:
        first.triggers_meta["system_startup"] = meta
        first.triggers_funcs["system_startup"] = module.run
        first._trigger_host._dispatch = lambda *args, **kwargs: events.append("first")
        apply(first, [source])
        wait_for(lambda: next(iter(first._trigger_host._instances.values())).status == "stopped")
        assert first._trigger_host.flush(1)
        assert events == ["first"]
        first.shutdown(2)
        second = create_test_engine({"rules": []}, rules_store=first._rules_store)
        second.triggers_meta["system_startup"] = meta
        second.triggers_funcs["system_startup"] = module.run
        second._trigger_host._dispatch = lambda *args, **kwargs: events.append("second")
        apply(second, [source])
        wait_for(lambda: next(iter(second._trigger_host._instances.values())).status == "stopped")
        assert second._trigger_host.flush(1)
        assert events == ["first"]
        assert first._trigger_host.boot_id == second._trigger_host.boot_id
    finally:
        first.shutdown(2)
        if second is not None:
            second.shutdown(2)
