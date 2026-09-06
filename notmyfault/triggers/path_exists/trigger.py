import math
import os
import stat
from pathlib import Path

from notmyfault.triggers.base import PollingTrigger


class PathExistsTrigger(PollingTrigger):
    def validate(self):
        path = self.config.get("path", "")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise ValueError("监控路径不能为空，且不能包含空字符")
        self.path = Path(os.path.abspath(os.path.expanduser(path)))
        self.kind = self.config.get("kind", "any")
        if self.kind not in ("any", "file", "directory"):
            raise ValueError("路径类型必须为 any、file 或 directory")
        self.target_state = self.config.get("state", "exists")
        if self.target_state not in ("exists", "missing", "changed"):
            raise ValueError("目标状态必须为 exists、missing 或 changed")
        try:
            self.interval = float(self.config.get("interval", 1))
        except (TypeError, ValueError):
            raise ValueError("检查间隔必须是 0.2 到 3600 秒之间的数字") from None
        if not math.isfinite(self.interval) or not 0.2 <= self.interval <= 3600:
            raise ValueError("检查间隔必须是 0.2 到 3600 秒之间的数字")

    def setup(self):
        self._previous = None
        self._read_error = False

    def poll(self):
        try:
            mode = self.path.stat().st_mode
            exists = (
                self.kind == "any"
                or self.kind == "file" and stat.S_ISREG(mode)
                or self.kind == "directory" and stat.S_ISDIR(mode)
            )
        except (FileNotFoundError, NotADirectoryError):
            exists = False
        except OSError as error:
            if not self._read_error:
                self.log(f"无法读取路径状态，本轮跳过：{type(error).__name__}")
            self._read_error = True
            return
        self._read_error = False
        previous = self._previous
        self._previous = exists
        state = "exists" if exists else "missing"
        if (
            previous is not None
            and exists != previous
            and self.target_state in (state, "changed")
            and not self._stop_event.is_set()
        ):
            self.emit({"path": str(self.path), "state": state, "exists": exists})


def run(meta, config, emit_event, shutdown_event):
    PathExistsTrigger(meta, config, emit_event, shutdown_event).run()
