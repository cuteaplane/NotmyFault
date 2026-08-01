"""轮询型触发器的公共基类（event-v2）。

统一"配置校验 → 每间隔轮询一次 → 异常隔离 → 原生段加锁 → 退出清理"
的骨架，避免每个触发器各自手写 while/except/wait，导致加锁、argtypes
这类原生安全纪律全靠自觉（曾因 clipboard 与 window_title 并发 ctypes
未加锁引发 0xc0000374 堆损坏静默崩溃，见 docs/native-safety.md）。

子类只需实现：

- ``interval``：轮询间隔（秒）
- ``native``：poll 是否触达原生 API（ctypes/COM）；True 时自动持有
  NATIVE_LOCK，杜绝多线程并发写 ctypes 函数对象共享引用表的竞态
- ``validate()``：校验配置，非法取值抛 ValueError（引擎会标记崩溃并告警）
- ``setup()`` / ``teardown()``：可选的一次性资源（注册热键、创建隐藏窗口）
- ``poll()``：单次轮询；可用 self.emit(payload) 发事件，异常由 run 隔离

模块入口保持 ``run(meta, config, emit_event, shutdown_event)`` 四参数，
由子类文件末尾的薄包装转发给基类，插件加载器按原签名校验。
"""
import sys
import threading
import traceback
from typing import Any, Dict

from notmyfault.native import NATIVE_LOCK


class PollingTrigger:
    """轮询型触发器基类。"""

    interval: float = 5.0
    native: bool = False

    def __init__(self, meta: Dict[str, Any], config: Any,
                 emit_event: Any, shutdown_event: threading.Event) -> None:
        self.meta = meta
        self.config = config
        self._emit_event = emit_event
        self._stop_event = shutdown_event
        self.trigger_id = str(meta.get("id", type(self).__name__))

    # ---- 供子类使用 ----
    def emit(self, payload: Dict[str, Any]) -> None:
        """发出 event-v2 事件（引擎已绑定 trigger ID 与输出契约校验）。"""
        self._emit_event(payload)

    def log(self, message: str) -> None:
        print(f"[Trigger:{self.trigger_id}] {message}", flush=True)

    # ---- 子类可覆写 ----
    def validate(self) -> None:
        """配置校验；非法取值抛 ValueError。"""

    def setup(self) -> None:
        """可选：启动前的一次性初始化（注册热键、创建隐藏窗口等）。"""

    def poll(self) -> None:
        """单次轮询；异常由 run 隔离并记录，不影响后续轮询。"""

    def teardown(self) -> None:
        """可选：退出时释放资源（注销热键、销毁窗口等）。"""

    # ---- 骨架 ----
    def run(self) -> None:
        self.validate()
        self.setup()
        try:
            while not self._stop_event.is_set():
                try:
                    if self.native:
                        with NATIVE_LOCK:
                            self.poll()
                    else:
                        self.poll()
                except Exception:
                    print(
                        f"[Trigger:{self.trigger_id}] 轮询出错:",
                        file=sys.stderr,
                    )
                    traceback.print_exc(limit=3)
                self._stop_event.wait(self.interval)
        finally:
            self.teardown()
