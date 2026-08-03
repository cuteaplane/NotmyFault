"""显示引擎告警通知，并可通过 notmyfault:// 打开 Dashboard。"""

import os
import sys
import threading
import time

from notmyfault.platform.platform_support import launch_python_entry, show_notification

if os.name == "nt":
    from windows_toasts import Toast, ToastButton
    _active_toasts: list[Toast] = []
    _toasts_lock = threading.Lock()


def _dashboard_pyw_path() -> str:
    """返回 dashboard.pyw 的绝对路径"""
    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    return os.path.join(project_root, "dashboard.pyw")


def _launch_dashboard() -> None:
    dashboard_pyw = _dashboard_pyw_path()
    if not os.path.exists(dashboard_pyw):
        print(f"[Alert] 找不到 dashboard 入口: {dashboard_pyw}", file=sys.stderr)
        return
    try:
        launch_python_entry(dashboard_pyw)
        print("[Alert] 已拉起 Dashboard")
    except Exception as e:
        print(f"[Alert] 拉起 Dashboard 失败: {e}", file=sys.stderr)


def alert_user(
    title: str,
    message: str,
    open_dashboard: bool = True,
    display_seconds: int = 15,
) -> None:
    """显示告警通知，open_dashboard 控制是否打开 Dashboard，display_seconds 控制显示时长。"""
    if os.name == "nt":
        try:
            _show_windows_alert(title, message, display_seconds)
        except Exception as e:
            print(f"[Alert] 通知发送失败: {e}", file=sys.stderr)
    else:
        show_notification(f"[!] {title}", message)

    if open_dashboard:
        threading.Thread(target=_launch_dashboard, daemon=True).start()


def _show_windows_alert(title: str, message: str, display_seconds: int) -> None:
    """发送带 Dashboard 操作按钮的 Windows Toast"""
    from Win_toaster.show_notification import toaster

    toast = Toast([f"[!] {title}", message])

    btn = ToastButton("打开控制面板")
    btn.launch = "notmyfault://dashboard"
    toast.AddAction(btn)

    def _on_activated(args):
        _launch_dashboard()

    toast.on_activated = _on_activated

    with _toasts_lock:
        _active_toasts.append(toast)

    toaster.show_toast(toast)

    def _keepalive_and_cleanup():
        deadline = time.time() + display_seconds + 2
        while time.time() < deadline:
            time.sleep(0.5)
        try:
            toaster.remove_toast(toast)
        except Exception:
            pass
        try:
            with _toasts_lock:
                _active_toasts.remove(toast)
        except ValueError:
            pass

    threading.Thread(target=_keepalive_and_cleanup, daemon=True).start()
