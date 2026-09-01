"""显示管理员执行确认通知。"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta


_pending_decisions: set[threading.Event] = set()
_pending_lock = threading.Lock()


def cancel_pending_admin_requests() -> None:
    """取消当前仍在等待点击的管理员通知。"""
    with _pending_lock:
        pending = tuple(_pending_decisions)
    for decision in pending:
        decision.set()


def confirm_admin_request(
    plugin_id: str,
    executable: str,
    timeout: float = 120.0,
) -> bool:
    """等待用户点击通知中的允许按钮，超时或关闭时返回 False。"""
    if os.name != "nt":
        return True

    try:
        from windows_toasts import (
            Toast,
            ToastButton,
            ToastDismissalReason,
            ToastDuration,
        )
        from Win_toaster.show_notification import toaster
    except Exception as error:
        raise RuntimeError(f"无法创建管理员确认通知: {error}") from error

    decision = threading.Event()
    approved = False
    toast = Toast(
        [
            "NotmyFault 请求管理员权限",
            f"插件“{plugin_id}”准备以管理员权限运行 {executable}。"
            "点击允许后将显示 UAC。",
        ],
        duration=ToastDuration.Long,
        expiration_time=datetime.now() + timedelta(seconds=max(float(timeout), 0.0)),
    )
    toast.AddAction(ToastButton("允许并继续", arguments="approve"))
    toast.AddAction(ToastButton("拒绝", arguments="deny"))

    def on_activated(args) -> None:
        nonlocal approved
        approved = getattr(args, "arguments", None) == "approve"
        decision.set()

    def on_dismissed(args) -> None:
        if getattr(args, "reason", None) == ToastDismissalReason.USER_CANCELED:
            decision.set()

    def on_failed(_args) -> None:
        decision.set()

    toast.on_activated = on_activated
    toast.on_dismissed = on_dismissed
    toast.on_failed = on_failed
    with _pending_lock:
        _pending_decisions.add(decision)
    try:
        toaster.show_toast(toast)
        decision.wait(timeout=max(float(timeout), 0.0))
        return approved
    finally:
        with _pending_lock:
            _pending_decisions.discard(decision)
        try:
            toaster.remove_toast(toast)
        except Exception:
            pass
