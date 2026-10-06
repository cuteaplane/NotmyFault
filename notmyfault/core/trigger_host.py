from __future__ import annotations

import collections
import hashlib
import random
import threading
from dataclasses import dataclass, field
from typing import Any

from notmyfault.core.clock import SystemClock
from notmyfault.core.compiled_rules import freeze, thaw
from notmyfault.core.engine_events import use_stream
from notmyfault.core.rules import config_fingerprint
from notmyfault.core.state_store import TriggerState
from notmyfault.core.trigger_state import StateDetector
from notmyfault.core.data_types import normalize_fields
from notmyfault.security.plugin_schema import check_payload_contract
from notmyfault.plugin_api import TriggerContext, use_trigger_context


@dataclass(eq=False)
class Instance:
    key: tuple[str, str]
    meta: dict
    config: Any
    function: Any
    version: str
    registry: Any
    state: TriggerState
    stop: threading.Event = field(default_factory=threading.Event)
    gate: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    status: str = "starting"
    accepting: bool = False
    generations: dict[int, int] = field(default_factory=dict)
    queue: collections.deque = field(default_factory=collections.deque)
    failures: collections.deque = field(default_factory=collections.deque)
    retry_at: float = 0.0
    alerted: bool = False
    last_error: str | None = None
    sequence: int = 0
    dropped: int = 0
    rejected: int = 0
    emitted: int = 0
    detector: StateDetector | None = None

    @property
    def id(self):
        return f"{self.key[0]}:{hashlib.sha256(self.key[1].encode()).hexdigest()}"


class Transition:
    def __init__(self, host, generation, desired, created):
        self.host, self.generation, self.desired, self.created = host, generation, desired, created

    def rollback(self):
        self.host._stop_instances(self.created, 2)
        self.host._apply_lock.release()

    def release(self):
        self.host.release_generation(self.generation.id)

    def commit(self):
        host = self.host
        try:
            with host._lock:
                stale = [instance for key, instance in host._instances.items() if self.desired.get(key) is not instance]
                for key, instance in self.desired.items():
                    instance.generations[self.generation.id] = self.generation.snapshot.subscription_counts[key]
                    host._instances[key] = instance
                    if instance.status not in ("zombie", "failed", "stopped"):
                        instance.accepting = True
                    instance.gate.set()
                for instance in stale:
                    instance.accepting = False
                    instance.queue.clear()
            host._wake.set()

        finally:
            host._apply_lock.release()


