import sys
import os
import signal
import socket
import threading
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from notmyfault.host.app import create_engine
from notmyfault.host.api_server import EngineAPI
from notmyfault.config import CONFIG_FILE
from notmyfault.core.logging import init_session_log
from notmyfault.core.runtime_controller import RuntimeController

LOG_DIR = os.path.join(os.path.dirname(CONFIG_FILE), "logs")

# Dashboard 控制端口与协议：与 dashboard.pyw 的 DASHBOARD_CONTROL_PORT /
# _CONTROL_QUIT 保持一致（不 import dashboard.pyw，避免拉起 webview 依赖）。
_DASHBOARD_CONTROL_PORT = 19197
_DASHBOARD_QUIT = b"NMF_DASHBOARD_QUIT_V1"

# 系统托盘（导入失败不阻塞，无托盘也能运行）
try:
    if os.name == "nt":
        from notmyfault.host.tray import TrayIcon
        _HAS_TRAY = True
    else:
        from notmyfault.host.tray_linux import TrayIcon, is_tray_supported
        _HAS_TRAY = is_tray_supported()
except Exception:
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

    # stdout / stderr 两个 writer 共享同一把锁：引擎、触发器、动作在多线程里
    # 同时 print 时，锁保证每一行（含时间戳）原子写入，否则会出现行与行交错
    # 粘在一起（如 "[Trigger:hotkey] ...启动[2026-07-31 ...]"）。
    _log_io_lock = threading.Lock()

    class _TimestampWriter:
        """在每行前面插入时间戳，同时写到文件和原始 stdout"""
        def __init__(self, file, orig, lock):
            self.file = file
            self.orig = orig
            self._lock = lock
            self._pending = True  # 下一行需要写时间戳

        def write(self, text: str):
            if not text:
                return
            with self._lock:
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
    sys.stdout = _TimestampWriter(log_fp, sys.__stdout__ or _devnull, _log_io_lock)  # type: ignore
    sys.stderr = _TimestampWriter(log_fp, sys.__stderr__ or _devnull, _log_io_lock)  # type: ignore
    print(f"--- NotmyFault 引擎启动 {datetime.now().isoformat()} ---")
    return log_path


def _notify_dashboard_quit():
    """通知 Dashboard 控制端口关闭自身（托盘退出时 UI 与引擎一起退出）。"""
    try:
        import socket
        with socket.create_connection(
            ("127.0.0.1", _DASHBOARD_CONTROL_PORT), timeout=1
        ) as client:
            client.sendall(_DASHBOARD_QUIT + b"\n")
            client.makefile("rb").readline(32)
    except OSError:
        pass


def _open_dashboard():
    """在后台打开 Dashboard。"""
    if getattr(sys, "frozen", False):
        import subprocess
        executable_dir = os.path.dirname(sys.executable)
        current = os.path.normcase(os.path.abspath(sys.executable))
        filenames = (
            ("NotmyFaultDashboard.exe", "dashboard.exe")
            if os.name == "nt"
            else ("NotmyFaultDashboard", "dashboard", "notmyfault-dashboard")
        )
        for filename in filenames:
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
            from notmyfault.platform.platform_support import launch_python_entry
            launch_python_entry(dashboard_pyw)
            return
        except Exception:
            pass
    print("[Dashboard] 找不到可启动的 Dashboard 入口", file=sys.stderr)


