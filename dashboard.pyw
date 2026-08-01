"""
NotmyFault Dashboard — 独立 UI 进程
pywebview 窗口 + dashboard.html，通过 HTTP API 与引擎通信。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import functools
import http.server
import socketserver
import socket
import threading

DASHBOARD_PORT = 19199
DASHBOARD_CONTROL_PORT = 19197
_CONTROL_SHOW = b"NMF_DASHBOARD_SHOW_V1"
_CONTROL_OK = b"NMF_DASHBOARD_OK_V1"
_CONTROL_QUIT = b"NMF_DASHBOARD_QUIT_V1"

# 确保能导入 notmyfault 包
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import webview
from notmyfault.config import CONFIG_FILE
from notmyfault.platform.platform_support import get_config_dir, launch_python_entry
from notmyfault.security.plugin_schema import scan_plugins

API = "http://127.0.0.1:19198"
# 必须与 notmyfault/api_server.py 的 API_TOKEN_FILE 保持一致：
# token 写在 config.json 同目录下（%APPDATA%/NotmyFault/.api_token），
# 不再用 %TEMP%/notmyfault_api_token（旧路径，Authenticated Users 可读，已废弃）。
API_TOKEN_FILE = os.path.join(os.path.dirname(CONFIG_FILE), ".api_token")


def _get_plugins_schema() -> dict:
    base = os.path.join(PROJECT_ROOT, "notmyfault")
    result = {
        "triggers": scan_plugins(base, "triggers", "trigger.json"),
        "actions": scan_plugins(base, "actions", "action.json"),
    }
    user_dir = os.path.join(get_config_dir(), "plugins")
    if os.path.isdir(user_dir):
        for plugin_type in ("triggers", "actions"):
            filename = "trigger.json" if plugin_type == "triggers" else "action.json"
            for plugin_id, meta in scan_plugins(
                user_dir, plugin_type, filename,
            ).items():
                result[plugin_type].setdefault(plugin_id, meta)
    return result


def _claim_dashboard_instance(port: int = DASHBOARD_CONTROL_PORT):
    """占用 Dashboard 控制端口；已有窗口时通知它恢复到前台。"""
    show_requested = threading.Event()
    quit_requested = threading.Event()

    class ControlHandler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                message = self.request.makefile("rb").readline(64).rstrip(b"\r\n")
                if message == _CONTROL_SHOW:
                    show_requested.set()
                    self.request.sendall(_CONTROL_OK + b"\n")
                elif message == _CONTROL_QUIT:
                    # 引擎/托盘退出时通知 Dashboard 关闭自身（UI 与引擎一起退出）
                    quit_requested.set()
                    self.request.sendall(_CONTROL_OK + b"\n")
            except OSError:
                pass

    class ControlServer(socketserver.ThreadingTCPServer):
        # Windows 上 SO_REUSEADDR 允许多个进程同时绑定同一端口，恰好违背
        # 单实例目标；必须使用独占绑定。
        allow_reuse_address = False
        allow_reuse_port = False
        daemon_threads = True

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_EXCLUSIVEADDRUSE,
                    1,
                )
            super().server_bind()

    last_error = None
    for attempt in range(5):
        try:
            server = ControlServer(
                ("127.0.0.1", port),
                ControlHandler,
            )
            break
        except OSError as error:
            last_error = error
            try:
                with socket.create_connection(
                    ("127.0.0.1", port), timeout=1
                ) as client:
                    client.sendall(_CONTROL_SHOW + b"\n")
                    response = client.makefile("rb").readline(64).rstrip(b"\r\n")
                    if response == _CONTROL_OK:
                        return None, None, None
            except OSError:
                pass
            if attempt < 4:
                time.sleep(0.1)
    else:
        raise RuntimeError(
            f"Dashboard 控制端口 {port} 被其他程序占用"
        ) from last_error

    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, show_requested, quit_requested
    return server, show_requested


class DashboardAPI:
    """暴露给前端 JS 的 Python 接口"""

    def __init__(self):
        # pywebview 会检查 bridge API 的公开属性；不能把原生 Window 放在
        # self.window 上，否则它会递归枚举 AccessibilityObject 并卡死。
        self._window = None

    def launch_engine(self) -> dict:
        """确保后台服务在线，并启动自动化核心。"""
        status = self.get_engine_status()
        if status.get("api_alive"):
            if status.get("engine_state") in ("running", "starting"):
                return {"ok": True, **status}
            return self._auth_request("/api/engine/start")

        if getattr(sys, "frozen", False):
            try:
                import subprocess
                executable_dir = os.path.dirname(sys.executable)
                current = os.path.normcase(os.path.abspath(sys.executable))
                sibling_names = (
                    ("NotmyFault.exe", "engine.exe")
                    if os.name == "nt"
                    else ("NotmyFault", "engine", "notmyfault-engine")
                )
                sibling = next(
                    (
                        candidate
                        for candidate in (
                            os.path.join(executable_dir, name)
                            for name in sibling_names
                        )
                        if os.path.isfile(candidate)
                        and os.path.normcase(os.path.abspath(candidate)) != current
                    ),
                    None,
                )
                if sibling:
                    command = [sibling]
                elif os.path.exists(os.path.join(PROJECT_ROOT, "NOTMYFAULT.pyw")):
                    command = [sys.executable, "--engine"]
                else:
                    return {
                        "ok": False,
                        "error": "打包目录中缺少 NotmyFault 引擎入口",
                    }
                subprocess.Popen(
                    command,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as e:
                return {"ok": False, "error": str(e)}
        else:
            pyw = os.path.join(PROJECT_ROOT, "NOTMYFAULT.pyw")
            if not os.path.exists(pyw):
                return {"ok": False, "error": f"找不到 {pyw}"}
            try:
                launch_python_entry(pyw)
            except Exception as e:
                return {"ok": False, "error": str(e)}

        # 启动与 token 文件发布均为异步；bridge 在这里统一等待，不让 Vue
        # 同时维护另一套轮询状态机。
        deadline = time.monotonic() + 15
        last_error = ""
        while time.monotonic() < deadline:
            status = self.get_engine_status()
            if status.get("api_alive"):
                return {"ok": True, **status}
            last_error = status.get("error", "")
            time.sleep(0.25)
        return {"ok": False, "error": last_error or "后台服务启动超时"}

    def get_config(self) -> dict:
        """直接读取 JSON 配置文件，文件不存在则返回默认规则"""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            return {"_error": str(e), "rules": []}
        return {"rules": []}

    def save_config(self, rules: list) -> dict:
        try:
            from notmyfault.config import (
                save_config as _save,
                _normalize_config,
                _validate_rules_safety,
            )
            from notmyfault.core.rules import (
                validate_rule_bindings,
                validate_rules_structure,
            )
            config = self.get_config()
            if not isinstance(config, dict) or config.get("_error"):
                config = {}
            config.pop("_signature", None)
            config["rules"] = rules
            config = _normalize_config(config)
            normalized_rules = config.get("rules", [])
            structure_errors = validate_rules_structure(normalized_rules)
            if structure_errors:
                return {
                    "ok": False,
                    "error": "规则结构校验失败",
                    "details": structure_errors[:10],
                }
            schema = _get_plugins_schema()
            binding_issues = []
            for index, rule in enumerate(normalized_rules):
                for issue in validate_rule_bindings(
                    rule, schema["triggers"], schema["actions"],
                ):
                    binding_issues.append({
                        "rule": rule.get("name", f"规则 #{index + 1}"),
                        **issue,
                    })
            if binding_issues:
                return {
                    "ok": False,
                    "error": "规则数据绑定无效",
                    "details": binding_issues[:20],
                }
            _warnings, errors = _validate_rules_safety(normalized_rules)
            if errors:
                return {
                    "ok": False,
                    "error": "规则安全校验失败",
                    "details": errors[:10],
                }
            ok = _save(config)
            return {"ok": ok, "rules": normalized_rules if ok else None}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _get_api_token(self) -> str:
        """读取 API 认证令牌"""
        try:
            with open(API_TOKEN_FILE, "r") as f:
                return f.read().strip()
        except (OSError, IOError):
            return ""

    def _auth_request(self, path: str, method: str = "POST", data: dict = None) -> dict:
        """发送带认证的 HTTP 请求；token 漂移时重读文件并重试一次。"""
        last_error = ""
        for attempt in range(2):
            token = self._get_api_token()
            try:
                body = json.dumps(data).encode("utf-8") if data is not None else None
                req = urllib.request.Request(
                    f"{API}{path}",
                    data=body,
                    method=method,
                )
                if token:
                    req.add_header("Authorization", f"Bearer {token}")
                if data is not None:
                    req.add_header("Content-Type", "application/json")
                return json.loads(urllib.request.urlopen(req, timeout=5).read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")
                last_error = f"HTTP {e.code}: {detail}"
                if e.code == 403 and attempt == 0:
                    time.sleep(0.05)
                    continue
                return {
                    "ok": False,
                    "error": last_error,
                    "status": e.code,
                    "token_found": bool(token),
                }
            except Exception as e:
                return {
                    "ok": False,
                    "error": str(e),
                    "status": 0,
                    "token_found": bool(token),
                }
        return {"ok": False, "error": last_error, "status": 403}

    def request_api(self, path: str, method: str = "GET", data: dict = None) -> dict:
        """pywebview 的统一 JSON API 代理；Dashboard 不存在浏览器降级模式。"""
        if not isinstance(path, str) or not path.startswith("/api/"):
            return {"ok": False, "error": "无效的 API 路径", "status": 400}
        return self._auth_request(path, method.upper(), data)

    def get_engine_status(self) -> dict:
        result = self._auth_request("/api/engine/status", "GET")
        if result.get("api_alive"):
            return result
        return {
            "api_alive": False,
            "engine_running": False,
            "engine_state": "offline",
            "error": result.get("error", "后台服务未运行"),
        }

    def get_api_token(self) -> str:
        """暴露给 JS bridge 的 API Token 读取方法"""
        return self._get_api_token()

    def select_folder(self, initial_path: str = "") -> str:
        """让 Dashboard 选择本地目录；仅通过桌面 bridge 暴露。"""
        if self._window is None:
            return ""
        try:
            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=initial_path if os.path.isdir(initial_path) else "",
            )
            return result[0] if result else ""
        except Exception:
            return ""

    def stop_engine(self) -> dict:
        return self._auth_request("/api/engine/stop")

    def shutdown_engine(self) -> dict:
        """彻底退出引擎进程"""
        return self._auth_request("/api/engine/shutdown")

    # ---- 日志读取 (bridge 直读文件，不依赖 API) ----

    _LOG_DIR = os.path.join(os.path.dirname(CONFIG_FILE), "logs")

    def _get_latest_log(self):
        """返回最新日志文件路径，没有则返回 None。"""
        from notmyfault.core.logging import get_latest_log
        return get_latest_log(self._LOG_DIR)

    def read_log_entries(self, lines: int = 500) -> list:
        """读取最新日志末尾 N 行，返回解析后的结构化条目列表。"""
        try:
            from notmyfault.core.logging import read_log_entries as _read
            log_path = self._get_latest_log()
            if not log_path:
                return [{"ts": "", "level": "INFO", "text": "还没有日志文件，请启动引擎", "data": None}]
            return _read(log_path, lines=lines)
        except Exception as e:
            return [{"ts": "", "level": "ERROR", "text": f"读取日志失败: {e}", "data": None}]

    def read_diagnostics(self) -> dict:
        """从最新日志文件构建诊断摘要。"""
        try:
            from notmyfault.core.logging import read_log_entries as _read, build_diagnostics
            log_path = self._get_latest_log()
            if not log_path:
                return {"error_count": 0, "warn_count": 0, "last_errors": ["还没有日志文件，请启动引擎"]}
            entries = _read(log_path, lines=500)
            return build_diagnostics(entries)
        except Exception as e:
            return {"error_count": 1, "last_errors": [str(e)]}

    def read_log_raw(self, lines: int = 300) -> str:
        """读取最新日志文件原始文本（供日志查看器使用）。"""
        try:
            log_path = self._get_latest_log()
            if not log_path:
                return "(还没有日志文件)\n\n请先启动引擎。"
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            if not all_lines:
                return f"(日志为空)\n{log_path}"
            return "".join(all_lines[-lines:])
        except Exception as e:
            return f"读取日志失败: {e}"

    def list_log_files(self) -> list:
        """列出所有日志文件信息。"""
        try:
            from notmyfault.core.logging import list_logs
            return list_logs(self._LOG_DIR)
        except Exception as e:
            return []



def _start_static_server(directory, port=DASHBOARD_PORT):
    """后台线程托管 Vue 构建产物（多文件 ES 模块）。

    pywebview 从 file:// 加载 ES 模块会被浏览器 CORS 拦截，故改用本地 HTTP。
    端口被占用时自动 +1 重试。
    """
    Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    for p in range(port, port + 20):
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", p), Handler)
            httpd.daemon_threads = True
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            return httpd, f"http://127.0.0.1:{p}/"
        except OSError:
            continue
    return None, None


def _ensure_dashboard_build():
    """如果 dashboard/dist 不存在，自动 npm run build。"""
    dist = os.path.join(PROJECT_ROOT, "dashboard", "dist")
    if os.path.isdir(dist) and os.path.exists(os.path.join(dist, "index.html")):
        return True
    npm = os.path.join(PROJECT_ROOT, "dashboard")
    if not os.path.exists(os.path.join(npm, "package.json")):
        return False
    print("[Dashboard] 构建产物不存在，自动 npm run build...")
    try:
        import subprocess
        result = subprocess.run(
            ["npm", "run", "build"],
            cwd=npm,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode == 0:
            print("[Dashboard] npm run build 成功")
            return True
        print(f"[Dashboard] npm run build 失败 (code={result.returncode}): {result.stderr.strip()[:200]}")
    except FileNotFoundError:
        print("[Dashboard] npm 未安装，无法自动构建")
    except subprocess.TimeoutExpired:
        print("[Dashboard] npm run build 超时")
    except Exception as e:
        print(f"[Dashboard] npm run build 异常: {e}")
    return False


def _resolve_dashboard_url():
    """使用 dashboard/dist 构建产物。"""
    dist = os.path.join(PROJECT_ROOT, "dashboard", "dist")
    if not (os.path.isdir(dist) and os.path.exists(os.path.join(dist, "index.html"))):
        _ensure_dashboard_build()
    if os.path.isdir(dist) and os.path.exists(os.path.join(dist, "index.html")):
        httpd, url = _start_static_server(dist)
        if url:
            return url, httpd
    return None, None

def main():
    try:
        control_server, show_requested, quit_requested = _claim_dashboard_instance()
    except RuntimeError as error:
        print(f"[Dashboard] {error}", file=sys.stderr)
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    str(error),
                    "NotmyFault Dashboard",
                    0x10,
                )
            except Exception:
                pass
        return
    if control_server is None:
        return

    # 注册协议（幂等，每次启动都确保存在）
    try:
        from Win_toaster.AUMID_Register import register_protocol
        register_protocol()
    except Exception:
        pass

    dashboard_url, _static_httpd = _resolve_dashboard_url()
    if not dashboard_url:
        print("[Dashboard] 找不到 dashboard/dist 构建产物", file=sys.stderr)
        return
    icon_path = os.path.join(PROJECT_ROOT, "logo.ico")

    api = DashboardAPI()

    window = webview.create_window(
        title="NotmyFault",
        url=dashboard_url,
        js_api=api,
        width=960,
        height=720,
        min_size=(640, 480),
        confirm_close=False,
        # 窗口背景色：HTML 渲染前 pywebview 显示这个颜色而非默认白色。
        # 用深色（与深色模式 --md-surface-c-low 一致），深色模式零闪烁；
        # 浅色模式会闪一下深色但不如白色刺眼，且 HTML 加载后立即被正确背景覆盖。
        background_color='#1b1b21',
    )
    api._window = window

    def watch_show_requests():
        while True:
            show_requested.wait()
            show_requested.clear()
            try:
                window.restore()
                window.show()
            except Exception:
                pass

    threading.Thread(target=watch_show_requests, daemon=True).start()

    def watch_quit_requests():
        quit_requested.wait()
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=watch_quit_requests, daemon=True).start()

    # 设置窗口图标（仅 Windows）
    if os.name == "nt" and os.path.exists(icon_path):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "notmyfault.dashboard"
            )
        except Exception:
            pass

    # 窗口关闭事件 — 确保干净退出
    def on_closing():
        print("[Dashboard] 窗口正在关闭...")

    window.events.closing += on_closing
    window.events.closed += lambda: print("[Dashboard] 窗口已关闭")

    try:
        if sys.platform.startswith("linux"):
            webview.start(gui="qt")
        else:
            webview.start()
    except KeyboardInterrupt:
        pass
    control_server.shutdown()
    control_server.server_close()
    print("[Dashboard] 已退出")


if __name__ == "__main__":
    if "--engine" in sys.argv:
        engine_entry = os.path.join(PROJECT_ROOT, "NOTMYFAULT.pyw")
        if not os.path.exists(engine_entry):
            raise SystemExit("找不到 NOTMYFAULT.pyw 引擎入口")
        import runpy
        runpy.run_path(engine_entry, run_name="__main__")
    else:
        # 处理协议调用: notmyfault://dashboard
        if "--protocol" in sys.argv:
            print("[Dashboard] 通过协议启动")
        main()
