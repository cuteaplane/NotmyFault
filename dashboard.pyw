"""
NotmyFault Dashboard — 独立 UI 进程
pywebview 窗口 + dashboard.html，通过 HTTP API 与引擎通信。
"""
import json
import os
import sys
import urllib.request
import functools
import http.server
import socketserver
import threading

DASHBOARD_PORT = 19199

# 确保能导入 notmyfault 包
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import webview

API = "http://127.0.0.1:19198"
CONFIG_FILE = os.path.join(os.environ.get("APPDATA", ""), "NotmyFault", "config.json")
API_TOKEN_FILE = os.path.join(os.environ.get("TEMP", ""), "notmyfault_api_token")


class DashboardAPI:
    """暴露给前端 JS 的 Python 接口"""

    def launch_engine(self) -> dict:
        """启动引擎。开发模式启动 NOTMYFAULT.pyw，exe 模式启动自身 --engine。"""
        if getattr(sys, "frozen", False):
            try:
                import subprocess
                subprocess.Popen([sys.executable])
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        pyw = os.path.join(PROJECT_ROOT, "NOTMYFAULT.pyw")
        if not os.path.exists(pyw):
            return {"ok": False, "error": f"找不到 {pyw}"}
        try:
            os.startfile(pyw)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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
            from notmyfault.config import save_config as _save
            ok = _save({"rules": rules})
            return {"ok": ok}
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
        """发送带认证的 HTTP 请求"""
        token = self._get_api_token()
        try:
            body = None
            if data is not None:
                import json as _j
                body = _j.dumps(data).encode("utf-8")
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
            return {"ok": False, "error": f"HTTP {e.code}: {detail}", "token_found": bool(token)}
        except Exception as e:
            return {"ok": False, "error": str(e), "token_found": bool(token)}

    def get_api_token(self) -> str:
        """暴露给 JS bridge 的 API Token 读取方法"""
        return self._get_api_token()

    def stop_engine(self) -> dict:
        return self._auth_request("/api/engine/stop")

    def shutdown_engine(self) -> dict:
        """彻底退出引擎进程"""
        return self._auth_request("/api/engine/shutdown")
    def fetch_api(self, path: str) -> dict:
        """代理 API 请求"""
        try:
            req = urllib.request.urlopen(f"{API}{path}", timeout=5)
            return json.loads(req.read())
        except Exception as e:
            return {"_error": str(e)}

    # ---- 日志读取 (bridge 直读文件，不依赖 API) ----

    _LOG_DIR = os.path.join(os.path.dirname(CONFIG_FILE), "logs")

    def _get_latest_log(self):
        """返回最新日志文件路径，没有则返回 None。"""
        from notmyfault.logging import get_latest_log
        return get_latest_log(self._LOG_DIR)

    def read_log_entries(self, lines: int = 500) -> list:
        """读取最新日志末尾 N 行，返回解析后的结构化条目列表。"""
        try:
            from notmyfault.logging import read_log_entries as _read
            log_path = self._get_latest_log()
            if not log_path:
                return [{"ts": "", "level": "INFO", "text": "还没有日志文件，请启动引擎", "data": None}]
            return _read(log_path, lines=lines)
        except Exception as e:
            return [{"ts": "", "level": "ERROR", "text": f"读取日志失败: {e}", "data": None}]

    def read_diagnostics(self) -> dict:
        """从最新日志文件构建诊断摘要。"""
        try:
            from notmyfault.logging import read_log_entries as _read, build_diagnostics
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
            from notmyfault.logging import list_logs
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
    """优先使用 dashboard/dist 构建产物，回退旧 dashboard.html。"""
    dist = os.path.join(PROJECT_ROOT, "dashboard", "dist")
    if not (os.path.isdir(dist) and os.path.exists(os.path.join(dist, "index.html"))):
        _ensure_dashboard_build()
    if os.path.isdir(dist) and os.path.exists(os.path.join(dist, "index.html")):
        httpd, url = _start_static_server(dist)
        if url:
            return url, httpd
        print("[Dashboard] 静态服务器启动失败，回退到单文件模式", file=sys.stderr)
    legacy = os.path.join(PROJECT_ROOT, "dashboard.html")
    if os.path.exists(legacy):
        return legacy, None
    return None, None

def main():
    # 注册协议（幂等，每次启动都确保存在）
    try:
        from Win_toaster.AUMID_Register import register_protocol
        register_protocol()
    except Exception:
        pass

    dashboard_url, _static_httpd = _resolve_dashboard_url()
    if not dashboard_url:
        print("[Dashboard] 找不到 dashboard/dist 构建产物，也找不到 dashboard.html", file=sys.stderr)
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
    )

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
        webview.start()
    except KeyboardInterrupt:
        pass
    print("[Dashboard] 已退出")


if __name__ == "__main__":
    # 处理协议调用: notmyfault://dashboard
    if "--protocol" in sys.argv:
        print("[Dashboard] 通过协议启动")
    main()