class EngineRunner:
    """桌面后台宿主 — 连接运行时、HTTP API、托盘与进程生命周期。"""

    def __init__(self):
        self._api = None
        self._tray = None
        self._api_socket = None
        self._force_exit_armed = threading.Event()
        self._runtime = RuntimeController(
            create_engine,
            failure_listener=self._handle_engine_failure,
        )
        self._runtime.add_state_listener(self._handle_engine_state)

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    @property
    def engine_running(self) -> bool:
        return self._runtime.running

    @property
    def engine_state(self) -> str:
        return self._runtime.state

    @property
    def shutdown_event(self):
        return self._runtime.shutdown_event

    @property
    def engine_thread(self):
        return self._runtime.engine_thread

    @property
    def current_engine(self):
        return self._runtime.current_engine

    @property
    def last_error(self):
        """上次引擎启动/运行失败的原因（配置校验失败等），供 Dashboard 展示。"""
        return self._runtime.status().last_error

    def _signal_handler(self, signum, frame):
        print(f"\n[Engine] 收到关闭信号 ({signum})，正在关闭...")
        self._runtime.request_stop()

    def _handle_engine_state(self, state: str) -> None:
        """以一个状态源同步核心、托盘和 Dashboard。"""
        if self._tray:
            self._tray.set_engine_state(state)
        if self._api:
            self._api.push_event("engine_state_changed", {"state": state})

    def _handle_engine_failure(self, error: Exception) -> None:
        if self._tray:
            self._tray.show_balloon("引擎异常", f"引擎线程崩溃: {error}", 3)
        try:
            from notmyfault.host.alert import alert_user
            alert_user("引擎异常退出", f"引擎线程崩溃: {error}", open_dashboard=True)
        except Exception:
            pass

    # ================================================================
    # 引擎生命周期
    # ================================================================

    def _start_engine_core(self):
        """启动引擎线程。

        返回 ``True`` 只表示本次成功创建了新线程。旧线程还在收尾时返回
        ``False``：停机中的引擎不允许被覆盖，必须等它自己的 finally 清理完。
        """
        return self._runtime.start()

    def start_engine(self) -> bool:
        """供 API 和宿主调用的稳定启动接口。"""
        return self._start_engine_core()

    def _stop_engine(self):
        """请求停止引擎；返回线程是否已经完全退出。"""
        return self._runtime.stop(timeout=5)

    def stop_engine(self) -> bool:
        """供 API 和宿主调用的稳定停止接口。"""
        return self._stop_engine()

    def _toggle_engine(self):
        """托盘切换引擎启停"""
        if self.engine_state == "running":
            self._stop_engine()
        elif self.engine_state == "stopped":
            self._start_engine_core()
        else:
            print(f"[Tray] 引擎正处于 {self.engine_state}，忽略重复切换")

    def _tray_exit(self):
        """托盘退出—关闭引擎、HTTP 服务、Dashboard 与进程"""
        print("[Tray] 用户请求退出")
        _notify_dashboard_quit()  # 让 Dashboard UI 一起退出
        self._stop_engine()
        self._request_process_shutdown(force_after=5)

    def _request_process_shutdown(self, force_after: float = 10) -> None:
        """关闭托盘和 API；超时后兜底终止，避免残留无控制面的僵尸进程。"""
        if self.engine_thread and self.engine_thread.is_alive():
            self._runtime.request_stop()
        else:
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

    def request_process_shutdown(self, force_after: float = 10) -> None:
        """供 API 调用的进程级关闭接口。"""
        self._request_process_shutdown(force_after=force_after)

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
            self._runtime.set_event_sink(self._api.push_event)

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
        self._runtime.request_stop()
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
    # 拒绝以管理员身份启动：插件提权必须走 notmyfault.security.sudo 的 UAC 授权，
    # 以提升令牌运行会让整个引擎绕过这道受控通道（最小权限原则）。
    from notmyfault.security.security import is_admin_process
    if is_admin_process():
        print(
            "[Engine] [!!] NotmyFault 拒绝以管理员身份启动：请用普通用户权限运行。"
            "插件需要提权时请通过 notmyfault.security.sudo.run_as_admin 弹出 UAC 授权。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    runner = EngineRunner()
    runner.run()


if __name__ == "__main__":
    if "--enable-autostart" in sys.argv or "--disable-autostart" in sys.argv:
        if os.name == "nt":
            raise SystemExit("请通过 Windows 托盘菜单管理开机自启")
        from notmyfault.platform.platform_support import set_linux_autostart
        enabled = "--enable-autostart" in sys.argv
        set_linux_autostart(enabled, PROJECT_ROOT)
        print("已启用开机自启" if enabled else "已关闭开机自启")
    elif "--dashboard" in sys.argv:
        _open_dashboard()
    else:
        main()
