"""引擎运行时生命周期控制。

这个模块只管理一代引擎实例的创建、运行与停止，不负责 HTTP、托盘或进程
退出。外围宿主通过状态监听器和事件回调连接这些界面层。
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Optional


EngineFactory = Callable[..., Any]
EventSink = Callable[[str, dict[str, Any]], None]
StateListener = Callable[[str], None]
FailureListener = Callable[[Exception], None]


@dataclass(frozen=True)
class RuntimeStatus:
    """某一时刻的只读运行时快照。"""

    state: str
    running: bool
    generation: int
    thread_alive: bool
    last_error: Optional[str]


class RuntimeController:
    """串行化引擎启停，并持有当前可服务的引擎实例。"""

    def __init__(
        self,
        engine_factory: EngineFactory,
        *,
        event_sink: Optional[EventSink] = None,
        failure_listener: Optional[FailureListener] = None,
    ) -> None:
        self._engine_factory = engine_factory
        self._event_sink = event_sink
        self._failure_listener = failure_listener
        self._state_listeners: list[StateListener] = []
        self._lifecycle_lock = threading.RLock()

        self._state = "stopped"
        self._shutdown_event = threading.Event()
        self._engine_thread: Optional[threading.Thread] = None
        self._current_engine: Any = None
        self._generation = 0
        self._last_error: Optional[str] = None

    @property
    def state(self) -> str:
        with self._lifecycle_lock:
            return self._state

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def shutdown_event(self) -> threading.Event:
        with self._lifecycle_lock:
            return self._shutdown_event

    @property
    def engine_thread(self) -> Optional[threading.Thread]:
        with self._lifecycle_lock:
            return self._engine_thread

    @property
    def current_engine(self) -> Any:
        with self._lifecycle_lock:
            return self._current_engine

    def status(self) -> RuntimeStatus:
        with self._lifecycle_lock:
            thread = self._engine_thread
            return RuntimeStatus(
                state=self._state,
                running=self._state == "running",
                generation=self._generation,
                thread_alive=bool(thread and thread.is_alive()),
                last_error=self._last_error,
            )

    def set_event_sink(self, event_sink: Optional[EventSink]) -> None:
        with self._lifecycle_lock:
            self._event_sink = event_sink

    def add_state_listener(self, listener: StateListener) -> None:
        with self._lifecycle_lock:
            if listener not in self._state_listeners:
                self._state_listeners.append(listener)

    def _set_state(self, state: str) -> None:
        with self._lifecycle_lock:
            if self._state == state:
                return
            self._state = state
            listeners = tuple(self._state_listeners)
        for listener in listeners:
            try:
                listener(state)
            except Exception:
                traceback.print_exc()

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        with self._lifecycle_lock:
            sink = self._event_sink
        if sink is not None:
            sink(event_type, data)

    def start(self) -> bool:
        """创建并启动一代新引擎；已有线程尚未退出时拒绝重入。"""
        with self._lifecycle_lock:
            thread = self._engine_thread
            if self._state in ("running", "starting"):
                print("[Engine] 引擎已在运行或正在启动")
                return False
            if thread is not None and thread.is_alive():
                print(f"[Engine] 引擎当前处于 {self._state}，拒绝重复启动")
                return False

            self._shutdown_event = threading.Event()
            self._generation += 1
            generation = self._generation
            self._last_error = None
            thread = threading.Thread(
                target=self._run_generation,
                args=(generation, self._shutdown_event),
                name=f"Engine-Core-{generation}",
                daemon=False,
            )
            self._engine_thread = thread
            self._set_state("starting")
            try:
                thread.start()
            except Exception:
                self._engine_thread = None
                self._set_state("stopped")
                raise
            return True

    def _run_generation(
        self,
        generation: int,
        shutdown_event: threading.Event,
    ) -> None:
        try:
            engine = self._engine_factory(on_event=self._emit_event)
            with self._lifecycle_lock:
                if generation != self._generation:
                    return
                self._current_engine = engine
            self._set_state("running")
            engine.start(shutdown_event=shutdown_event)
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            with self._lifecycle_lock:
                self._last_error = str(exc)
            print(f"[Engine] 引擎错误: {exc}")
            traceback.print_exc()
            self._emit_event("error", {"error": str(exc)})
            if self._failure_listener is not None:
                try:
                    self._failure_listener(exc)
                except Exception:
                    traceback.print_exc()
        finally:
            with self._lifecycle_lock:
                is_current_generation = generation == self._generation
                if is_current_generation:
                    self._current_engine = None
            if is_current_generation:
                self._set_state("stopped")

    def request_stop(self) -> bool:
        """发出停止信号；返回发出信号时是否仍有引擎线程。"""
        with self._lifecycle_lock:
            thread = self._engine_thread
            alive = bool(thread and thread.is_alive())
            if alive:
                self._set_state("stopping")
            shutdown_event = self._shutdown_event
        shutdown_event.set()
        return alive

    def stop(self, timeout: float = 5.0) -> bool:
        """请求停止并等待线程退出；返回是否已完全停止。"""
        print("[Engine] 收到停止指令")
        self.request_stop()
        with self._lifecycle_lock:
            thread = self._engine_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                print(f"[Engine] 警告：引擎线程 {timeout:g} 秒内未退出，继续停止中")
                return False
        self._set_state("stopped")
        return True
