"""全局热键触发器。"""

from notmyfault.plugin_api import platform
from notmyfault.triggers.base import PollingTrigger


class HotkeyTrigger(PollingTrigger):
    interval = 0.05

    def validate(self):
        self._raw = str(self.config.get("hotkey", "")).strip()
        if not self._raw:
            raise ValueError("未配置热键（hotkey 参数为空）")
        self._keys = self._raw.replace("+", " ").split()

    def setup(self):
        self._listener = platform.hotkey.register(self._keys)
        self.log(f"已注册热键: {self._raw}")

    def poll(self):
        for _ in range(self._listener.poll()):
            self.log(f"热键触发: {self._raw}")
            self.emit({"hotkey": self._raw})

    def teardown(self):
        listener = getattr(self, "_listener", None)
        if listener is not None:
            listener.close()


def run(meta, config, emit_event, shutdown_event):
    HotkeyTrigger(meta, config, emit_event, shutdown_event).run()
