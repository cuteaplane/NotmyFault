"""NotmyFault 的独立 Dashboard 进程，通过 HTTP API 与引擎通信"""
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
import secrets

DASHBOARD_PORT = 19199
DASHBOARD_CONTROL_PORT = 19197
_CONTROL_SHOW = b"NMF_DASHBOARD_SHOW_V2"
_CONTROL_OK = b"NMF_DASHBOARD_OK_V1"
_CONTROL_QUIT = b"NMF_DASHBOARD_QUIT_V2"

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import webview
from notmyfault.application_paths import ApplicationPaths
from notmyfault.config import SignedConfigStore
from notmyfault.host.api.auth import ApiTokenStore
from notmyfault.platform.platform_support import launch_python_entry
from notmyfault.security.plugin_schema import scan_plugins

def _patch_qt_permission_policy():
    try:
        from webview.platforms import qt
    except ImportError:
        return

    page = qt.BrowserView.WebPage
    policy = page.PermissionPolicy
    feature = page.Feature
    media_features = (
        feature.MediaAudioCapture,
        feature.MediaVideoCapture,
        feature.MediaAudioVideoCapture,
    )

    def handle_permission(self, url, requested_feature):
        local_page = url.scheme() == "http" and url.host() in {"127.0.0.1", "localhost"}
        allowed = requested_feature in media_features or (
            requested_feature == feature.ClipboardReadWrite and local_page
        )
        permission = (
            policy.PermissionGrantedByUser if allowed else policy.PermissionDeniedByUser
        )
        self.setFeaturePermission(url, requested_feature, permission)

    page.onFeaturePermissionRequested = handle_permission


API = "http://127.0.0.1:19198"
def _get_plugins_schema(paths: ApplicationPaths) -> dict:
    base = str(paths.package_root)
    result = {
        "triggers": scan_plugins(base, "triggers", "trigger.json"),
        "actions": scan_plugins(base, "actions", "action.json"),
    }
    user_dir = str(paths.user_plugins_dir)
    if os.path.isdir(user_dir):
        for plugin_type in ("triggers", "actions"):
            filename = "trigger.json" if plugin_type == "triggers" else "action.json"
            for plugin_id, meta in scan_plugins(
                user_dir, plugin_type, filename,
            ).items():
                result[plugin_type].setdefault(plugin_id, meta)
    return result


def _read_control_secret(path) -> bytes:
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) != 64:
            return b""
        int(value, 16)
        return value.encode("ascii")
    except (OSError, UnicodeError, ValueError):
        return b""


def _send_control_command(path, command: bytes, port: int) -> bool:
    secret = _read_control_secret(path)
    if not secret:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
            client.settimeout(1)
            client.sendall(secret + b" " + command + b"\n")
            response = client.makefile("rb").readline(64).rstrip(b"\r\n")
            return response == _CONTROL_OK
    except OSError:
        return False


def _remove_control_secret(path, expected: str) -> None:
    try:
        if secrets.compare_digest(path.read_text(encoding="utf-8").strip(), expected):
            path.unlink()
    except OSError:
        pass


def _claim_dashboard_instance(control_token_path, port: int = DASHBOARD_CONTROL_PORT):
    """占用 Dashboard 控制端口，已有实例时通知它恢复到前台"""
    show_requested = threading.Event()
    quit_requested = threading.Event()
    control_secret = secrets.token_hex(32)

    class ControlHandler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                self.request.settimeout(1)
                message = self.request.makefile("rb").readline(128).rstrip(b"\r\n")
                candidate, separator, command = message.partition(b" ")
                authenticated = separator and secrets.compare_digest(
                    candidate.decode("ascii"), control_secret,
                )
                if authenticated and command == _CONTROL_SHOW:
                    show_requested.set()
                    self.request.sendall(_CONTROL_OK + b"\n")
                elif authenticated and command == _CONTROL_QUIT:
                    # 引擎或托盘退出时让 Dashboard 一起退出
                    quit_requested.set()
                    self.request.sendall(_CONTROL_OK + b"\n")
            except (OSError, UnicodeDecodeError):
                pass

    class ControlServer(socketserver.ThreadingTCPServer):
        # Windows 的 SO_REUSEADDR 会允许多个进程绑定同一端口，监听 socket 需保持独占
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
            if _send_control_command(control_token_path, _CONTROL_SHOW, port):
                return None, None, None, None
            if attempt < 4:
                time.sleep(0.1)
    else:
        raise RuntimeError(
            f"Dashboard 控制端口 {port} 被其他程序占用"
        ) from last_error

    try:
        token_store = ApiTokenStore(control_token_path, token=control_secret)
        token_store.repair_file()
    except Exception as error:
        server.server_close()
        raise RuntimeError("Dashboard 控制令牌无法安全写入") from error
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, show_requested, quit_requested, control_secret


