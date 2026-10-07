import threading
from types import SimpleNamespace

import pytest

from notmyfault.core.clock import FakeClock
from notmyfault.tests.test_trigger_host import apply, engine, instance, rule, wait_for
from notmyfault.triggers.base import PollingTrigger


def sampled(engine, cls):
    engine._trigger_host.clock = FakeClock()
    engine.triggers_meta["demo"]["sampling_api"] = "poll-v1"
    engine._plugin_modules["demo"] = SimpleNamespace(create_sampler=cls.from_context)


def test_shared_workers_keep_instance_state_and_do_not_overlap(engine):
    calls, active = {}, set()
    lock = threading.Lock()

    class Trigger(PollingTrigger):
        interval = 2

        def poll(self):
            value = self.config["value"]
            with lock:
                assert value not in active
                active.add(value)
                calls[value] = calls.get(value, 0) + 1
                active.remove(value)

    sampled(engine, Trigger)
    document = [rule(f"r_sample{index:04}", config={"value": index}) for index in range(30)]
    apply(engine, document)
    wait_for(lambda: len(calls) == 30)
    wait_for(lambda: all(item.sample_future.done() for item in engine._trigger_host._instances.values()))
    assert len([thread for thread in threading.enumerate() if thread.name.startswith("TriggerSampler-")]) == 4
    originals = dict(engine._trigger_host._instances)
    document[0]["name"] = "改名"
    apply(engine, document)
    assert engine._trigger_host._instances == originals
    engine._trigger_host.clock.advance(2)
    engine._trigger_host.service_timers()
    wait_for(lambda: all(value == 2 for value in calls.values()))
    assert len(calls) == 30


def test_sampling_failure_retries_after_teardown_and_stops_at_breaker(engine):
    calls = []

    class Trigger(PollingTrigger):
        def setup(self):
            calls.append("setup")

        def poll(self):
            raise ValueError("采样失败")

        def teardown(self):
            calls.append("teardown")

    sampled(engine, Trigger)
    host = engine._trigger_host
    host._jitter = lambda delay: 0
    apply(engine, [rule()])
    item = instance(engine)
    for count in range(1, 6):
        wait_for(lambda: len(item.failures) == count and item.sample_future.done())
        assert calls == ["setup", "teardown"] * count
        assert item.status == ("failed" if count == 5 else "backoff")
        if count < 5:
            host.clock.advance(item.retry_at - host.clock.monotonic())
            host.service_timers()
    host.clock.advance(60)
    host.service_timers()
    assert len(item.failures) == 5


def test_sampling_keeps_scheduled_interval_and_skips_missed_rounds(engine):
    calls = []

    class Trigger(PollingTrigger):
        interval = 1

        def poll(self):
            calls.append(self.clock.monotonic())
            self.clock.advance(.4 if len(calls) == 1 else 2.4)

    sampled(engine, Trigger)
    host = engine._trigger_host
    apply(engine, [rule()])
    item = instance(engine)
    wait_for(lambda: item.sample_future is not None and item.sample_future.done())
    assert calls == [0]
    assert item.sample_at == 1
    host.clock.advance(.6)
    host.service_timers()
    wait_for(lambda: item.sample_future.done() and len(calls) == 2)
    assert calls == [0, 1]
    assert item.sample_at == 4


def test_retry_rejects_emit_callback_saved_by_previous_sampler(engine):
    callbacks = []

    class Trigger(PollingTrigger):
        def setup(self):
            callbacks.append(self.ctx.emit)

        def poll(self):
            if len(callbacks) == 1:
                raise ValueError("首次采样失败")

    sampled(engine, Trigger)
    host = engine._trigger_host
    apply(engine, [rule()])
    item = instance(engine)
    wait_for(lambda: item.status == "backoff" and item.sample_future.done())
    host.clock.advance(item.retry_at - host.clock.monotonic())
    host.service_timers()
    wait_for(lambda: len(callbacks) == 2 and item.sample_future.done())
    assert not callbacks[0]({"value": 1})
    assert callbacks[1]({"value": 1})


def test_blocked_sample_is_zombie_and_rejects_emits_and_replacement(engine):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Trigger(PollingTrigger):
        def poll(self):
            entered.set()
            release.wait(3)
            calls.append(self.ctx.emit({"value": 1}))

        def teardown(self):
            calls.append("closed")

    sampled(engine, Trigger)
    apply(engine, [rule()])
    item = instance(engine)
    assert entered.wait(1)
    try:
        assert not engine._trigger_host._stop_instances([item], .01)
        assert item.status == "zombie"
        with pytest.raises(RuntimeError, match="尚未退出"):
            apply(engine, [rule()])
    finally:
        release.set()
    wait_for(lambda: item.sample_future.done())
    assert calls == [False, "closed"]


def test_partial_setup_and_invalid_interval_close_sampler(engine):
    calls = []

    class Trigger(PollingTrigger):
        def setup(self):
            calls.append("opened")
            raise ValueError("初始化失败")

        def teardown(self):
            calls.append("closed")

    sampled(engine, Trigger)
    apply(engine, [rule()])
    item = instance(engine)
    wait_for(lambda: item.status == "backoff" and item.sample_future.done())
    assert calls == ["opened", "closed"]


