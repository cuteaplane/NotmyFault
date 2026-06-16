"""
NotmyFault Dashboard — 独立 UI 进程
pywebview 窗口 + dashboard.html，通过 HTTP API 与引擎通信。
"""
import json
import os
import sys
import urllib.request

# 确保能导入 notmyfault 包
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import webview

API = "http://127.0.0.1:19198"
CONFIG_FILE = os.path.join(os.environ.get("APPDATA", ""), "NotmyFault", "config.json")


class DashboardAPI:
    """暴露给前端 JS 的 Python 接口"""

    def launch_engine(self) -> dict:
        """启动 NOTMYFAULT.pyw — os.startfile 完全独立，父进程退出后不受影响"""
        pyw = os.path.join(PROJECT_ROOT, "NOTMYFAULT.pyw")
        if not os.path.exists(pyw):
            return {"ok": False, "error": f"找不到 {pyw}"}
        try:
            os.startfile(pyw)  # Windows 原生"双击打开"，与父进程彻底无关
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
        """直接写入 JSON 配置文件"""
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"rules": rules}, f, ensure_ascii=False, indent=4)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def stop_engine(self) -> dict:
        """停止引擎（通过 bridge 代理 POST，避免 pywebview 的 CORS 限制）"""
        try:
            req = urllib.request.Request(
                f"{API}/api/engine/stop", method="POST"
            )
            return json.loads(urllib.request.urlopen(req, timeout=5).read())
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def shutdown_engine(self) -> dict:
        """彻底退出引擎进程"""
        try:
            req = urllib.request.Request(
                f"{API}/api/engine/shutdown", method="POST"
            )
            return json.loads(urllib.request.urlopen(req, timeout=5).read())
        except Exception as e:
            return {"ok": False, "error": str(e)}

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


def main():
    # 注册协议（幂等，每次启动都确保存在）
    try:
        from Win_toaster.AUMID_Register import register_protocol
        register_protocol()
    except Exception:
        pass

    dashboard_path = os.path.join(PROJECT_ROOT, "dashboard.html")
    icon_path = os.path.join(PROJECT_ROOT, "logo.ico")

    api = DashboardAPI()

    window = webview.create_window(
        title="NotmyFault",
        url=dashboard_path,
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
