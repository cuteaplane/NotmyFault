"""
引擎告警模块
------------
当引擎发生需要用户关注的错误时，弹出 Windows 交互式通知。
点击按钮通过协议（notmyfault://）拉起 Dashboard，不依赖进程内 COM 回调。

用法:
    from notmyfault.alert import alert_user
    alert_user("触发器崩溃", "process_state 触发器线程异常退出", open_dashboard=True)
"""

import os
import sys
import threading
import time

from windows_toasts import Toast, ToastButton

# 持有活跃 toast 引用，防止 GC 回收
_active_toasts: list[Toast] = []
_toasts_lock = threading.Lock()


def _dashboard_pyw_path() -> str:
    """返回 dashboard.pyw 的绝对路径。"""
    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )
    return os.path.join(project_root, "dashboard.pyw")


def _launch_dashboard() -> None:
    """启动 Dashboard；冻结态优先使用同目录 UI，可回退统一入口参数。"""
    if getattr(sys, "frozen", False):
        try:
            import subprocess
            executable_dir = os.path.dirname(sys.executable)
            current = os.path.normcase(os.path.abspath(sys.executable))
            sibling = next(
                (
                    candidate
                    for candidate in (
                        os.path.join(executable_dir, "NotmyFaultDashboard.exe"),
                        os.path.join(executable_dir, "dashboard.exe"),
                    )
                    if os.path.isfile(candidate)
                    and os.path.normcase(os.path.abspath(candidate)) != current
                ),
                None,
            )
            command = [sibling] if sibling else [sys.executable, "--dashboard"]
            subprocess.Popen(
                command,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            print("[Alert] 已拉起 Dashboard (exe mode)")
        except Exception as e:
            print(f"[Alert] 拉起 Dashboard 失败: {e}", file=sys.stderr)
        return
    dashboard_pyw = _dashboard_pyw_path()
    if not os.path.exists(dashboard_pyw):
        print(f"[Alert] 找不到 dashboard 入口: {dashboard_pyw}", file=sys.stderr)
        return
    try:
        os.startfile(dashboard_pyw)
        print("[Alert] 已拉起 Dashboard")
    except Exception as e:
        print(f"[Alert] 拉起 Dashboard 失败: {e}", file=sys.stderr)


def alert_user(
    title: str,
    message: str,
    open_dashboard: bool = True,
    display_seconds: int = 15,
) -> None:
    """向用户发出告警通知。

    通知带有「打开控制面板」按钮，通过 Windows 协议 (notmyfault://)
    拉起 Dashboard，不依赖进程内 COM 回调。

    Args:
        title: 通知标题
        message: 通知正文
        open_dashboard: 是否同时立即打开 Dashboard（默认 True）
        display_seconds: 通知显示秒数（到达后自动消失）
    """
    try:
        from Win_toaster.show_notification import toaster

        toast = Toast([f"[!] {title}", message])

        # 协议按钮 — launch 字段 = 协议 URL，库自动设 activationType="protocol"
        btn = ToastButton("打开控制面板")
        btn.launch = "notmyfault://dashboard"
        toast.AddAction(btn)

        # 点击通知本体也尝试拉起（作为备用，不依赖 COM）
        def _on_activated(args):
            _launch_dashboard()

        toast.on_activated = _on_activated

        # 持有引用防止 GC
        with _toasts_lock:
            _active_toasts.append(toast)

        toaster.show_toast(toast)

        # 保活线程 — 确保进程在通知显示期间存活
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

    except Exception as e:
        print(f"[Alert] 通知发送失败: {e}", file=sys.stderr)

    # 立即拉起 Dashboard
    if open_dashboard:
        threading.Thread(target=_launch_dashboard, daemon=True).start()
