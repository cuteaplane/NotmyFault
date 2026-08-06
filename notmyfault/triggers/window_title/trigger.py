import ctypes

from notmyfault.triggers.base import PollingTrigger


def _get_window_titles() -> dict:
    """枚举所有可见窗口，返回 {hwnd: title} 字典"""
    # argtypes 声明在 notmyfault.native，多线程并发调用 ctypes 时持锁
    from notmyfault.native import NATIVE_LOCK, typed_user32
    user32 = typed_user32()
    with NATIVE_LOCK:
        return _get_window_titles_locked(user32)


def _get_window_titles_locked(user32) -> dict:
    from notmyfault.native import WNDENUMPROC
    titles = {}

    def enum_callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if title.strip():
                    titles[hwnd] = title
        return True  # 继续枚举

    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    return titles


class WindowTitleTrigger(PollingTrigger):
    """窗口标题状态检测：目标窗口出现 / 关闭时触发"""

    interval = 3.0
    native = True

    def validate(self):
        if not str(self.config.get("title_pattern", "")).strip():
            raise ValueError("未配置标题关键词（title_pattern 为空）")
        state = self.config.get("state", "opened")
        if state not in ("opened", "closed"):
            raise ValueError(
                f"无效的窗口状态: {state!r}（可选: opened/closed）"
            )

    def setup(self):
        self.pattern = str(self.config.get("title_pattern", "")).strip().lower()
        self.target_state = self.config.get("state", "opened")
        self._was_matched = False
        self.log(f"开始监视窗口标题: {self.pattern}")

    def poll(self):
        titles = _get_window_titles()
        # 找出实际命中的标题，可能同时命中多个窗口
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
