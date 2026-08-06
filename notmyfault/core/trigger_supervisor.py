"""触发器管理，用于管线程/停止事件/锁，
engine.py 里的 _trigger_threads / _trigger_events / _trigger_lock 原本是引擎
自己的属性，现在收进这个组件统一管理启动、停止、崩溃和健康状态数据；
对外仍用 property 代理回引擎的老名字用于测试
"""
import sys
import threading
import time
from notmyfault.core.rules import config_fingerprint

# 连续两次 join() 超时后只在 health() 中标记卡住，旧线程仍运行时不能启动新线程
_STUCK_THRESHOLD = 2
from typing import Any, Callable, Dict, List, Optional


class TriggerSupervisor:
    """触发器线程管理：启动、停止、记崩溃、报健康

    threads / events / lock 通过 property 暴露且支持赋值，
    使 ``AutomationEngine._trigger_threads`` 等属性仍可被外部直接读写
    """

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
        self._lock = threading.RLock()
        # 启停锁覆盖 thread.start() 到 join() 的完整过程，stop() 只能处理已经挂载的线程
        self._lifecycle_lock = threading.RLock()

    # engine 仍通过这三个属性访问同一个线程管理对象

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

    def start(
        self,
        aggregated: Dict[str, List[Dict[str, Any]]],
        triggers_funcs: Dict[str, Any],
        triggers_meta: Dict[str, Dict[str, Any]],
        run_trigger_cb: Callable[..., Any],
    ) -> int:
        """接收聚合订阅并启动触发器线程，先挂载线程再调用 start"""
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
                # event-v2 按配置指纹合并相同配置，事件仍能匹配所有叶子
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
                    # 在线程字典中挂载线程后再调用 start()，stop() 才能安全处理它
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
                        self._stop_failures.pop(instance_id, None)
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
            for event_type, thread in threads:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    thread.join(timeout=remaining)
                if thread.is_alive():
                    # _stop_failures 的读写统一在 _lock 内，health() 也用它
                    with self._lock:
                        failures = self._stop_failures.get(event_type, 0) + 1
                        self._stop_failures[event_type] = failures
                    print(
                        f"[Engine] [!!] 触发器线程 {event_type}"
                        f" 未在 {timeout}s 内退出（连续 {failures} 次）",
                        file=sys.stderr,
                    )
                else:
                    with self._lock:
                        self._stop_failures.pop(event_type, None)

            alive = {et for et, thread in threads if thread.is_alive()}
            remaining_alive = set(alive)
            stopped_count = len(threads) - len(alive)
            with self._lock:
                for event_type, thread in threads:
                    # 只删除仍对应当前线程对象的条目
                    if (
                        event_type not in alive
                        and self._threads.get(event_type) is thread
                    ):
                        self._threads.pop(event_type, None)
                        self._events.pop(event_type, None)
                        self._configs.pop(event_type, None)
                        self._crash_errors.pop(event_type, None)
                        self._stop_failures.pop(event_type, None)
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

    def mark_crashed(self, trigger_id: str, error: str) -> None:
        """记录仍在字典中的线程崩溃信息"""
        with self._lock:
            if trigger_id in self._threads:
                self._crash_errors[trigger_id] = error[:300]

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
