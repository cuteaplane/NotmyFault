"""临时捕获全局热键，退出时取消录制。"""

import math
import threading
import time

from notmyfault.plugin_api import PlatformServiceError, platform


def capture_hotkey(context, payload):
    options = payload if isinstance(payload, dict) else {}
    try:
        timeout = float(options.get("timeout_seconds", 15))
    except (TypeError, ValueError, OverflowError):
        timeout = 15.0
    if not math.isfinite(timeout):
        timeout = 15.0
    timeout = min(max(timeout, 3.0), 60.0)
    cancelled = threading.Event()
    context.register_cleanup(cancelled.set)
    context.session.set_status("请按下要录制的快捷键，按 Esc 取消…")
    deadline = time.monotonic() + timeout
    while not cancelled.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            keys = platform.hotkey.capture(min(0.1, remaining))
        except PlatformServiceError as error:
            return context.error(error.reason, close=True)
        if cancelled.is_set():
            return context.result({"cancelled": True}, close=True)
        if keys:
            return context.commit_value("+".join(keys))
    if cancelled.is_set():
        return context.result({"cancelled": True}, close=True)
    return context.error("没有等到按键，请再试一次", close=True)
