"""管理一代引擎实例的创建、运行和停止，并通过监听器报告状态"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Optional

from notmyfault.core.logging import engine_error, engine_info


EngineFactory = Callable[..., Any]
EventSink = Callable[[str, dict[str, Any]], None]
StateListener = Callable[[str], None]
FailureListener = Callable[[Exception], None]


@dataclass(frozen=True)
class RuntimeStatus:
    """某一时刻的只读运行时状态"""

    state: str
    running: bool
    generation: int
    thread_alive: bool
    last_error: Optional[str]


class RuntimeController:
    """统一管理引擎启停并保存当前实例"""

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
        engine_info(f"引擎状态：{state}")
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
        """创建并启动新一代引擎，已有线程运行时返回 False"""
        with self._lifecycle_lock:
            thread = self._engine_thread
            if self._state in ("running", "starting"):
                print("[Engine] 引擎已在运行或正在启动")
                return False
            if thread is not None and thread.is_alive():
                print(f"[Engine] 引擎当前处于 {self._state}，拒绝重复启动")
                return False
            if self._current_engine is not None:
                self._last_error = "上一代引擎尚未完全停止"
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
            except Exception as exc:
                self._engine_thread = None
                self._set_state("stopped")
                engine_error(
                    "engine_start_failed",
                    reason=str(exc),
                    error_type=type(exc).__name__,
                )
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
            engine_error(
                "engine_failed",
                reason=str(exc),
                error_type=type(exc).__name__,
            )
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
                    clean = getattr(self._current_engine, "_shutdown_clean", True)
                    if clean:
                        self._current_engine = None
                    else:
                        self._last_error = "引擎仍有未退出的触发器或动作"
            if is_current_generation:
                self._set_state("stopped" if clean else "stopping")

    def request_stop(self) -> bool:
        """发出停止信号并返回当前是否存在运行线程"""
        with self._lifecycle_lock:
            thread = self._engine_thread
            alive = bool(thread and thread.is_alive())
            if alive:
                self._set_state("stopping")
            shutdown_event = self._shutdown_event
        shutdown_event.set()
        return alive

    def stop(self, timeout: float = 5.0) -> bool:
        """请求停止并等待线程退出，返回是否已完全停止"""
        print("[Engine] 收到停止指令")
        deadline = time.monotonic() + max(0.0, timeout)
        self.request_stop()
        with self._lifecycle_lock:
            thread = self._engine_thread
            generation = self._generation
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                print(f"[Engine] 警告：引擎线程 {timeout:g} 秒内未退出，继续停止中")
                return False
        with self._lifecycle_lock:
            engine = self._current_engine
        if engine is not None:
            engine.shutdown(timeout=max(0.0, deadline - time.monotonic()))
            if not getattr(engine, "_shutdown_clean", True):
                return False
        with self._lifecycle_lock:
            if generation != self._generation:
                return False
            if self._current_engine is engine:
                self._current_engine = None
            self._set_state("stopped")
        return True
