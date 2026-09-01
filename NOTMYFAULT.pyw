import sys
import os
import signal
import socket
import threading
from datetime import datetime
from notmyfault.platform.platform_support import launch_python_entry, show_notification
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from notmyfault.host.app import create_engine
from notmyfault.application_paths import ApplicationPaths
from notmyfault.config import SignedConfigStore
from notmyfault.host.api_server import create_api_server
from notmyfault.host.api.auth import ApiTokenStore
from notmyfault.host.api.events import EventBroker
from notmyfault.host.api.plugin_installation import (
    PendingPreviewStore,
    PluginFileSystem,
    PluginTemporaryStorage,
)
from notmyfault.host.plugin_registry import PluginRegistryClient
from notmyfault.core.run_history import RunHistory
from notmyfault.security.api_key_store import AIKeyStore
from notmyfault.core.logging import init_session_log
from notmyfault.core.runtime_controller import RuntimeController

# Dashboard 控制端口和退出协议与 dashboard.pyw 共用，直接导入会拉起 webview 依赖
_DASHBOARD_CONTROL_PORT = 19197
_DASHBOARD_QUIT = b"NMF_DASHBOARD_QUIT_V2"

# 托盘模块导入失败时仍可运行引擎
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
    """创建带时间戳的会话日志，保留最近 7 个文件并删除旧版 engine.log"""
    old_log = os.path.join(os.path.dirname(log_dir), "engine.log")
    if os.path.exists(old_log):
        try:
            os.remove(old_log)
        except OSError:
            pass

    log_path = init_session_log(log_dir)
    log_fp = open(log_path, "a", encoding="utf-8", buffering=1)

    # 多线程同时写日志时用同一把锁让时间戳和内容保持在同一行
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
                for i, line in enumerate(text.splitlines(True)):
                    if i == 0 and not self._pending:
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
            return False  # 日志文件不是终端，uvicorn 不需要输出颜色

        def flush(self):
            self.file.flush()
            self.orig.flush()

    # pythonw.exe 没有控制台时标准输出可能为 None，日志改写器使用 os.devnull
    _devnull = open(os.devnull, "w")
    sys.stdout = _TimestampWriter(log_fp, sys.__stdout__ or _devnull, _log_io_lock)  # type: ignore
    sys.stderr = _TimestampWriter(log_fp, sys.__stderr__ or _devnull, _log_io_lock)  # type: ignore
    print(f"--------     NotmyFault Engine     --------")
    print(f"------ {datetime.now().isoformat()} ------")
    print(f"------     Welcome to NotmyFault!    ------")
    return log_path


def _notify_dashboard_quit():
    """通知 Dashboard 控制端口关闭自身，让 UI 与引擎一起退出"""
    try:
        token = (
            ApplicationPaths.default()
            .dashboard_control_token_file.read_text(encoding="utf-8")
            .strip()
        )
        if len(token) != 64:
            return
        int(token, 16)
        with socket.create_connection(
            ("127.0.0.1", _DASHBOARD_CONTROL_PORT), timeout=1
        ) as client:
            client.settimeout(1)
            client.sendall(token.encode("ascii") + b" " + _DASHBOARD_QUIT + b"\n")
            client.makefile("rb").readline(32)
    except (OSError, UnicodeError, ValueError):
        pass


def _open_dashboard():
    dashboard_pyw = os.path.join(PROJECT_ROOT, "dashboard.pyw")
    if os.path.exists(dashboard_pyw):
        try:
            from notmyfault.platform.platform_support import launch_python_entry
            print(dashboard_pyw)
            launch_python_entry(dashboard_pyw)
            return
        except Exception:
            pass
    print("[Dashboard] 找不到可启动的 Dashboard 入口", file=sys.stderr)


