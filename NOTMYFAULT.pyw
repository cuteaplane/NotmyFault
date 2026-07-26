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
    return log_path


def _open_dashboard():
    """在后台打开 Dashboard。"""
    if getattr(sys, "frozen", False):
        import subprocess
        executable_dir = os.path.dirname(sys.executable)
        current = os.path.normcase(os.path.abspath(sys.executable))
        for filename in ("NotmyFaultDashboard.exe", "dashboard.exe"):
            candidate = os.path.join(executable_dir, filename)
            if (
                os.path.isfile(candidate)
                and os.path.normcase(os.path.abspath(candidate)) != current
            ):
                subprocess.Popen(
                    [candidate],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return
    dashboard_pyw = os.path.join(PROJECT_ROOT, "dashboard.pyw")
    if os.path.exists(dashboard_pyw):
        try:
            os.startfile(dashboard_pyw)
            return
        except Exception:
            pass
    print("[Dashboard] 找不到可启动的 Dashboard 入口", file=sys.stderr)


class EngineRunner:
    """后台引擎运行器 — 管理引擎生命周期"""

    def __init__(self):
        self.engine_running = False
        self.engine_state = "stopped"
        self.shutdown_event = threading.Event()
        self.engine_thread = None
        # 启停可能同时来自托盘和 HTTP API。更关键的是，旧引擎线程尚未退出时
        # 绝不能替换 shutdown_event 后再启动一代新线程，否则两个引擎会同时监听。
        self._lifecycle_lock = threading.RLock()
        self._api = None
        self._tray = None
        self._api_socket = None
        self._force_exit_armed = threading.Event()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        print(f"\n[Engine] 收到关闭信号 ({signum})，正在关闭...")
        self.shutdown_event.set()

    def _set_engine_state(self, state: str) -> None:
        """以一个状态源同步核心、托盘和 Dashboard。"""
        with self._lifecycle_lock:
            self.engine_state = state
            self.engine_running = state == "running"
        if self._tray:
            self._tray.set_engine_state(state)
        if self._api:
            self._api.push_event("engine_state_changed", {"state": state})

    # ================================================================
    # 引擎生命周期
    # ================================================================

    def _start_engine_core(self):
        """启动引擎线程。

        返回 ``True`` 只表示本次成功创建了新线程。旧线程还在收尾时返回
        ``False``：停机中的引擎不允许被覆盖，必须等它自己的 finally 清理完。
        """
        with self._lifecycle_lock:
            if self.engine_running:
                print("[Engine] 引擎已在运行")
                return False

            if self.engine_thread and self.engine_thread.is_alive():
                print(f"[Engine] 引擎当前处于 {self.engine_state}，拒绝重复启动")
                return False

            # 每个已完全结束的引擎实例使用独立 shutdown_event，避免旧触发器
            # 读取到新 Event 后死灰复燃。
            self.shutdown_event = threading.Event()
            self.engine_thread = threading.Thread(
                target=self._run_engine,
                name="Engine-Core",
                daemon=False,
            )
            self._set_engine_state("starting")
            self.engine_thread.start()
            return True

    def _run_engine(self):
        try:
            engine = create_engine(
                on_event=self._api.push_event if self._api else None,
            )
            # 注入引擎引用，供 API 诊断端点使用
            if self._api:
                self._api._engine_ref = engine
            self._set_engine_state("running")
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
            if self._api:
                self._api._engine_ref = None
            self._set_engine_state("stopped")

    def _stop_engine(self):
        """请求停止引擎；返回线程是否已经完全退出。"""
        print("[Engine] 收到停止指令")
        if not self.engine_thread or not self.engine_thread.is_alive():
            self._set_engine_state("stopped")
            return True
        self._set_engine_state("stopping")
        self.shutdown_event.set()
        if self.engine_thread and self.engine_thread.is_alive():
            self.engine_thread.join(timeout=5)
            if self.engine_thread.is_alive():
                print("[Engine] 警告：引擎线程 5 秒内未退出，继续停止中")
                return False
        self._set_engine_state("stopped")
        return True

    def _toggle_engine(self):
        """托盘切换引擎启停"""
        if self.engine_state == "running":
            self._stop_engine()
        elif self.engine_state == "stopped":
            self._start_engine_core()
        else:
            print(f"[Tray] 引擎正处于 {self.engine_state}，忽略重复切换")

    def _tray_exit(self):
        """托盘退出—关闭引擎、HTTP 服务、退出进程"""
        print("[Tray] 用户请求退出")
        self._stop_engine()
        self._request_process_shutdown(force_after=5)

    def _request_process_shutdown(self, force_after: float = 10) -> None:
        """关闭托盘和 API；超时后兜底终止，避免残留无控制面的僵尸进程。"""
        if self.engine_thread and self.engine_thread.is_alive():
            self._set_engine_state("stopping")
        self.shutdown_event.set()
        if self._api and self._api._server:
            self._api._server.should_exit = True
        if self._tray:
            self._tray.stop()
        if self._force_exit_armed.is_set():
            return
        self._force_exit_armed.set()

        def _force():
            import time
            time.sleep(force_after)
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

    @staticmethod
    def _claim_api_socket(port: int = 19198):
        """在启动任何触发器之前独占 API 端口，消除并发启动竞态。"""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_EXCLUSIVEADDRUSE,
                    1,
                )
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            return listener
        except OSError:
            listener.close()
            return None

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

        # 0. 在启动核心前先独占监听端口。单纯“先 connect 再 bind”存在
        # TOCTOU 窗口，两个并发实例可能都启动触发器。
        self._api_socket = self._claim_api_socket()
        if self._api_socket is None:
            if self._check_already_running():
                print("[Engine] 已有实例或其他服务占用 127.0.0.1:19198，拒绝重复启动")
            else:
                print("[Engine] 无法独占 127.0.0.1:19198，拒绝启动", file=sys.stderr)
            return

        try:
            # 1. 创建 HTTP API 服务
            self._api = EngineAPI(self)

            # 2. 启动引擎
            print("[启动] 启动主引擎...")
            self._start_engine_core()

            # 3. 系统托盘
            if _HAS_TRAY:
                self._tray = TrayIcon(
                    on_open_dashboard=_open_dashboard,
                    on_toggle_engine=self._toggle_engine,
                    on_exit=self._tray_exit,
                )
                self._tray.start()
                self._tray.set_engine_state(self.engine_state)
                self._tray.show_balloon("NotmyFault", "引擎已启动")
                print("[Tray] 系统托盘图标已启动")

            # 4. 启动 HTTP 服务（阻塞，直到 uvicorn 退出）
            self._api.serve(
                host="127.0.0.1",
                port=19198,
                sockets=[self._api_socket],
            )
        except KeyboardInterrupt:
            print("\n[Engine] 手动中断")
        except Exception as e:
            print(f"\n[Engine] 启动或 HTTP 服务异常: {e}")
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
            if self.engine_thread.is_alive():
                print("[Cleanup] 引擎线程仍未退出，已安排强制终止", file=sys.stderr)
                self._request_process_shutdown(force_after=5)
        if self._tray:
            self._tray.show_balloon("NotmyFault", "引擎已停止", 1)
        if self._api_socket is not None:
            try:
                self._api_socket.close()
            except OSError:
                pass
            self._api_socket = None
        print("[Cleanup] Done! ")
        print(f"--- 引擎关闭 {datetime.now().isoformat()} ---")


def main():
    runner = EngineRunner()
    runner.run()


if __name__ == "__main__":
    if "--dashboard" in sys.argv:
        _open_dashboard()
    else:
        main()
