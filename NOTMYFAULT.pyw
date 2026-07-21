import sys
import os
import signal
import socket
import threading
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from notmyfault.app import create_engine
from notmyfault.api_server import EngineAPI
from notmyfault.config import CONFIG_FILE
from notmyfault.logging import init_session_log

LOG_DIR = os.path.join(os.path.dirname(CONFIG_FILE), "logs")

# 系统托盘（导入失败不阻塞，无托盘也能运行）
try:
    from notmyfault.tray import TrayIcon
    _HAS_TRAY = True
except ImportError:
    _HAS_TRAY = False


def setup_logging(log_dir: str) -> str:
    """初始化 session 日志文件，重定向 stdout/stderr。

    每次调用创建新文件 engine-YYYYMMDD-HHMMSS.log，自动清理旧文件保留 7 个。
    同时删除旧版单体 engine.log（迁移用，仅首次执行一次）。
    """
    # 删除旧版单体日志文件
    old_log = os.path.join(os.path.dirname(log_dir), "engine.log")
    if os.path.exists(old_log):
        try:
            os.remove(old_log)
        except OSError:
            pass

    log_path = init_session_log(log_dir)
    log_fp = open(log_path, "a", encoding="utf-8", buffering=1)

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

    # pythonw.exe 无控制台，sys.__stdout__/__stderr__ 为 None，_devnull 兜底
    # 这玩意不能删！！！
    _devnull = open(os.devnull, "w")
    sys.stdout = _TimestampWriter(log_fp, sys.__stdout__ or _devnull)  # type: ignore
    sys.stderr = _TimestampWriter(log_fp, sys.__stderr__ or _devnull)  # type: ignore
    print(f"--- NotmyFault 引擎启动 {datetime.now().isoformat()} ---")


def _open_dashboard():
    """在后台打开 Dashboard。"""
    dashboard_pyw = os.path.join(PROJECT_ROOT, "dashboard.pyw")
    if os.path.exists(dashboard_pyw):
        try:
            os.startfile(dashboard_pyw)
        except Exception:
            pass


class EngineRunner:
    """后台引擎运行器 — 管理引擎生命周期"""

    def __init__(self):
        self.engine_running = False
        self.shutdown_event = threading.Event()
        self.engine_thread = None
        self._api = None
        self._tray = None

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
            if self._tray:
                self._tray.set_engine_running(True)
            engine = create_engine(
                on_event=self._api.push_event if self._api else None,
            )
            # 注入引擎引用，供 API 诊断端点使用
            if self._api:
                self._api._engine_ref = engine
            engine.start(shutdown_event=self.shutdown_event)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(f"[Engine] 引擎错误: {e}")
            import traceback
            traceback.print_exc()
            if self._tray:
                self._tray.show_balloon("引擎异常", f"引擎线程崩溃: {e}", 3)
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
            if self._tray:
                self._tray.set_engine_running(False)
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

    def _toggle_engine(self):
        """托盘切换引擎启停"""
        if self.engine_running:
            self._stop_engine()
            if self._tray:
                self._tray.set_engine_running(False)
        else:
            # 启动是异步的，乐观更新托盘状态；
            # _run_engine 线程启动后会再次同步，避免读取尚未置位的 engine_running
            self._start_engine_core()
            if self._tray:
                self._tray.set_engine_running(True)

    def _tray_exit(self):
        """托盘退出—关闭引擎、HTTP 服务、退出进程"""
        print("[Tray] 用户请求退出")
        self.shutdown_event.set()
        if self._api and self._api._server:
            self._api._server.should_exit = True
        if self._tray:
            self._tray.stop()
        # 强制退出（5 秒内干净的 shutdown 没完成就硬杀）
        def _force():
            import time
            time.sleep(5)
            os._exit(0)
        threading.Thread(target=_force, daemon=True).start()

    # ================================================================
    # 单实例检查
    # ================================================================

    @staticmethod
    def _check_already_running(port: int = 19198) -> bool:
        """尝试连接本地端口，连上说明已有实例在运行"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", port))
            return True
        except (socket.error, OSError):
            return False

    # ================================================================
    # 主启动流程
    # ================================================================

    def run(self):
        log_path = setup_logging(LOG_DIR)

        print("=" * 50)
        print("  NotmyFault Engine ")
        print("=" * 50)
        print(f"  日志目录: {LOG_DIR}")
        print(f"  当前日志: {log_path}")

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

        # 3. 系统托盘
        if _HAS_TRAY:
            self._tray = TrayIcon(
                on_open_dashboard=_open_dashboard,
                on_toggle_engine=self._toggle_engine,
                on_exit=self._tray_exit,
            )
            self._tray.start()
            self._tray.set_engine_running(True)
            self._tray.show_balloon("NotmyFault", "引擎已启动")
            print("[Tray] 系统托盘图标已启动")

        # 4. 启动 HTTP 服务（阻塞，直到 uvicorn 退出）
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
        if self._tray:
            self._tray.stop()
        if self.engine_thread and self.engine_thread.is_alive():
            self.engine_thread.join(timeout=5)
        if self._tray:
            self._tray.show_balloon("NotmyFault", "引擎已停止", 1)
        print("[Cleanup] Done! ")
        print(f"--- 引擎关闭 {datetime.now().isoformat()} ---")


def main():
    runner = EngineRunner()
    runner.run()


if __name__ == "__main__":
    main()
