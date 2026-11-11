"""窗口标题状态检测：目标窗口出现或关闭时触发"""

from notmyfault.plugin_api import platform
from notmyfault.triggers.base import PollingTrigger


class WindowTitleTrigger(PollingTrigger):
    interval = 3.0
    native = False

    def validate(self):
        if not str(self.config.get("title_pattern", "")).strip():
            raise ValueError("未配置标题关键词（title_pattern 为空）")
        state = self.config.get("state", "opened")
        if state not in ("opened", "closed"):
            raise ValueError(f"无效的窗口状态: {state!r}（可选: opened/closed）")

    def setup(self):
        self.pattern = str(self.config.get("title_pattern", "")).strip()
        self.target_state = self.config.get("state", "opened")
        self.log(f"开始监视窗口标题: {self.pattern}")

    def poll(self):
        titles = platform.window.titles()
        matched_titles = [t for t in titles.values() if self.pattern.lower() in t.lower()]
        matched = bool(matched_titles)

        self.emit({"title_pattern": self.pattern, "state": "opened" if matched else "closed", "matched_title": max(matched_titles, key=len) if matched else ""})


def run(meta, config, emit_event, shutdown_event):
    WindowTitleTrigger(meta, config, emit_event, shutdown_event).run()
