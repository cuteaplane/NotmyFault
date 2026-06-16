"""
引擎告警模块
------------
当引擎发生需要用户关注的错误时，弹出 Windows 交互式通知。
点击通知或按钮即可打开 Dashboard。

用法:
    from notmyfault.alert import alert_user
    alert_user("触发器崩溃", "process_state 触发器线程异常退出", open_dashboard=True)
"""

import os
import sys
import threading

from windows_toasts import Toast, ToastButton


def _launch_dashboard() -> None:
    """启动 dashboard.pyw（独立进程）。"""
    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )
    dashboard_pyw = os.path.join(project_root, "dashboard.pyw")
    if not os.path.exists(dashboard_pyw):
        print(
            f"[Alert] 找不到 dashboard 入口: {dashboard_pyw}",
            file=sys.stderr,
        )
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

    通知带有一个「打开控制面板」按钮，点击通知本体或按钮均可打开 Dashboard。

    Args:
        title: 通知标题
        message: 通知正文
        open_dashboard: 是否同时立即打开 Dashboard（默认 True）
        display_seconds: 通知显示秒数（到达后自动消失）
    """
    # 1. 交互式 Windows 通知（点击可打开 Dashboard）
    try:
        from Win_toaster.show_notification import toaster

        toast = Toast([f"[!] {title}", message])

        # 添加按钮
        btn = ToastButton("打开控制面板", "open_dashboard")
        toast.AddAction(btn)

        # 点击通知或按钮 → 拉起 Dashboard
        def _on_activated(args):
            _launch_dashboard()

        toast.on_activated = _on_activated

        toaster.show_toast(toast)

        # 定时移除通知（与 show_notification 行为一致）
        def _remove_after_delay():
            import time
            time.sleep(display_seconds)
            try:
                toaster.remove_toast(toast)
            except Exception:
                pass

        threading.Thread(target=_remove_after_delay, daemon=True).start()

    except Exception as e:
        print(f"[Alert] 通知发送失败: {e}", file=sys.stderr)

    # 2. 立即拉起 Dashboard（独立于通知点击）
    if open_dashboard:
        threading.Thread(target=_launch_dashboard, daemon=True).start()