class DashboardAPI:
    """提供给前端 JavaScript 调用的 Python 接口"""

    def __init__(
        self,
        store: SignedConfigStore | None = None,
        paths: ApplicationPaths | None = None,
    ) -> None:
        self._paths = paths or ApplicationPaths.default()
        self._store = store or SignedConfigStore(self._paths)
        self._window = None

    def set_window_state(self, action: str) -> dict:
        if action not in ("minimize", "restore"):
            return {"ok": False, "error": "不支持的窗口操作"}
        if self._window is None:
            return {"ok": False, "error": "Dashboard 窗口尚未就绪"}
        try:
            if action == "minimize":
                self._window.minimize()
            else:
                self._window.restore()
                self._window.show()
            return {"ok": True, "action": action}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def launch_engine(self) -> dict:
        """保证后台服务在线，并启动自动化核心"""
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

        # 启动与令牌文件发布均为异步，bridge 在这里统一等待让 Vue 只维护一套状态
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
        """有效规则先补齐身份，验签失败时保留原文供安全页核对"""
        try:
            if self._paths.rules_file.exists():
                with open(self._paths.rules_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                rules = data.get("rules", []) if isinstance(data, dict) else []
                rules = rules if isinstance(rules, list) else []
                try:
                    normalized = self._store.load_verified_rules()
                    if normalized != rules:
                        self._store.save_rules(normalized)
                    return {"rules": normalized}
                except Exception:
                    return {"rules": rules}
        except Exception as e:
            return {"_error": str(e), "rules": []}
        return {"rules": []}

    def save_config(self, rules: list, admin_key_password: str = "") -> dict:
        try:
            from notmyfault.config import (
                ConfigValidationError,
                normalize_rules,
                validate_rules_safety,
            )
            from notmyfault.core.rules import (
                validate_rule_bindings,
                validate_rules_structure,
            )
            normalized_rules = normalize_rules(rules)
            structure_errors = validate_rules_structure(normalized_rules)
            if structure_errors:
                return {
                    "ok": False,
                    "error": "规则结构校验失败",
                    "details": structure_errors[:10],
                }
            schema = _get_plugins_schema(self._paths)
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
            _warnings, errors = validate_rules_safety(normalized_rules)
            if errors:
                return {
                    "ok": False,
                    "error": "规则安全校验失败",
                    "details": errors[:10],
                }
            previous_rules = []
            if self._paths.rules_file.exists():
                try:
                    previous_rules = self._store.load_verified_rules()
                except ConfigValidationError as error:
                    return {
                        "ok": False,
                        "error": f"现有规则未通过完整性校验: {error}",
                    }
            from notmyfault.security.rule_approval import (
                AdminRuleApprovalError,
                require_admin_rule_approval,
            )
            try:
                require_admin_rule_approval(
                    previous_rules,
                    normalized_rules,
                    schema,
                    admin_key_password,
                )
            except AdminRuleApprovalError as error:
                return {
                    "ok": False,
                    "code": error.code,
                    "error": str(error),
                    "plugins": error.plugins,
                }
            ok = self._store.save_rules(normalized_rules)
            return {"ok": ok, "rules": normalized_rules if ok else None}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _get_api_token(self) -> str:
        try:
            with open(self._paths.api_token_file, "r") as f:
                return f.read().strip()
        except (OSError, IOError):
            return ""

    def _auth_request(
        self,
        path: str,
        method: str = "POST",
        data: dict = None,
        timeout: float = 5,
    ) -> dict:
        """发送带认证的 HTTP 请求，认证失败时重读令牌并重试一次"""
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
                return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")
                try:
                    error_payload = json.loads(detail)
                except (TypeError, ValueError):
                    error_payload = {}
                if not isinstance(error_payload, dict):
                    error_payload = {}
                last_error = error_payload.get("error") or f"HTTP {e.code}: {detail}"
                if e.code == 403 and attempt == 0:
                    time.sleep(0.05)
                    continue
                return {
                    **error_payload,
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
        """通过 pywebview bridge 转发 Dashboard 的 JSON API 请求"""
        if not isinstance(path, str) or not path.startswith("/api/"):
            return {"ok": False, "error": "无效的 API 路径", "status": 400}
        timeout = 30 if path == "/api/rules/draft/ai" else 5
        return self._auth_request(path, method.upper(), data, timeout=timeout)

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
        return self._get_api_token()

    def select_folder(self, initial_path: str = "") -> str:
        """通过桌面窗口选择本地目录并返回路径"""
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

    def _get_latest_log(self):
        from notmyfault.core.logging import get_latest_log
        return get_latest_log(str(self._paths.logs_dir))

    def read_log_entries(self, lines: int = 500) -> list:
        """读取最新日志末尾 N 行，返回解析后的结构化条目列表"""
        try:
            from notmyfault.core.logging import read_log_entries as _read
            log_path = self._get_latest_log()
            if not log_path:
                return [{"ts": "", "level": "INFO", "text": "还没有日志文件，请启动引擎", "data": None}]
            return _read(log_path, lines=lines)
        except Exception as e:
            return [{"ts": "", "level": "ERROR", "text": f"读取日志失败: {e}", "data": None}]

    def read_diagnostics(self) -> dict:
        """从最新日志文件构建诊断摘要"""
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
        """读取最新日志文件原始文本，供日志查看器使用"""
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
        """列出所有日志文件信息"""
        try:
            from notmyfault.core.logging import list_logs
            return list_logs(self._LOG_DIR)
        except Exception as e:
            return []

    def read_log_file_entries(self, name: str, lines: int = 600) -> list:
        """读取指定历史日志文件末尾 N 行，返回解析后的结构化条目列表"""
        try:
            # 文件名只认 engine-*.log，堵住 ../ 之类构造出来的路径
            if (
                not isinstance(name, str)
                or os.path.basename(name) != name
                or not (name.startswith("engine-") and name.endswith(".log"))
            ):
                return []
            log_path = os.path.join(self._LOG_DIR, name)
            if not os.path.isfile(log_path):
                return []
            from notmyfault.core.logging import read_log_entries as _read
            return _read(log_path, lines=lines)
        except Exception as e:
            return [{"ts": "", "level": "ERROR", "text": f"读取日志失败: {e}", "data": None}]



class _DashboardStaticHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


def _start_static_server(directory, port=DASHBOARD_PORT):
    """pywebview 从 file:// 加载 ES 模块会被 CORS 拦截，因此用本地 HTTP 服务托管并在端口占用时递增重试"""
    Handler = functools.partial(_DashboardStaticHandler, directory=directory)
    for p in range(port, port + 20):
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", p), Handler)
            httpd.daemon_threads = True
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            version = int(os.path.getmtime(os.path.join(directory, "index.html")))
            return httpd, f"http://127.0.0.1:{p}/?v={version}"
        except OSError:
            continue
    return None, None


def _ensure_dashboard_build():
    """构建产物不存在时运行 npm run build 并返回是否成功"""
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
    """准备构建产物和静态服务器并返回 Dashboard 地址"""
    dist = os.path.join(PROJECT_ROOT, "dashboard", "dist")
    if not (os.path.isdir(dist) and os.path.exists(os.path.join(dist, "index.html"))):
        _ensure_dashboard_build()
    if os.path.isdir(dist) and os.path.exists(os.path.join(dist, "index.html")):
        httpd, url = _start_static_server(dist)
        if url:
            return url, httpd
    return None, None

def main():
    if sys.platform.startswith("linux"):
        _patch_qt_permission_policy()

    paths = ApplicationPaths.default()
    try:
        control_server, show_requested, quit_requested, control_secret = (
            _claim_dashboard_instance(paths.dashboard_control_token_file)
        )
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

    # 每次启动都调用协议注册器，注册器内部保持幂等
    try:
        from Win_toaster.AUMID_Register import register_protocol
        register_protocol()
    except Exception:
        pass

    dashboard_url, _static_httpd = _resolve_dashboard_url()
    if not dashboard_url:
        print("[Dashboard] 找不到 dashboard/dist 构建产物", file=sys.stderr)
        control_server.shutdown()
        control_server.server_close()
        _remove_control_secret(paths.dashboard_control_token_file, control_secret)
        return
    icon_path = os.path.join(PROJECT_ROOT, "logo.ico")

    api = DashboardAPI(SignedConfigStore(paths), paths)

    window = webview.create_window(
        title="NotmyFault",
        url=dashboard_url,
        js_api=api,
        width=960,
        height=720,
        min_size=(640, 480),
        confirm_close=False,
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

    # Windows 进程需要设置应用图标
    if os.name == "nt" and os.path.exists(icon_path):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "notmyfault.dashboard"
            )
        except Exception:
            pass

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
    _remove_control_secret(paths.dashboard_control_token_file, control_secret)
    print("[Dashboard] 已退出")


if __name__ == "__main__":
    if "--engine" in sys.argv:
        engine_entry = os.path.join(PROJECT_ROOT, "NOTMYFAULT.pyw")
        if not os.path.exists(engine_entry):
            raise SystemExit("找不到 NOTMYFAULT.pyw 引擎入口")
        import runpy
        runpy.run_path(engine_entry, run_name="__main__")
    else:
        if "--protocol" in sys.argv:
            print("[Dashboard] 通过协议启动")
        main()