class EngineRunner:
    """桌面后台连接运行时、HTTP API、托盘与进程"""

    def __init__(self, paths, store, engine_factory=create_engine):
        self._paths = paths
        self._store = store
        self._api = None
        self._tray = None
        self._api_socket = None
        self._force_exit_armed = threading.Event()
        self._runtime = RuntimeController(
            lambda on_event=None: engine_factory(
                store=self._store,
                on_event=on_event,
            ),
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
        """返回上次引擎启动或运行失败的原因，供 Dashboard 展示"""
        return self._runtime.status().last_error

    def _signal_handler(self, signum, frame):
        print(f"\n[Engine] 收到关闭信号 ({signum})，正在关闭...")
        self._runtime.request_stop()

    def _handle_engine_state(self, state: str) -> None:
        if self._tray:
            self._tray.set_engine_state(state)
        if self._api:
            self._api.publish_event("engine_state_changed", {"state": state})

    def _handle_engine_failure(self, error: Exception) -> None:
        if self._tray:
            self._tray.show_balloon("引擎异常", f"引擎线程崩溃: {error}", 3)
        try:
            from notmyfault.host.alert import alert_user
            alert_user("引擎异常退出", f"引擎线程崩溃: {error}", open_dashboard=True)
        except Exception:
            pass

    def start_engine(self) -> bool:
        """启动引擎线程并返回是否创建新线程，停机线程未收尾时返回 False"""
        return self._runtime.start()

    def stop_engine(self) -> bool:
        return self._runtime.stop(timeout=5)

    def _toggle_engine(self):
        if self.engine_state == "running":
            self.stop_engine()
        elif self.engine_state == "stopped":
            self.start_engine()
        else:
            print(f"[Tray] 引擎正处于 {self.engine_state}，忽略重复切换")

    def _tray_exit(self):
        """退出时依次关闭引擎、HTTP 服务、Dashboard 和进程"""
        print("[Tray] 用户请求退出")
        _notify_dashboard_quit()
        self.stop_engine()
        self.request_process_shutdown(force_after=5)

    def request_process_shutdown(self, force_after: float = 10) -> None:
        """关闭托盘和 API，等待超时后强制结束进程"""
        if self.engine_thread and self.engine_thread.is_alive():
            self._runtime.request_stop()
        else:
            self.shutdown_event.set()
        if self._api:
            self._api.stop()
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

    @staticmethod
    def _check_already_running(port: int = 19198) -> bool:
        """连接本地端口判断是否已有实例运行"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", port))
            return True

        except (socket.error, OSError):
            return False

    @staticmethod
    def _claim_api_socket(port: int = 19198):
        """在启动触发器前独占 API 端口"""
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

    def run(self):
        log_dir = str(self._paths.logs_dir)
        log_path = setup_logging(log_dir)

        print("=" * 50)
        print(" 拉起 NotmyFault Engine ......")
        print("=" * 50)
        print(f"  日志目录: {log_dir}")
        print(f"  当前日志: {log_path}")

        # 先独占监听端口再启动引擎，先调用 connect() 再 bind() 会让两个实例同时通过检查
        self._api_socket = self._claim_api_socket()
        if self._api_socket is None:
            if self._check_already_running():
                try:
                    print("[Engine] 已有实例或其他服务占用 127.0.0.1:19198，拒绝重复启动")
                    print("[Engine] 拉起 Dashboard ......")
                    _open_dashboard()
                except Exception as e:
                    print(f"[Alert] 拉起 Dashboard 失败: {e}", file=sys.stderr)
                    print(
                        "[Engine] 无法独占 127.0.0.1:19198，拒绝启动",
                        file=sys.stderr,
                    )
            return

        try:
            api_token_store = ApiTokenStore(self._paths.api_token_file)
            ai_key_store = AIKeyStore(self._paths.ai_api_key_file)
            run_history = RunHistory(str(self._paths.run_history_file))
            event_broker = EventBroker(run_history)
            self._api = create_api_server(
                engine_runner=self,
                store=self._store,
                paths=self._paths,
                token_store=api_token_store,
                ai_key_store=ai_key_store,
                plugin_file_system=PluginFileSystem(),
                pending_previews=PendingPreviewStore(),
                plugin_temporary_storage=PluginTemporaryStorage(),
                plugin_registry=PluginRegistryClient(),
                run_history=run_history,
                event_broker=event_broker,
            )
            self._runtime.set_event_sink(self._api.publish_event)

            print("[启动] 启动主引擎...")
            self.start_engine()

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

            # serve 会一直运行到 uvicorn 退出
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
                self.request_process_shutdown(force_after=5)
        if self._tray:
            self._tray.show_balloon("NotmyFault", "引擎已停止", 1)
        if self._api_socket is not None:
            try:
                self._api_socket.close()
            except OSError:
                pass
            self._api_socket = None
        print("[Cleanup] Done! ")
        print(f"--- 引擎关闭! {datetime.now().isoformat()} ---")


def main():
    # 主进程拒绝管理员令牌，插件提权统一走 notmyfault.security.sudo 的 UAC 授权
    from notmyfault.security.security import is_admin_process
    if is_admin_process():
        print(
            "[Engine] [!!] NotmyFault 不允许以管理员身份启动：请用普通用户权限运行"
            "插件需要提权时将通过弹出 UAC 授权",
            file=sys.stderr,
        )
        raise SystemExit(1)
    paths = ApplicationPaths.default()
    store = SignedConfigStore(paths)
    runner = EngineRunner(paths, store)
    runner.run()


if __name__ == "__main__":
    if "--enable-autostart" in sys.argv or "--disable-autostart" in sys.argv:
        if os.name == "nt":
            raise SystemExit("出于安全考虑，请通过 Windows 托盘菜单管理开机自启")
        from notmyfault.platform.platform_support import set_linux_autostart
        enabled = "--enable-autostart" in sys.argv
        set_linux_autostart(enabled, PROJECT_ROOT)
        print("已启用开机自启" if enabled else "已关闭开机自启")
    elif "--dashboard" in sys.argv:
        _open_dashboard()
    else:
        main()
