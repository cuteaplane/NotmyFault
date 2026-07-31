"""触发器线程监管器：独占触发器线程、停止事件与锁的登记和生命周期管理。

作为后续增量订阅和健康状态承载层的前置抽取，本模块将 engine.py 中
_trigger_threads / _trigger_events / _trigger_lock 的所有权移至独立组件，
同时通过属性代理保持引擎的兼容马甲（测试可直接读写这些属性）。
"""
import sys
import threading
import time
from notmyfault.rules import config_fingerprint
from typing import Any, Callable, Dict, List, Optional


class TriggerSupervisor:
    """触发器线程的单一所有者：登记、启动、停止与健康观测。

    threads / events / lock 通过 property 暴露且支持赋值，
    使 ``AutomationEngine._trigger_threads`` 等属性仍可被外部直接读写。
    """

    def __init__(
        self,
        alert_cb: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._alert_cb = alert_cb
        self._threads: Dict[str, threading.Thread] = {}
        self._events: Dict[str, threading.Event] = {}
        self._crash_errors: Dict[str, str] = {}
        self._configs: Dict[str, Dict[str, Any]] = {}  # 实例 ID -> 配置快照（健康状态展示）
        self._lock = threading.RLock()
        # start/stop 不能交叉。登记锁只保护字典，生命周期锁覆盖 thread.start
        # 到 stop/join 的完整操作，封死“已登记但尚未 start 就被 join”的窗口。
        self._lifecycle_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 兼容马甲：engine._trigger_threads / _events / _lock 指向同一对象
    # ------------------------------------------------------------------

    @property
    def threads(self) -> Dict[str, threading.Thread]:
        return self._threads

    @threads.setter
    def threads(self, value: Dict[str, threading.Thread]) -> None:
        with self._lock:
            self._threads = value

    @property
    def events(self) -> Dict[str, threading.Event]:
        return self._events

    @events.setter
    def events(self, value: Dict[str, threading.Event]) -> None:
        with self._lock:
            self._events = value

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @lock.setter
    def lock(self, value: threading.RLock) -> None:
        self._lock = value

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def start(
        self,
        aggregated: Dict[str, List[Dict[str, Any]]],
        triggers_funcs: Dict[str, Any],
        triggers_meta: Dict[str, Dict[str, Any]],
        run_trigger_cb: Callable[..., Any],
    ) -> int:
        """接收聚合订阅并启动触发器线程，保持先登记后启动语义。

        ``event-v1`` 每类触发器共用一个配置列表；``event-v2`` 为每个配置
        启动一个隔离实例，实例 ID 使用 ``trigger_id:index``。
        """
        missing = [et for et in aggregated if et not in triggers_funcs]
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

        count = 0
        with self._lifecycle_lock:
            for event_type, config_list in aggregated.items():
                if event_type not in triggers_funcs:
                    continue

                trigger_meta = triggers_meta.get(event_type, {})
                trigger_func = triggers_funcs[event_type]
                # event-v2 按配置指纹去重：多条规则使用相同配置（如同一热键、
                # 同一监控文件夹）时只启动一个实例，事件仍按指纹匹配所有
                # 叶子；否则相同配置会启动 N 个实例，每次真实事件被放大 N 倍。
                instances = (
                    config_list
                    if trigger_meta.get("trigger_api") == "event-v2"
                    else [config_list]
                )
                if trigger_meta.get("trigger_api") == "event-v2":
                    skipped = 0
                    seen_fingerprints = set()
                    unique_instances = []
                    for cfg in instances:
                        fp = config_fingerprint(cfg)
                        if fp in seen_fingerprints:
                            skipped += 1
                            continue
                        seen_fingerprints.add(fp)
                        unique_instances.append(cfg)
                    instances = unique_instances
                    if skipped:
                        print(
                            f"[Engine] 触发器 {event_type} 有 {skipped} 个重复配置"
                            "（相同指纹）已合并，只启动一个实例",
                            file=sys.stderr,
                        )
                for index, config in enumerate(instances):
                    instance_id = f"{event_type}:{index + 1}" if len(instances) > 1 else event_type
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
                    # 登记和 start 在同一临界区内。stop 只能观察到“尚未登记”或
                    # “已经启动”的线程，不会 join 一个未启动的 Thread。
                    with self._lock:
                        existing = self._threads.get(instance_id)
                        if existing is not None and existing.is_alive():
                            print(
                                f"[Engine] [!!] 触发器线程 {instance_id} 已在运行，"
                                "拒绝重复启动",
                                file=sys.stderr,
                            )
                            continue
                        self._events[instance_id] = trigger_event
                        self._configs[instance_id] = config
                        self._threads[instance_id] = thread
                        self._crash_errors.pop(instance_id, None)
                        try:
                            thread.start()
                        except Exception:
                            if self._threads.get(instance_id) is thread:
                                self._configs.pop(instance_id, None)
                                self._threads.pop(instance_id, None)
                                self._events.pop(instance_id, None)
                            raise
                    count += 1
                    print(
                        f"[Engine] 已启动触发器线程: {instance_id}"
                        f"（监听 {event_type} 的第 {index + 1} 项配置）"
                    )

        return count

    # ------------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------------

    def stop(self, timeout: float = 30.0) -> bool:
        """广播停止、共享 deadline join，仅清退已退出线程。

        仍在运行的线程保留登记，避免热重载在同一触发器上再启动一代线程。
        """
        with self._lifecycle_lock:
            with self._lock:
                if not self._threads:
                    return True
                events = list(self._events.values())
                threads = list(self._threads.items())

            # 先广播"收工"，再 join；反过来等会儿基本就是和自己较劲。
            for evt in events:
                evt.set()

            deadline = time.monotonic() + timeout
            for event_type, thread in threads:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    thread.join(timeout=remaining)
                if thread.is_alive():
                    print(
                        f"[Engine] [!!] 触发器线程 {event_type}"
                        f" 未在 {timeout}s 内退出，继续保留监管",
                        file=sys.stderr,
                    )

            alive = {et for et, thread in threads if thread.is_alive()}
            with self._lock:
                for event_type, thread in threads:
                    # 即使未来允许并发增量替换，也不能让旧 stop 删除新线程。
                    if (
                        event_type not in alive
                        and self._threads.get(event_type) is thread
                    ):
                        self._threads.pop(event_type, None)
                        self._events.pop(event_type, None)
                        self._configs.pop(event_type, None)
                        self._crash_errors.pop(event_type, None)
            return not alive

    def request_stop_all(self) -> None:
        """幂等地广播停止信号给所有已登记触发器。

        Event.set() 本身幂等，多次调用安全无副作用。
        """
        with self._lifecycle_lock:
            with self._lock:
                events = list(self._events.values())
            for evt in events:
                evt.set()

    def mark_crashed(self, trigger_id: str, error: str) -> None:
        """把崩溃关联到当前登记代，而不是复用 Diagnostics 的历史记录。"""
        with self._lock:
            if trigger_id in self._threads:
                self._crash_errors[trigger_id] = error[:300]

    # ------------------------------------------------------------------
    # 健康观测
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Dict[str, Any]]:
        """返回每个已登记触发器的 alive/crashed/last_error 状态。

        崩溃信息按当前登记代保存；新一代启动时会清除上一代错误。
        """
        with self._lock:
            result: Dict[str, Dict[str, Any]] = {}
            for trigger_id, thread in list(self._threads.items()):
                alive = thread.is_alive()
                last_error = self._crash_errors.get(trigger_id)
                result[trigger_id] = {
                    "config": self._configs.get(trigger_id),
                    "alive": alive,
                    "crashed": (not alive) and last_error is not None,
                    "last_error": last_error if not alive else None,
                }
            return result