class TriggerHost:
    def __init__(self, dispatch, events, store, *, clock=None, alert=None, boot_id="", engine_started_at=0,
                 stop_timeout=5, jitter=None, queue_capacity=200, on_failure=None):
        self._dispatch = dispatch
        self.events = events
        self._store = store
        self.clock = clock or SystemClock()
        self._alert = alert or (lambda *args: None)
        self.boot_id = boot_id
        self.engine_started_at = engine_started_at
        self.stop_timeout = stop_timeout
        self._jitter = jitter or (lambda delay: random.uniform(0, delay * 0.2))
        if queue_capacity < 1:
            raise ValueError("事件队列容量必须大于零")
        self._on_failure = on_failure or (lambda identity, error: None)
        self._capacity = queue_capacity
        self._instances = {}
        self._lock = threading.RLock()
        self._dispatch_idle = threading.Condition(self._lock)
        self._dispatching = 0
        self._apply_lock = threading.RLock()
        self._ready = collections.deque()
        self._ready_keys = set()
        self._dispatch_wake = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._dispatcher = None
        self._timer = None

    def start(self):
        with self._apply_lock:
            if self._dispatcher is not None:
                return
            dispatcher = threading.Thread(target=self._dispatch_loop, name="TriggerDispatcher", daemon=True)
            timer = threading.Thread(target=self._timer_loop, name="TriggerRestarts", daemon=True)
            try:
                dispatcher.start()
                timer.start()
            except Exception:
                self._stop.set()
                self._dispatch_wake.set()
                self._wake.set()
                if dispatcher.ident is not None:
                    dispatcher.join(2)
                raise
            self._dispatcher, self._timer = dispatcher, timer

    def reconcile(self, generation):
        self.start()
        self._apply_lock.acquire()
        created, desired = [], {}
        try:
            snapshot = generation.snapshot
            counts = {}
            configs = {}
            for rule in snapshot.compiled.values():
                for subscription in rule.subscriptions:
                    key = (subscription.trigger_id, subscription.fingerprint)
                    counts[key] = counts.get(key, 0) + 1
            for trigger_id, items in snapshot.trigger_params.items():
                for config in items:
                    configs[(trigger_id, config_fingerprint(config))] = config
            snapshot.subscription_counts = counts
            for key in counts:
                trigger_id = key[0]
                meta = generation.plugins.triggers_meta[trigger_id]
                function = generation.plugins.triggers_funcs[trigger_id]
                version = generation.plugins.digests[f"trigger:{trigger_id}"]
                with self._lock:
                    existing = self._instances.get(key)
                if existing is not None and existing.version == version:
                    if existing.status == "zombie" and existing.thread.is_alive():
                        raise RuntimeError(f"触发器 {existing.id} 尚未退出")
                    if existing.status != "zombie" and (existing.status != "stopped" or existing.meta.get("lifecycle") == "one_shot"):
                        desired[key] = existing
                        continue
                if existing is not None and existing.thread.is_alive():
                    raise RuntimeError(f"触发器 {existing.id} 的旧版本尚未退出")
                instance = Instance(key, meta, freeze(configs[key]), function, version, snapshot.registry,
                                    TriggerState(self._store, f"{trigger_id}:{hashlib.sha256(key[1].encode()).hexdigest()}"))
                if meta.get("state_key"):
                    instance.detector = StateDetector(meta, instance.config, instance.state)
                desired[key] = instance
                created.append(instance)
                self._start_instance(instance)
            return Transition(self, generation, desired, created)
        except Exception:
            self._stop_instances(created, 2)
            self._apply_lock.release()
            raise

    def release_generation(self, generation_id):
        with self._lock:
            for instance in self._instances.values():
                instance.generations.pop(generation_id, None)
                if not instance.generations:
                    instance.accepting = False
                    instance.stop.set()
                    instance.queue.clear()
        self._wake.set()

    def _start_instance(self, instance):
        instance.stop = threading.Event()
        instance.gate = threading.Event()
        instance.status = "starting"
        instance.thread = threading.Thread(target=self._run, args=(instance,), name=f"Trigger-{instance.id[:35]}", daemon=True)
        instance.thread.start()

    def _run(self, instance):
        instance.gate.wait()
        if instance.stop.is_set():
            return
        with self._lock:
            instance.status = "running"
            instance.accepting = bool(instance.generations)
        instance_stop = instance.stop
        context = TriggerContext(instance.meta, instance.config, lambda payload: self.emit(instance, payload, token=instance_stop), instance.stop,
                                 instance.state, self.boot_id, self.engine_started_at, self.clock)
        self.events.publish("trigger.started", {"trigger_id": instance.key[0], "instance_id": instance.id})
        try:
            with use_stream(self.events), use_trigger_context(context):
                if instance.meta.get("trigger_api") != "event-v2":
                    raise ValueError("触发器必须使用 event-v2")
                instance.function(instance.meta, thaw(instance.config), context.emit, context.stopping)
            if not instance.stop.is_set() and instance.meta.get("lifecycle") != "one_shot":
                raise RuntimeError("持续运行的触发器提前返回")
        except Exception as error:
            if not instance.stop.is_set():
                self._failed(instance, error)
        else:
            with self._lock:
                if instance.status in ("running", "starting"):
                    instance.status = "stopped"
            self.events.publish("trigger.stopped", {"trigger_id": instance.key[0], "instance_id": instance.id})

    def _failed(self, instance, error):
        moment = self.clock.monotonic()
        with self._lock:
            while instance.failures and instance.failures[0] < moment - 300:
                instance.failures.popleft()
            instance.failures.append(moment)
            instance.accepting = False
            instance.stop.set()
            instance.last_error = type(error).__name__
            instance.status = "failed" if len(instance.failures) >= 5 else "backoff"
            delay = min(60, 2 ** (len(instance.failures) - 1))
            instance.retry_at = moment + min(60, delay + self._jitter(delay))
            alert = instance.status == "failed" and not instance.alerted
            instance.alerted |= alert
        self._on_failure(instance.id, type(error).__name__)
        self.events.publish("trigger.crashed", {"trigger_id": instance.key[0], "instance_id": instance.id,
                            "error": {"code": "trigger_failed", "message": "触发器异常退出"}})
        self.events.publish("trigger.failed" if alert else "trigger.backoff", {"trigger_id": instance.key[0], "instance_id": instance.id, "retry_at": instance.retry_at, "failures": len(instance.failures)})
        if alert:
            self._alert("触发器已停止重试", f"{instance.key[0]} 在 5 分钟内失败 5 次，请检查插件或配置")
        self._wake.set()

    def emit(self, instance, payload, *, token=None):
        with self._lock:
            if (token is not None and token is not instance.stop) or not instance.accepting or instance.stop.is_set() or not instance.generations:
                instance.rejected += 1
                return False
        problems = check_payload_contract(instance.meta.get("outputs"), payload, instance.registry) if isinstance(payload, dict) else ["事件 payload 必须是对象"]
        if problems:
            self.events.publish("trigger.payload_invalid", {"trigger_id": instance.key[0], "instance_id": instance.id, "problems": problems})
            return False
        value = freeze(normalize_fields(payload, instance.meta.get("outputs"), instance.registry))
        with self._lock:
            if (token is not None and token is not instance.stop) or not instance.accepting or instance.stop.is_set():
                instance.rejected += 1
                return False
            if instance.detector is not None:
                for event in instance.detector.offer(value, self.clock.monotonic()):
                    self._enqueue(instance, event)
                if instance.detector.deadline is not None:
                    self._wake.set()
            else:
                self._enqueue(instance, value)
        return True

    def _enqueue(self, instance, value):
        instance.sequence += 1
        item = (value, instance.sequence, self.clock.time())
        coalesce = instance.meta.get("overflow") == "coalesce"
        key = instance.meta.get("coalesce_key")
        if coalesce and key:
            for index, previous in enumerate(instance.queue):
                if previous[0].get(key) == value.get(key):
                    instance.queue[index] = item
                    instance.dropped += 1
                    break
            else:
                instance.queue.append(item)
        else:
            instance.queue.append(item)
        capacity = min(self._capacity, int(instance.meta.get("event_queue_size", self._capacity)))
        if len(instance.queue) > capacity:
            instance.queue.popleft()
            instance.dropped += 1
        instance.emitted += 1
        if instance not in self._ready_keys:
            self._ready_keys.add(instance)
            self._ready.append(instance)
        self._dispatch_wake.set()

    def _dispatch_loop(self):
        while not self._stop.is_set():
            self._dispatch_wake.wait()
            with self._lock:
                if not self._ready:
                    self._dispatch_wake.clear()
                    continue
                instance = self._ready.popleft()
                self._ready_keys.remove(instance)
                if not instance.queue or not instance.accepting:
                    continue
                payload, seq, ts = instance.queue.popleft()
                if instance.queue:
                    self._ready.append(instance)
                    self._ready_keys.add(instance)
                self._dispatching += 1
                token = {"config": instance.config, "fingerprint": instance.key[1], "seq": seq, "ts": ts}
            try:
                self._dispatch(instance.key[0], payload, instance=token)
            except Exception as error:
                self.events.publish("trigger.dispatch_failed", {"trigger_id": instance.key[0], "instance_id": instance.id,
                                    "error": {"code": "trigger_dispatch_failed", "message": type(error).__name__}})
            finally:
                with self._dispatch_idle:
                    self._dispatching -= 1
                    self._dispatch_idle.notify_all()

    def flush(self, timeout=5):
        import time

        deadline = time.monotonic() + timeout
        with self._dispatch_idle:
            while self._dispatching or any(item.queue for item in self._ready):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._dispatch_idle.wait(remaining)
        return True

    def service_timers(self):
        errors = []
        now = self.clock.monotonic()
        with self._apply_lock:
            with self._lock:
                instances = list(self._instances.values())
                for instance in instances:
                    if instance.status == "zombie" and not instance.thread.is_alive():
                        instance.status = "stopped"
                retired = [instance for instance in instances if not instance.generations and instance.status != "zombie"]
                restart = [instance for instance in instances if instance.status == "backoff" and instance.generations
                           and instance.retry_at <= now and not instance.thread.is_alive()]
                for instance in instances:
                    if instance.detector is not None and instance.accepting:
                        try:
                            for event in instance.detector.due(now):
                                self._enqueue(instance, event)
                        except Exception as error:
                            instance.accepting = False
                            errors.append((instance, error))
            self._stop_instances(retired, self.stop_timeout)
            for instance in restart:
                try:
                    self._start_instance(instance)
                    instance.gate.set()
                except Exception as error:
                    errors.append((instance, error))
        for instance, error in errors:
            self._failed(instance, error)

    def _timer_loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            self.service_timers()
            with self._lock:
                deadlines = [instance.retry_at for instance in self._instances.values() if instance.status == "backoff"]
                deadlines += [instance.detector.deadline for instance in self._instances.values() if instance.detector is not None and instance.detector.deadline is not None and instance.accepting]
                zombies = any(instance.status == "zombie" for instance in self._instances.values())
            timeout = max(0.01, min(deadlines) - self.clock.monotonic()) if deadlines else None
            if zombies:
                timeout = min(timeout, 0.1) if timeout is not None else 0.1
            self.clock.wait(self._wake, timeout)

    def _stop_instances(self, instances, timeout):
        for instance in instances:
            with self._lock:
                instance.accepting = False
                instance.queue.clear()
                instance.stop.set()
                instance.gate.set()
        deadline = self.clock.monotonic() + timeout
        for instance in instances:
            thread = instance.thread
            if thread is not None and thread.ident is not None and thread is not threading.current_thread():
                thread.join(max(0, deadline - self.clock.monotonic()))
            with self._lock:
                instance.status = "zombie" if thread is not None and thread.is_alive() else "stopped"
                zombie = instance.status == "zombie"
                if instance.status == "stopped" and self._instances.get(instance.key) is instance and not instance.generations:
                    self._instances.pop(instance.key, None)
            if zombie:
                self.events.publish("trigger.zombie", {"trigger_id": instance.key[0], "instance_id": instance.id})
        return all(instance.status != "zombie" for instance in instances)

    def request_stop_all(self):
        with self._lock:
            for instance in self._instances.values():
                instance.accepting = False
                instance.stop.set()
                instance.gate.set()

    def stop(self, timeout=30):
        self.request_stop_all()
        self._stop.set()
        self._wake.set()
        self._dispatch_wake.set()
        with self._apply_lock:
            with self._lock:
                instances = list(self._instances.values())
                for instance in instances:
                    instance.generations.clear()
            clean = self._stop_instances(instances, timeout)
        for thread in (self._dispatcher, self._timer):
            if thread is not None and thread is not threading.current_thread():
                thread.join(min(2, timeout))
                clean &= not thread.is_alive()
        return clean

    def health(self):
        with self._lock:
            return {instance.id: {"trigger_id": instance.key[0], "fingerprint": instance.key[1], "config": thaw(instance.config),
                    "alive": instance.thread.is_alive(), "status": instance.status, "stuck": instance.status == "zombie",
                    "crashed": instance.status in ("backoff", "failed"), "last_error": instance.last_error,
                    "references": sum(instance.generations.values()), "generations": dict(instance.generations),
                    "queued": len(instance.queue), "emitted": instance.emitted, "dropped": instance.dropped,
                    "rejected": instance.rejected} for instance in self._instances.values()}
