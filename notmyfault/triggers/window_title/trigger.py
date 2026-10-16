"""窗口标题状态检测：目标窗口出现 / 关闭时触发
Windows 用 EnumWindows；Linux 用 WindowBackend 枚举（仅 X11）
"""

import os
import shutil

from notmyfault.plugin_api import platform_backend_api
from notmyfault.triggers.base import PollingTrigger

_platform_backend = platform_backend_api()
BackendError = _platform_backend.BackendError
BackendMissingError = _platform_backend.BackendMissingError
WindowBackend = _platform_backend.WindowBackend
default_runner = _platform_backend.default_runner


def _get_window_titles() -> dict:
    if os.name == "nt":
        return _get_window_titles_windows()
    return _get_window_titles_linux()


def _get_window_titles_windows() -> dict:
    import ctypes
    from notmyfault.native import NATIVE_LOCK, typed_user32, WNDENUMPROC
    user32 = typed_user32()
    titles = {}
    with NATIVE_LOCK:
        def enum_callback(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                    if title.strip():
                        titles[hwnd] = title
            return True
        user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    return titles


def _get_window_titles_linux() -> dict:
    try:
        return WindowBackend(default_runner).list_titles()
    except BackendError as error:
        raise RuntimeError(str(error)) from error


class WindowTitleTrigger(PollingTrigger):
    interval = 3.0
    native = False

    def validate(self):
        if not str(self.config.get("title_pattern", "")).strip():
            raise ValueError("未配置标题关键词（title_pattern 为空）")
        state = self.config.get("state", "opened")
        if state not in ("opened", "closed"):
            raise ValueError(f"无效的窗口状态: {state!r}（可选: opened/closed）")
        if os.name != "nt":
            if not shutil.which("wmctrl") and not shutil.which("xdotool"):
                raise BackendMissingError("依赖缺失：窗口标题监视需要 wmctrl 或 xdotool")

    def setup(self):
        self.pattern = str(self.config.get("title_pattern", "")).strip()
        self.target_state = self.config.get("state", "opened")
        self.log(f"开始监视窗口标题: {self.pattern}")

    def poll(self):
        titles = _get_window_titles()
        matched_titles = [t for t in titles.values() if self.pattern.lower() in t.lower()]
        matched = bool(matched_titles)

        self.emit({"title_pattern": self.pattern, "state": "opened" if matched else "closed", "matched_title": max(matched_titles, key=len) if matched else ""})


def run(meta, config, emit_event, shutdown_event):
    WindowTitleTrigger(meta, config, emit_event, shutdown_event).run()
