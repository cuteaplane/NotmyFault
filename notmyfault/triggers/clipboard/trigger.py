from notmyfault.plugin_api import platform
from notmyfault.triggers.base import PollingTrigger


class ClipboardTrigger(PollingTrigger):
    """剪贴板内容监控，match_text 为空时任意内容变化都会触发"""

    interval = 1.0
    native = False

    def setup(self):
        self.match_text = str(self.config.get("match_text", "")).strip()
        self._last_content = platform.clipboard.read()
        self.log("剪贴板监控启动")

    def poll(self):
        current = platform.clipboard.read()
        if current is None or current == self._last_content:
            return
        if not self.match_text:
            self.log("剪贴板内容变化")
            self.emit({"text": current, "match_text": ""})
        else:
            current_lower = current.lower()
            if self.match_text.lower() in current_lower:
                self.log(f"剪贴板匹配: {self.match_text}")
                self.emit({
                    "text": current,
                    "match_text": self.match_text,
                    "matched": self.match_text,
                })
        self._last_content = current


def run(meta, config, emit_event, shutdown_event):
    ClipboardTrigger(meta, config, emit_event, shutdown_event).run()
