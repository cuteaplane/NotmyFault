"""规则并发调度

同一规则被反复触发时在这里决定新事件怎么办：single 丢弃、queue 排队、
replace 取消旧 run 换新的、parallel 直接并行。EventBus 只负责把事件匹配成
一次 run 请求，进不进执行由这里说了算；单次 run 的执行仍在 WorkflowExecutor。

排队条目在入队时冻结规则快照，热重载改了规则也不影响已排队的 run；
规则被删掉时排队的 run 丢弃并写 run history。
"""
import threading
from collections import deque
from typing import Any, Callable, Dict

MODES = ("single", "queue", "replace", "parallel")
DEFAULT_MODE = "parallel"
DEFAULT_MAX_CONCURRENCY = 1
DEFAULT_QUEUE_LIMIT = 20


def _concurrency_config(rule: Dict[str, Any]) -> Dict[str, Any]:
    raw = rule.get("concurrency") if isinstance(rule, dict) else None
    if not isinstance(raw, dict):
        return {"mode": DEFAULT_MODE}
    mode = raw.get("mode")
    if mode not in MODES:
        return {"mode": DEFAULT_MODE}
    config = {"mode": mode}
    for key, default in (
        ("max_concurrency", DEFAULT_MAX_CONCURRENCY),
        ("queue_limit", DEFAULT_QUEUE_LIMIT),
    ):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            config[key] = value
        else:
            config[key] = default
    return config


