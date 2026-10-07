from __future__ import annotations

import copy
import threading
import time
import uuid
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

MODES = ("single", "queue", "replace", "parallel")
DEFAULT_MODE = "single"
DEFAULT_MAX_CONCURRENCY = 1
DEFAULT_QUEUE_LIMIT = 20
DEFAULT_WORKERS = 8
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "timed_out", "dropped"})


def _concurrency_config(rule: dict[str, Any]) -> dict[str, Any]:
    raw = rule.get("concurrency", {})
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        mode = DEFAULT_MODE
    result = {"mode": mode, "queue_limit": DEFAULT_QUEUE_LIMIT}
    for key in ("max_concurrency", "queue_limit"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            result[key] = value
    if mode != "parallel":
        result.setdefault("max_concurrency", DEFAULT_MAX_CONCURRENCY)
    return result


@dataclass(frozen=True)
class RunOutcome:
    status: str = "succeeded"
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in TERMINAL_STATES - {"dropped"}:
            raise ValueError(f"无效的运行结果：{self.status}")


@dataclass
class _Run:
    id: str
    key: str
    rule: dict[str, Any]
    name: str
    context: dict[str, Any]
    cancel: threading.Event
    generation: Any = None
    done: threading.Event = field(default_factory=threading.Event)
    state: str = "queued"
    outcome: RunOutcome | None = None


class RunManager:
    def __init__(
        self,
        execute_fn: Callable[[str, dict, str, dict], RunOutcome | None],
        on_event: Callable[[str, dict], None] | None = None,
        *,
        max_workers: int = DEFAULT_WORKERS,
        max_pending_runs: int | None = None,
        spawn_thread_fn: Callable[[Callable[[], None]], Any] | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers 必须大于零")
        self._execute = execute_fn
        self._on_event = on_event or (lambda kind, data: None)
        self._max_workers = max_workers
        self._max_pending = max_pending_runs if max_pending_runs is not None else max(DEFAULT_QUEUE_LIMIT, max_workers * 4)
        self._executor = None
        if spawn_thread_fn is None:
            self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="RuleWorkflow")
            self._spawn = self._executor.submit
        else:
            self._spawn = spawn_thread_fn
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._runs: dict[str, _Run] = {}
        self._finished: OrderedDict[str, str] = OrderedDict()
        self._last_results: dict[str, str] = {}
        self._active: dict[str, set[str]] = {}
        self._queues: dict[str, deque[_Run]] = {}
        self._ready: deque[str] = deque()
        self._ready_set: set[str] = set()
        self._removed: set[str] = set()
        self._worker_count = 0
        self._closed = False
        self._generation = None
        self._effects: deque[Callable[[], Any]] = deque()
        self._draining = False
        self._drainer: int | None = None
        self._effects_done = threading.Condition(self._lock)

    def stats(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {key: {"running": len(self._active.get(key, ())), "queued": len(self._queues.get(key, ()))}
                    for key in self._active.keys() | self._queues.keys()}

    def state(self, run_id: str) -> str | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run.state if run is not None else self._finished.get(run_id)

    def rule_status(self, rule_id: str) -> dict:
        with self._lock:
            counts = {"running": 0, "queued": 0}
            for run in self._runs.values():
                identity = run.rule.get("rule_id", run.context.get("rule", {}).get("id", run.key))
                if identity == rule_id and run.state in counts:
                    counts[run.state] += 1
            return {**counts, "last_status": self._last_results.get(rule_id)}

    def submit(self, rule_key: str, rule: dict, rule_name: str, context: dict) -> str:
        run, decision = self._admit(rule_key, rule, rule_name, context)
        self._flush_effects()
        return decision

    def execute_sync(self, rule_key: str, rule: dict, rule_name: str, context: dict) -> RunOutcome | None:
        run, _ = self._admit(rule_key, rule, rule_name, context)
        self._flush_effects()
        run.done.wait()
        return run.outcome

    def _admit(self, key: str, rule: dict, name: str, context: dict) -> tuple[_Run, str]:
        run_id = context.setdefault("run", {}).setdefault("id", f"run_{uuid.uuid4().hex}")
        cancel = threading.Event()
        run = _Run(run_id, key, copy.deepcopy(rule), name, context, cancel, generation=context.get("_generation"))
        config = _concurrency_config(run.rule)
        with self._lock:
            if run_id in self._runs or run_id in self._finished:
                raise ValueError(f"运行 ID 已存在：{run_id}")
            if run.generation is not None:
                run.generation.retain()
            context["_run_cancel_event"] = cancel
            self._removed.discard(key)
            self._runs[run_id] = run
            self._event_locked("run.queued", run)
            queue = self._queues.setdefault(key, deque())
            active = self._active.setdefault(key, set())
            if self._closed:
                self._finish_locked(run, "dropped", {"reason": "引擎正在关闭"})
                return run, "dropped"
            reason = self._generation_drop_reason(run, self._generation)
            if reason:
                self._finish_locked(run, "dropped", {"reason": reason})
                return run, "dropped"
            if config["mode"] == "single" and (active or queue):
                self._finish_locked(run, "dropped", {"reason": "已有运行中的 run"})
                return run, "dropped"
            if config["mode"] == "replace":
                for old_id in active:
                    self._runs[old_id].cancel.set()
                while queue:
                    self._finish_locked(queue.popleft(), "dropped", {"reason": "被新触发的 run 替换"})
            can_start = not queue and self._worker_count < self._max_workers and len(active) < (1 if config["mode"] == "replace" else config.get("max_concurrency", self._max_workers))
            if can_start:
                self._start_locked(run)
                return run, "started"
            if len(queue) >= config["queue_limit"]:
                self._finish_locked(run, "dropped", {"reason": "排队已满"})
                return run, "dropped"
            if sum(len(items) for items in self._queues.values()) >= self._max_pending:
                self._finish_locked(run, "dropped", {"reason": "调度队列已满"})
                return run, "dropped"
            queue.append(run)
            self._mark_ready_locked(key)
            return run, "queued"

    @staticmethod
    def _generation_drop_reason(run, generation):
        if generation is None or run.context.get("_standalone_snapshot"):
            return None
        previous = (run.generation.snapshot.compiled.get(run.key) if run.generation is not None else
                    run.context.get("_compiled_rule"))
        if previous is None:
            return None
        current = generation.snapshot.compiled.get(run.key)
        if current is None:
            return "规则已删除"
        if current.digest != previous.digest:
            return "规则已修改"
        return None

    def activate_generation(self, generation):
        with self._lock:
            self._generation = generation
            for queue in self._queues.values():
                retained = []
                while queue:
                    run = queue.popleft()
                    reason = self._generation_drop_reason(run, generation)
                    if reason:
                        self._finish_locked(run, "dropped", {"reason": reason})
                    else:
                        retained.append(run)
                queue.extend(retained)

    def flush_events(self):
        self._flush_effects()

    def cancel_run(self, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.state in TERMINAL_STATES:
                return False
            run.cancel.set()
            if run.state == "queued":
                self._queues[run.key].remove(run)
                self._finish_locked(run, "cancelled", {"reason": "运行已取消"})
                self._dispatch_locked()
        self._flush_effects()
        return True

    def drop_rule(self, rule_key: str, reason: str = "规则已删除") -> int:
        with self._lock:
            self._removed.add(rule_key)
            queue = self._queues.pop(rule_key, deque())
            count = len(queue)
            while queue:
                self._finish_locked(queue.popleft(), "dropped", {"reason": reason})
            self._prune_locked(rule_key)
        self._flush_effects()
        return count

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for queue in self._queues.values():
                while queue:
                    self._finish_locked(queue.popleft(), "dropped", {"reason": "引擎关闭"})
            for run in self._runs.values():
                run.cancel.set()
            self._ready.clear()
            self._ready_set.clear()
            if self._executor is not None:
                self._effects.append(lambda: self._executor.shutdown(wait=False, cancel_futures=False))
        self._flush_effects()

    def wait_for_idle(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._idle:
            while self._worker_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
        self._flush_effects()
        return True

    def _event_locked(self, kind: str, run: _Run, data: dict | None = None, error: dict | None = None) -> None:
        payload = {"run_id": run.id, "rule_id": run.rule.get("rule_id", run.context.get("rule", {}).get("id", "")), "rule_name": run.name,
                   "status": run.state, **(data or {})}
        if run.generation is not None:
            payload["generation"] = run.generation.id
        if error is not None:
            payload["error"] = error
        self._effects.append(lambda: self._on_event(kind, payload))

    def _finish_locked(self, run: _Run, status: str, data: dict | None = None, error: dict | None = None) -> None:
        if run.state in TERMINAL_STATES:
            return
        run.state = status
        rule_id = run.rule.get("rule_id", run.context.get("rule", {}).get("id", run.key))
        self._last_results[rule_id] = status
        if status != "dropped":
            run.outcome = RunOutcome(status, data or {}, error)
        self._event_locked(f"run.{status}", run, data, error)
        self._runs.pop(run.id, None)
        self._finished[run.id] = status
        while len(self._finished) > 1000:
            self._finished.popitem(last=False)
        if run.generation is not None:
            self._effects.append(run.generation.release)
        self._effects.append(run.done.set)

    def _start_locked(self, run: _Run) -> None:
        run.state = "running"
        self._active.setdefault(run.key, set()).add(run.id)
        self._worker_count += 1
        self._event_locked("run.started", run)
        self._effects.append(lambda: self._launch(run))

    def _launch(self, run: _Run) -> None:
        try:
            self._spawn(lambda: self._work(run))
        except Exception:
            self._return_worker(run, RunOutcome("failed", error={"code": "worker_start_failed", "message": "无法启动工作线程"}))

    def _work(self, run: _Run) -> None:
        outcome = RunOutcome("failed", error={"code": "execution_failed", "message": "工作流执行异常"})
        try:
            if run.cancel.is_set():
                outcome = RunOutcome("cancelled")
            else:
                result = self._execute(run.key, run.rule, run.name, run.context)
                if result is not None and not isinstance(result, RunOutcome):
                    raise TypeError("工作流必须返回 RunOutcome")
                outcome = result or RunOutcome()
        except Exception:
            pass
        finally:
            self._return_worker(run, outcome)

    def _return_worker(self, run: _Run, outcome: RunOutcome) -> None:
        with self._idle:
            self._worker_count -= 1
            self._active[run.key].discard(run.id)
            if run.cancel.is_set():
                outcome = RunOutcome("cancelled", {"failure_kind": "cancelled"})
            self._finish_locked(run, outcome.status, outcome.data, outcome.error)
            self._dispatch_locked()
            self._prune_locked(run.key)
            self._idle.notify_all()
        self._flush_effects()

    def _prune_locked(self, key: str) -> None:
        if key in self._removed and not self._active.get(key) and not self._queues.get(key):
            self._active.pop(key, None)
            self._queues.pop(key, None)
            self._removed.discard(key)

    def _mark_ready_locked(self, key: str) -> None:
        if key not in self._ready_set:
            self._ready.append(key)
            self._ready_set.add(key)

    def _dispatch_locked(self) -> None:
        stalled = 0
        while not self._closed and self._ready and self._worker_count < self._max_workers:
            key = self._ready.popleft()
            self._ready_set.remove(key)
            queue = self._queues.get(key)
            if not queue:
                continue
            run = queue[0]
            config = _concurrency_config(run.rule)
            if len(self._active.get(key, ())) < (1 if config["mode"] == "replace" else config.get("max_concurrency", self._max_workers)):
                self._start_locked(queue.popleft())
                stalled = 0
            else:
                stalled += 1
            if queue:
                self._mark_ready_locked(key)
            if stalled >= len(self._ready):
                break

    def _flush_effects(self) -> None:
        with self._effects_done:
            if self._drainer == threading.get_ident():
                return
            while self._draining:
                self._effects_done.wait()
            self._draining = True
            self._drainer = threading.get_ident()
        while True:
            with self._effects_done:
                if not self._effects:
                    self._draining = False
                    self._drainer = None
                    self._effects_done.notify_all()
                    return
                effect = self._effects.popleft()
            try:
                effect()
            except Exception as error:
                from notmyfault.core.logging import engine_error

                engine_error("run_callback_failed", error=str(error))
