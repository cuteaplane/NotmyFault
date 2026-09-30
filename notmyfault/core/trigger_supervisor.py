"""统一管理触发器线程、停止事件、崩溃和健康状态。"""
import sys
import threading
import time
from notmyfault.core.rules import config_fingerprint

# 连续两次 join() 超时后只在 health() 中标记卡住，旧线程仍运行时不能启动新线程
_STUCK_THRESHOLD = 2
# 崩溃后重启的等待时间，按次数往后取，最后固定 60 秒
_CRASH_BACKOFFS = (2.0, 5.0, 15.0, 60.0)
# 连续崩到这个次数就不再自动重启，等下次保存规则或重启引擎
_MAX_CRASH_RESTARTS = 4
# 实例活过这么久再崩算一次新故障，等待时间从头算
_CRASH_RESET_SECONDS = 60.0
from typing import Any, Callable, Dict, List, Optional, Tuple


class TriggerSupervisor:
    """触发器线程管理：启动、停止、记录崩溃并报告健康状态。"""

    def __init__(
        self,
        alert_cb: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._alert_cb = alert_cb
        self._threads: Dict[str, threading.Thread] = {}
        self._events: Dict[str, threading.Event] = {}
        self._crash_errors: Dict[str, str] = {}
        self._stop_failures: Dict[str, int] = {}  # 连续退出失败次数，用于判断卡住
        self._configs: Dict[str, Dict[str, Any]] = {}  # 实例 ID 到配置快照，用于健康状态
        self._fingerprints: Dict[str, str] = {}  # 实例 ID 到配置指纹，重载时用来判断配置有没有变
        self._event_types: Dict[str, str] = {}  # 实例 ID 到触发器类型
        self._specs: Dict[str, Tuple[str, Any, str, Dict[str, Any], Any]] = {}  # 实例 ID 到启动参数
        self._started_at: Dict[str, float] = {}  # 实例 ID 到启动时刻，用来判断是不是老故障
        # (触发器类型, 配置指纹) 到连续失败次数和下次重试时刻
        self._crash_retries: Dict[Tuple[str, str], Tuple[int, float]] = {}
        self._last_counts: Dict[str, int] = {}
        self._last_run_cb: Optional[Callable[..., Any]] = None
        self._lock = threading.RLock()
        # 启停锁覆盖 thread.start() 到 join() 的完整过程，stop() 只能处理已经挂载的线程
        self._lifecycle_lock = threading.RLock()

    def start(
        self,
        aggregated: Dict[str, List[Dict[str, Any]]],
        triggers_funcs: Dict[str, Any],
        triggers_meta: Dict[str, Dict[str, Any]],
        run_trigger_cb: Callable[..., Any],
        *,
        stop_timeout: float = 5.0,
    ) -> int:
        """按配置指纹决定线程去留：配置没变的保留，变了或被删的停掉，缺的启动"""
        desired, counts, missing = self._desired_instances(
            aggregated, triggers_funcs, triggers_meta
        )
        if missing:
            print(
                f"[Engine] [!!] 规则引用了未加载的触发器: {', '.join(missing)}",
                file=sys.stderr,
            )
            if self._alert_cb:
                self._alert_cb(
                    "触发器缺失",
                    f"以下触发器未装载，相关规则不会生效: {', '.join(missing)}",
                )

        with self._lifecycle_lock:
            self._last_counts = counts
            self._last_run_cb = run_trigger_cb
            self._stop_undesired(desired, stop_timeout)
            return self._start_missing(desired, counts, run_trigger_cb)

    def _desired_instances(
        self,
        aggregated: Dict[str, List[Dict[str, Any]]],
        triggers_funcs: Dict[str, Any],
        triggers_meta: Dict[str, Dict[str, Any]],
    ) -> Tuple[List[Tuple[str, Any, str, Dict[str, Any], Any]], Dict[str, int], List[str]]:
        """算出这次要跑哪些实例，每项带配置指纹，用来和已在运行的线程比对"""
        desired: List[Tuple[str, Any, str, Dict[str, Any], Any]] = []
        counts: Dict[str, int] = {}
        missing: List[str] = []
        for event_type, config_list in aggregated.items():
            if event_type not in triggers_funcs:
                missing.append(event_type)
                continue
            trigger_meta = triggers_meta.get(event_type, {})
            trigger_func = triggers_funcs[event_type]
            if trigger_meta.get("trigger_api") == "event-v2":
                # 相同指纹的配置只跑一个实例，事件仍能匹配所有叶子
                skipped = 0
                seen_fingerprints = set()
                instances = []
                for cfg in config_list:
                    fingerprint = config_fingerprint(cfg)
                    if fingerprint in seen_fingerprints:
                        skipped += 1
                        continue
                    seen_fingerprints.add(fingerprint)
                    instances.append((cfg, fingerprint))
                if skipped:
                    print(
                        f"[Engine] 触发器 {event_type} 有 {skipped} 个重复配置"
                        "（相同指纹）已合并，只启动一个实例",
                        file=sys.stderr,
                    )
            else:
                instances = [(config_list, config_fingerprint(config_list))]
            counts[event_type] = len(instances)
            for config, fingerprint in instances:
                desired.append((event_type, config, fingerprint, trigger_meta, trigger_func))
        return desired, counts, missing

    def _stop_undesired(
        self,
        desired: List[Tuple[str, Any, str, Dict[str, Any], Any]],
        timeout: float,
    ) -> None:
        """停掉配置已经变了或已经不存在的实例，卡住的留在字典里给健康状态报告"""
        wanted = {(event_type, fingerprint) for event_type, _cfg, fingerprint, _meta, _func in desired}
        with self._lock:
            stale = [
                (instance_id, thread)
                for instance_id, thread in self._threads.items()
                if (
                    self._event_types.get(instance_id),
                    self._fingerprints.get(instance_id),
                )
                not in wanted
            ]
        if not stale:
            return

        for instance_id, _thread in stale:
            with self._lock:
                event = self._events.get(instance_id)
            if event is not None:
                event.set()

        deadline = time.monotonic() + timeout
        for instance_id, thread in stale:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                thread.join(timeout=remaining)
            with self._lock:
                if thread.is_alive():
                    failures = self._stop_failures.get(instance_id, 0) + 1
                    self._stop_failures[instance_id] = failures
                    print(
                        f"[Engine] [!!] 触发器线程 {instance_id} 未在 {timeout:g}s 内退出"
                        f"（连续 {failures} 次），新配置会用新的实例 ID 启动",
                        file=sys.stderr,
                    )
                    continue
                key = (
                    self._event_types.get(instance_id),
                    self._fingerprints.get(instance_id),
                )
                self._forget(instance_id, thread)
                self._crash_retries.pop(key, None)
            print(f"[Engine] 已停止触发器线程: {instance_id}")

    def _start_missing(
        self,
        desired: List[Tuple[str, Any, str, Dict[str, Any], Any]],
        counts: Dict[str, int],
        run_trigger_cb: Callable[..., Any],
    ) -> int:
        """启动还没有在跑的实例，先挂载线程再调用 start()"""
        count = 0
        for event_type, config, fingerprint, trigger_meta, trigger_func in desired:
            with self._lock:
                running = False
                dead: Optional[Tuple[str, threading.Thread]] = None
                for instance_id, thread in self._threads.items():
                    if (
                        self._event_types.get(instance_id),
                        self._fingerprints.get(instance_id),
                    ) != (event_type, fingerprint):
                        continue
                    if thread.is_alive():
                        running = True
                        break
                    dead = (instance_id, thread)
            if running:
                continue
            if dead is not None:
                # 同一个配置的上一条线程已经退出（崩溃或正常返回），放掉它的 ID 再启用新的
                self._forget(*dead)

            instance_id = self._next_instance_id(event_type, counts.get(event_type, 1) > 1)
            trigger_event = threading.Event()
            thread = threading.Thread(
                target=run_trigger_cb,
                args=(
                    instance_id,
                    event_type,
                    trigger_func,
                    trigger_meta,
                    config,
                    trigger_event,
                ),
                daemon=True,
            )
            with self._lock:
                self._events[instance_id] = trigger_event
                self._configs[instance_id] = config
                self._fingerprints[instance_id] = fingerprint
                self._event_types[instance_id] = event_type
                self._specs[instance_id] = (
                    event_type,
                    config,
                    fingerprint,
                    trigger_meta,
                    trigger_func,
                )
                self._started_at[instance_id] = time.monotonic()
                self._stop_failures.pop(instance_id, None)
                self._threads[instance_id] = thread
                self._crash_errors.pop(instance_id, None)
                try:
                    thread.start()
                except Exception as error:
                    self._forget(instance_id, thread)
                    print(f"[Engine] 触发器 {instance_id} 启动失败: {error}", file=sys.stderr)
                    if self._alert_cb:
                        self._alert_cb("触发器启动失败", f"{instance_id}: {type(error).__name__}")
                    continue
            count += 1
            print(f"[Engine] 已启动触发器线程: {instance_id}")
        return count

    def _next_instance_id(self, event_type: str, multiple: bool) -> str:
        """挑一个没被占用的实例 ID；被卡住的旧线程占着就往后排"""
        with self._lock:
            taken = {
                instance_id
                for instance_id in self._threads
                if instance_id == event_type or instance_id.startswith(f"{event_type}:")
            }
        if not multiple and event_type not in taken:
            return event_type
        index = 1
        while f"{event_type}:{index}" in taken:
            index += 1
        return f"{event_type}:{index}"

    def _forget(self, instance_id: str, thread: threading.Thread) -> None:
        """清掉这个实例的全部记录，只清仍然属于该线程的条目"""
        with self._lock:
            if self._threads.get(instance_id) is not thread:
                return
            self._threads.pop(instance_id, None)
            self._events.pop(instance_id, None)
            self._configs.pop(instance_id, None)
            self._crash_errors.pop(instance_id, None)
            self._stop_failures.pop(instance_id, None)
            self._fingerprints.pop(instance_id, None)
            self._event_types.pop(instance_id, None)
            self._specs.pop(instance_id, None)
            self._started_at.pop(instance_id, None)

    def stop(self, timeout: float = 30.0) -> bool:
        """广播停止信号并等待线程退出，返回是否全部停止"""
        with self._lifecycle_lock:
            with self._lock:
                if not self._threads:
                    return True
                events = list(self._events.values())
                threads = list(self._threads.items())

            # 先给线程发停止信号，再按同一个截止时间调用 join()
            for evt in events:
                evt.set()

            deadline = time.monotonic() + timeout
            for instance_id, thread in threads:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    thread.join(timeout=remaining)
                if thread.is_alive():
                    # _stop_failures 的读写统一在 _lock 内，health() 也用它
                    with self._lock:
                        failures = self._stop_failures.get(instance_id, 0) + 1
                        self._stop_failures[instance_id] = failures
                    print(
                        f"[Engine] [!!] 触发器线程 {instance_id}"
                        f" 未在 {timeout}s 内退出（连续 {failures} 次）",
                        file=sys.stderr,
                    )
                else:
                    with self._lock:
                        self._stop_failures.pop(instance_id, None)

            alive = {et for et, thread in threads if thread.is_alive()}
            remaining_alive = set(alive)
            stopped_count = len(threads) - len(alive)
            for instance_id, thread in threads:
                # 只删除仍对应当前线程对象的条目
                if instance_id not in alive:
                    self._forget(instance_id, thread)
            if stopped_count:
                print(f"[Engine] 已停止 {stopped_count} 个触发器线程")
            return not remaining_alive

    def request_stop_all(self) -> None:
        """向所有已挂载线程发送停止信号"""
        with self._lifecycle_lock:
            with self._lock:
                events = list(self._events.values())
            for evt in events:
                evt.set()

    def mark_crashed(self, trigger_id: str, error: str) -> int:
        """记录崩溃并算好下次重试时刻，返回这是连续第几次失败"""
        moment = time.monotonic()
        with self._lock:
            if trigger_id in self._threads:
                self._crash_errors[trigger_id] = error[:300]
            spec = self._specs.get(trigger_id)
            if spec is None:
                return 0
            key = (spec[0], spec[2])
            attempts = self._crash_retries.get(key, (0, 0.0))[0]
            if moment - self._started_at.get(trigger_id, 0.0) > _CRASH_RESET_SECONDS:
                attempts = 0
            attempts += 1
            delay = _CRASH_BACKOFFS[min(attempts, len(_CRASH_BACKOFFS)) - 1]
            self._crash_retries[key] = (attempts, moment + delay)
            if attempts > _MAX_CRASH_RESTARTS:
                print(
                    f"[Engine] [!!] 触发器 {trigger_id} 连续崩溃 {attempts} 次，"
                    "不再自动重启",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[Engine] 触发器 {trigger_id} 第 {attempts} 次崩溃，"
                    f"{delay:g} 秒后自动重启",
                    file=sys.stderr,
                )
            return attempts

    def revive_crashed(self, now: Optional[float] = None) -> int:
        """把到点的崩溃实例重新拉起来，没到退避时间的跳过"""
        moment = time.monotonic() if now is None else now
        with self._lock:
            callback = self._last_run_cb
            if callback is None:
                return 0
            counts = dict(self._last_counts)
            due: List[Tuple[str, Any, str, Dict[str, Any], Any]] = []
            for key, (attempts, retry_at) in list(self._crash_retries.items()):
                if attempts > _MAX_CRASH_RESTARTS or retry_at > moment:
                    continue
                instance_id = self._instance_for_key(key)
                if instance_id is None:
                    continue
                thread = self._threads.get(instance_id)
                if thread is not None and thread.is_alive():
                    continue
                spec = self._specs.get(instance_id)
                if spec is not None:
                    due.append(spec)
        if not due:
            return 0
        return self._start_missing(due, counts, callback)

    def _instance_for_key(self, key: Tuple[str, str]) -> Optional[str]:
        for instance_id, spec in self._specs.items():
            if (spec[0], spec[2]) == key:
                return instance_id
        return None

    def health(self) -> Dict[str, Dict[str, Any]]:
        """返回每个已挂载线程的运行和错误状态"""
        with self._lock:
            result: Dict[str, Dict[str, Any]] = {}
            for trigger_id, thread in list(self._threads.items()):
                alive = thread.is_alive()
                last_error = self._crash_errors.get(trigger_id)
                result[trigger_id] = {
                    "config": self._configs.get(trigger_id),
                    "alive": alive,
                    "stuck": self._stop_failures.get(trigger_id, 0) >= _STUCK_THRESHOLD,
                    "crashed": (not alive) and last_error is not None,
                    "last_error": last_error if not alive else None,
                }
            return result
