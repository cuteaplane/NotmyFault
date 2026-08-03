"""轮询型触发器基类
每轮调用子类的 poll()，native=True 时持有 NATIVE_LOCK，异常记录后继续轮询，退出时调用 teardown()
子类实现 validate()、setup()、poll()、teardown()，文件末尾的 run() 包装函数转发到基类
"""
import sys
import threading
import traceback
from typing import Any, Dict

from notmyfault.native import NATIVE_LOCK


class PollingTrigger:
    """默认每 5 秒调用 poll() 的触发器基类"""

    interval: float = 5.0
    native: bool = False

    def __init__(self, meta: Dict[str, Any], config: Any,
                 emit_event: Any, shutdown_event: threading.Event) -> None:
        self.meta = meta
        self.config = config
        self._emit_event = emit_event
        self._stop_event = shutdown_event
        self.trigger_id = str(meta.get("id", type(self).__name__))

    def emit(self, payload: Dict[str, Any]) -> None:
        """向引擎发送带 trigger_id 校验的 event-v2 事件"""
        self._emit_event(payload)

    def log(self, message: str) -> None:
        print(f"[Trigger:{self.trigger_id}] {message}", flush=True)

    def validate(self) -> None:
        """校验配置，非法取值抛 ValueError"""

    def setup(self) -> None:
        """启动前执行一次性初始化，子类可注册热键或创建隐藏窗口"""

    def poll(self) -> None:
        """执行一次采样，run() 捕获异常并继续下一轮"""

    def teardown(self) -> None:
        """退出时释放资源，子类可注销热键或销毁窗口"""

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
