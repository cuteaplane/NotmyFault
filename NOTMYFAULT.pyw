import sys
import os
import signal
import socket
import threading
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from notmyfault import run as start_engine
from notmyfault.api_server import EngineAPI
from notmyfault.config import CONFIG_FILE

LOG_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "engine.log")


def setup_logging(log_path: str):
    """将 stdout / stderr 重定向到日志文件，每行带时间戳"""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    log_fp = open(log_path, "a", encoding="utf-8", buffering=1)  # 行缓冲

    class _TimestampWriter:
        """在每行前面插入时间戳，同时写到文件和原始 stdout"""
        def __init__(self, file, orig):
            self.file = file
            self.orig = orig
            self._pending = True  # 下一行需要写时间戳

        def write(self, text: str):
            if not text:
                return
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 按换行符拆分，每行单独加时间戳
            for i, line in enumerate(text.splitlines(True)):
                if i == 0 and not self._pending:
                    # 续上一行
                    self.file.write(line)
                    self.orig.write(line)
                elif line.strip():
                    prefix = f"[{ts}] "
                    self.file.write(prefix + line)
                    self.orig.write(prefix + line)
                    self._pending = False
                else:
                    self.file.write(line)
                    self.orig.write(line)
                if line.endswith("\n"):
                    self._pending = True
            try:
                self.file.flush()
                self.orig.flush()
            except Exception:
                pass

        def isatty(self) -> bool:
            return False  # 日志文件不是终端，uvicorn 不会尝试输出颜色

        def flush(self):
            self.file.flush()
            self.orig.flush()

    _devnull = open(os.devnull, "w")
    sys.stdout = _TimestampWriter(log_fp, sys.__stdout__ or _devnull)  # type: ignore
    sys.stderr = _TimestampWriter(log_fp, sys.__stderr__ or _devnull)  # type: ignore
    print(f"--- NotmyFault 引擎启动 {datetime.now().isoformat()} ---")


class EngineRunner:
    """后台引擎运行器 — 管理引擎生命周期"""

    def __init__(self):
        self.engine_running = False
        self.shutdown_event = threading.Event()
        self.engine_thread = None
        self._api = None

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        print(f"\n[Engine] 收到关闭信号 ({signum})，正在关闭...")
        self.shutdown_event.set()

    # ================================================================
    # 引擎生命周期
    # ================================================================

    def _start_engine_core(self):
        """启动引擎线程"""
        if self.engine_running:
            print("[Engine] 引擎已在运行")
            return

        # 确保旧引擎线程彻底退出后再启动新的
        if self.engine_thread and self.engine_thread.is_alive():
            print("[Engine] 等待旧引擎线程退出...")
            self.engine_thread.join(timeout=10)

        # 每个引擎实例使用独立的 shutdown_event，避免旧 daemon
        # 触发器线程在新引擎 clear() 后死灰复燃造成重复处理
        self.shutdown_event = threading.Event()
        self.engine_thread = threading.Thread(
            target=self._run_engine,
            name="Engine-Core",
            daemon=False,
        )
        self.engine_thread.start()

    def _run_engine(self):
        try:
            self.engine_running = True
            start_engine(
                shutdown_event=self.shutdown_event,
                on_event=self._api.push_event if self._api else None,
            )
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(f"[Engine] 引擎错误: {e}")
            import traceback
            traceback.print_exc()
            # 拉起 Dashboard 通知用户
            try:
                from notmyfault.alert import alert_user
                alert_user("引擎异常退出", f"引擎线程崩溃: {e}", open_dashboard=True)
            except Exception:
                pass
            if self._api:
                self._api.push_event("error", {"error": str(e)})
        finally:
            self.engine_running = False
            if self._api:
                self._api.push_event("engine_state_changed", {"state": "stopped"})

    def _stop_engine(self):
        """停止引擎"""
        print("[Engine] 收到停止指令")
        self.shutdown_event.set()
        if self.engine_thread and self.engine_thread.is_alive():
            self.engine_thread.join(timeout=5)
            if self.engine_thread.is_alive():
                print("[Engine] 警告：引擎线程 5 秒内未退出，强制标记为停止")
        self.engine_running = False

    # ================================================================
    # 单实例检查
    # ================================================================

    @staticmethod
    def _check_already_running(port: int = 19198) -> bool:
        """尝试连接本地端口，连上说明已有实例在运行"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except (socket.error, OSError):
            return False

    # ================================================================
    # 主启动流程
    # ================================================================

    def run(self):
        setup_logging(LOG_FILE)

        print("=" * 50)
        print("  NotmyFault Engine ")
        print("=" * 50)
        print(f"  日志文件: {LOG_FILE}")

        # 0. 单实例检查
        if self._check_already_running():
            print("[Engine] 引擎已在运行，无需重复启动")
            return

        # 1. 创建 HTTP API 服务
        self._api = EngineAPI(self)

        # 2. 启动引擎
        print("[启动] 启动主引擎...")
        self._start_engine_core()
        self._api.push_event("engine_state_changed", {"state": "running"})

        # 3. 启动 HTTP 服务（阻塞，直到 uvicorn 退出）
        try:
            self._api.serve(host="127.0.0.1", port=19198)
        except KeyboardInterrupt:
            print("\n[Engine] 手动中断")
        except Exception as e:
            print(f"\n[Engine] HTTP 服务异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._cleanup()

    def _cleanup(self):
        print("\n[Cleanup] 正在关闭...")
        self.shutdown_event.set()
        if self.engine_thread and self.engine_thread.is_alive():
            self.engine_thread.join(timeout=5)
        print("[Cleanup] Done! ")
        print(f"--- 引擎关闭 {datetime.now().isoformat()} ---")


def main():
    runner = EngineRunner()
    runner.run()


if __name__ == "__main__":
    main()