def test_partial_worker_start_keeps_old_generation_and_can_retry(engine, monkeypatch):
    class Trigger(PollingTrigger):
        def poll(self):
            pass

    old = apply(engine, [rule()])
    original = instance(engine)
    engine.triggers_meta["sampled"] = {"trigger_api": "event-v2", "sampling_api": "poll-v1", "outputs": []}
    engine.triggers_funcs["sampled"] = lambda *args: None
    engine._plugin_modules["sampled"] = SimpleNamespace(create_sampler=Trigger.from_context)
    start = threading.Thread.start

    def fail(thr):
        if thr.name == "TriggerSampler-2":
            raise RuntimeError("工作线程启动失败")
        return start(thr)

    document = [rule(), rule("r_sample_new", trigger="sampled")]
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail)
        with pytest.raises(RuntimeError, match="工作线程启动失败"):
            apply(engine, document)
    assert engine._generations.current is old
    assert instance(engine) is original
    assert not original.stop.is_set()
    apply(engine, document)
    sampled_instance = next(item for item in engine._trigger_host._instances.values() if item.key[0] == "sampled")
    wait_for(lambda: sampled_instance.sample_future is not None and sampled_instance.sample_future.done())
    assert len([thread for thread in threading.enumerate() if thread.name.startswith("TriggerSampler-")]) == 4


def test_pool_closes_threads_started_before_startup_failure(monkeypatch):
    from notmyfault.core.sampling_pool import SamplingPool

    pool = SamplingPool(lambda: None)
    start = threading.Thread.start

    def fail(thr):
        if thr.name == "TriggerSampler-2":
            raise RuntimeError("工作线程启动失败")
        return start(thr)

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail)
        with pytest.raises(RuntimeError):
            pool.start()
    assert pool.close(1)
    assert all(not thread.is_alive() for thread in pool._threads)


def test_hosted_path_samples_preserve_change_events_and_stop(engine, tmp_path):
    import json
    from pathlib import Path
    from notmyfault.triggers.path_exists.trigger import PathExistsTrigger

    meta = json.loads((Path(__file__).parents[1] / "triggers/path_exists/trigger.json").read_text(encoding="utf-8"))
    engine.triggers_meta["demo"] = meta
    sampled(engine, PathExistsTrigger)
    received = []
    host = engine._trigger_host
    host._dispatch = lambda kind, payload, instance: received.append(dict(payload))
    path = tmp_path / "sample.txt"
    config = {"path": str(path), "state": "changed", "interval": 1}
    apply(engine, [rule(config=config)])
    item = instance(engine, config)
    wait_for(lambda: item.sample_future is not None and item.sample_future.done())
    assert received == []
    path.write_text("ready", encoding="utf-8")
    host.clock.advance(1)
    host.service_timers()
    wait_for(lambda: item.emitted == 1)
    assert host.flush()
    path.unlink()
    wait_for(lambda: item.sample_future.done())
    host.clock.advance(1)
    host.service_timers()
    wait_for(lambda: item.emitted == 2)
    assert host.flush()
    assert [event["state"] for event in received] == ["exists", "missing"]
    apply(engine, [])
    wait_for(lambda: not host._instances)


def test_cpu_samples_do_not_depend_on_worker_thread(monkeypatch):
    from collections import namedtuple
    from notmyfault.triggers.system_resource.trigger import SystemResourceTrigger

    Times = namedtuple("Times", "user system idle")
    samples = iter([Times(10, 10, 80), Times(20, 20, 100)])
    monkeypatch.setattr("notmyfault.triggers.system_resource.trigger.psutil.cpu_times", lambda: next(samples))
    emitted = []
    trigger = SystemResourceTrigger({}, {"resource": "cpu"}, emitted.append, threading.Event())
    trigger.validate()
    trigger.setup()
    for _ in range(2):
        thread = threading.Thread(target=trigger.sample)
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
    assert emitted[0]["value"] == 50.0


def test_repeated_state_samples_reuse_validation_and_reject_type_changes(engine, tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from notmyfault.core import trigger_host
    from notmyfault.triggers.path_exists.trigger import PathExistsTrigger

    meta = json.loads((Path(__file__).parents[1] / "triggers/path_exists/trigger.json").read_text(encoding="utf-8"))
    engine.triggers_meta["demo"] = meta
    sampled(engine, PathExistsTrigger)
    host = engine._trigger_host
    config = {"path": str(tmp_path / "missing"), "state": "changed", "interval": 1}
    apply(engine, [rule(config=config)])
    item = instance(engine, config)
    wait_for(lambda: item.sample_future is not None and item.sample_future.done())
    calls = []
    original = trigger_host.check_payload_contract

    def check(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(trigger_host, "check_payload_contract", check)
    payload = {"path": config["path"], "state": "missing", "exists": False}
    assert host.emit(item, payload)
    assert calls == []
    assert not host.emit(item, {**payload, "exists": 0})
    assert calls == [True]
    assert host.emit(item, {**payload, "state": "exists", "exists": True})
    assert calls == [True, True]
    assert item.emitted == 1