class RuleScheduler:
    """同一 rule_key 的 run 准入、排队和替换"""

    def __init__(
        self,
        execute_fn: Callable[[str, Dict[str, Any], str, Dict[str, Any]], None],
        cancel_run_fn: Callable[[str], bool],
        is_deferred_fn: Callable[[str], bool],
        on_history_event: Callable[[str, Dict[str, Any]], None] | None = None,
        spawn_thread_fn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._execute_fn = execute_fn
        self._cancel_run_fn = cancel_run_fn
        self._is_deferred_fn = is_deferred_fn
        self._on_history_event = on_history_event or (lambda kind, data: None)
        self._spawn_thread = spawn_thread_fn or self._spawn_daemon
        self._lock = threading.RLock()
        # rule_key -> 活跃 run_id 集合，deferred 的 run 也在里面
        self._active_runs: Dict[str, set[str]] = {}
        # rule_key -> 排队条目（规则快照 + context）
        self._queues: Dict[str, deque] = {}
        # rule_key -> queue 模式的并发配置，入队时的规则快照说了算
        self._queue_configs: Dict[str, Dict[str, Any]] = {}
        self._shutting_down = False

    @staticmethod
    def _spawn_daemon(target: Callable[[], None]) -> None:
        threading.Thread(target=target, daemon=True).start()

    def stats(self) -> Dict[str, Dict[str, int]]:
        """供状态接口展示每个规则的运行数和排队数"""
        with self._lock:
            return {
                rule_key: {
                    "running": len(runs),
                    "queued": len(self._queues.get(rule_key, ())),
                }
                for rule_key, runs in self._active_runs.items()
            }

    def submit(
        self,
        rule_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
    ) -> str:
        """决定一次 run 请求的去向，返回 started / queued / dropped / replaced"""
        run_id = str(context.get("run", {}).get("id", ""))
        config = _concurrency_config(rule)
        mode = config["mode"]
        run_now = False
        with self._lock:
            if self._shutting_down:
                self._emit_history("run_dropped", rule, run_id, rule_name, "引擎正在关闭")
                return "dropped"
            active = self._active_runs.setdefault(rule_key, set())

            if mode == "parallel":
                active.add(run_id)
                run_now = True
                decision = "started"

            elif mode == "single":
                if active:
                    self._emit_history("run_dropped", rule, run_id, rule_name, "已有运行中的 run")
                    return "dropped"
                active.add(run_id)
                run_now = True
                decision = "started"

            elif mode == "replace":
                for old_run_id in list(active):
                    self._emit_history("run_replaced", rule, old_run_id, rule_name, "被新触发的 run 替换")
                # 锁外再取消，cancel 回调可能反过来碰调度器
                to_cancel = list(active)
                active.clear()
                active.add(run_id)
                run_now = True
                decision = "replaced"

            else:
                queue = self._queues.setdefault(rule_key, deque())
                max_concurrency = config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY)
                if len(active) < max_concurrency:
                    active.add(run_id)
                    run_now = True
                    decision = "started"
                elif len(queue) >= config.get("queue_limit", DEFAULT_QUEUE_LIMIT):
                    self._emit_history("run_dropped", rule, run_id, rule_name, "排队已满")
                    return "dropped"
                else:
                    # 规则快照入队时冻结，热重载改规则不影响这条
                    frozen_rule = dict(rule)
                    queue.append((frozen_rule, rule_name, context, run_id))
                    self._queue_configs[rule_key] = config
                    self._emit_history("run_queued", rule, run_id, rule_name, f"排队中（第 {len(queue)} 个）")
                    return "queued"

        if mode == "replace":
            for old_run_id in to_cancel:
                self._cancel_with_retry(old_run_id)
        # 执行放锁外，长动作不能挡住别的规则提交
        if run_now:
            self._run_entry(rule_key, rule, rule_name, context, run_id)
        return decision

    def _cancel_with_retry(self, run_id: str) -> None:
        # 刚起跑的 run 要过一会儿才把取消事件登记进 executor，登记前 cancel 返回
        # False，这里重试等到登记或确认 run 已经结束
        import time

        for _ in range(20):
            try:
                if self._cancel_run_fn(run_id):
                    return
            except Exception:
                return
            time.sleep(0.005)

    def on_run_event(self, event_type: str, run_id: Any) -> None:
        """workflow 终态事件回来时把 run 从活跃集合里去掉并补发排队中的 run"""
        if event_type not in ("workflow_completed", "workflow_failed"):
            return
        if not isinstance(run_id, str) or not run_id:
            return
        with self._lock:
            rule_key = self._rule_key_of(run_id)
            if rule_key is None:
                return
            active = self._active_runs.get(rule_key)
            if active is None or run_id not in active:
                return
            active.discard(run_id)
            self._dispatch_queued_locked(rule_key)

    def drop_rule(self, rule_key: str) -> int:
        """规则被删掉时丢弃它的排队条目，返回丢弃数量"""
        with self._lock:
            queue = self._queues.pop(rule_key, None)
            self._queue_configs.pop(rule_key, None)
            if not queue:
                return 0
            count = len(queue)
            for rule, rule_name, context, run_id in list(queue):
                self._emit_history("run_dropped", rule, run_id, rule_name, "规则已删除")
            queue.clear()
            return count

    def shutdown(self) -> None:
        """引擎关闭：排队条目全部丢弃并写 run history"""
        with self._lock:
            self._shutting_down = True
            for rule_key in list(self._queues):
                queue = self._queues[rule_key]
                for rule, rule_name, context, run_id in list(queue):
                    self._emit_history("run_dropped", rule, run_id, rule_name, "引擎关闭")
                queue.clear()
            self._queues.clear()
            self._queue_configs.clear()

    def _rule_key_of(self, run_id: str) -> str | None:
        for rule_key, runs in self._active_runs.items():
            if run_id in runs:
                return rule_key
        return None

    def _dispatch_queued_locked(self, rule_key: str) -> None:
        queue = self._queues.get(rule_key)
        active = self._active_runs.get(rule_key)
        if not queue or active is None:
            return
        config = self._queue_configs.get(rule_key) or {}
        max_concurrency = config.get("max_concurrency", DEFAULT_MAX_CONCURRENCY)
        while queue and len(active) < max_concurrency:
            rule, rule_name, context, run_id = queue.popleft()

            def entry(rk=rule_key, r=rule, rn=rule_name, c=context, rid=run_id):
                self._run_entry(rk, r, rn, c, rid)

            active.add(run_id)
            self._spawn_thread(entry)

    def _run_entry(
        self,
        rule_key: str,
        rule: Dict[str, Any],
        rule_name: str,
        context: Dict[str, Any],
        run_id: str,
    ) -> None:
        # execute_fn 抛异常也必须把 run 从活跃集合去掉，
        # 否则这条规则以后 single 永远丢事件、queue 永远排队
        try:
            self._execute_fn(rule_key, rule, rule_name, context)
        except Exception:
            import traceback

            traceback.print_exc()
        finally:
            self._retire_if_done(rule_key, run_id)

    def _retire_if_done(self, rule_key: str, run_id: str) -> None:
        """execute_fn 返回后：deferred 的继续留在活跃集合等终态事件，其余直接退场"""
        if not run_id:
            return
        try:
            still_deferred = self._is_deferred_fn(run_id)
        except Exception:
            still_deferred = False
        if still_deferred:
            return
        with self._lock:
            active = self._active_runs.get(rule_key)
            if active is not None:
                active.discard(run_id)
            self._dispatch_queued_locked(rule_key)

    def _emit_history(
        self,
        kind: str,
        rule: Dict[str, Any],
        run_id: str,
        rule_name: str,
        reason: str,
    ) -> None:
        data: Dict[str, Any] = {
            "run_id": run_id,
            "rule_id": rule.get("rule_id", ""),
            "rule_name": rule_name,
            "reason": reason,
        }
        self._on_history_event(kind, data)
