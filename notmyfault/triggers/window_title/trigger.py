"""窗口标题状态检测：目标窗口出现 / 关闭时触发
Windows 用 EnumWindows；Linux 用 xdotool（仅 X11，Wayland 不可用）
"""

import os
import shutil
import subprocess
import sys

from notmyfault.plugin_api import platform_backend_api
from notmyfault.triggers.base import PollingTrigger

BackendMissingError = platform_backend_api().BackendMissingError


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
    xdotool = shutil.which("xdotool")
    if not xdotool:
        return {}
    try:
        result = subprocess.run(
            [xdotool, "search", "--name", ""],
            capture_output=True, text=True, errors="replace", timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    titles = {}
    for line in result.stdout.splitlines():
        wid = line.strip()
        if not wid.isdigit():
            continue
        try:
            title_result = subprocess.run(
                [xdotool, "getwindowname", wid],
                capture_output=True, text=True, errors="replace", timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        title = title_result.stdout.strip()
        if title_result.returncode == 0 and title:
            titles[wid] = title
    return titles


class WindowTitleTrigger(PollingTrigger):
    interval = 3.0
    native = True

    def validate(self):
        if not str(self.config.get("title_pattern", "")).strip():
            raise ValueError("未配置标题关键词（title_pattern 为空）")
        state = self.config.get("state", "opened")
        if state not in ("opened", "closed"):
            raise ValueError(f"无效的窗口状态: {state!r}（可选: opened/closed）")
        if sys.platform != "win32" and not shutil.which("xdotool"):
            raise BackendMissingError("依赖缺失：窗口标题监视需要 xdotool")

    def setup(self):
        self.pattern = str(self.config.get("title_pattern", "")).strip().lower()
        self.target_state = self.config.get("state", "opened")
        self._was_matched = False
        self.log(f"开始监视窗口标题: {self.pattern}")

    def poll(self):
        titles = _get_window_titles()
        matched_titles = [t for t in titles.values() if self.pattern in t.lower()]
        matched = bool(matched_titles)

        if matched and not self._was_matched:
            if self.target_state == "opened":
                actual_title = max(matched_titles, key=len)
                self.log(f"窗口出现: '{self.pattern}'")
                self.emit({
                    "title_pattern": self.pattern,
                    "state": "opened",
                    "matched_title": actual_title,
                })
            self._was_matched = True
        elif not matched and self._was_matched:
            if self.target_state == "closed":
                self.log(f"窗口关闭: '{self.pattern}'")
                self.emit({
                    "title_pattern": self.pattern,
                    "state": "closed",
                    "matched_title": "",
                })
            self._was_matched = False


def run(meta, config, emit_event, shutdown_event):
    WindowTitleTrigger(meta, config, emit_event, shutdown_event).run()
