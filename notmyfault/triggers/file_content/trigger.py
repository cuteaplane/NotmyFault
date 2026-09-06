import math
import os
import stat
from pathlib import Path

from notmyfault.triggers.base import PollingTrigger


class FileContentTrigger(PollingTrigger):
    def validate(self):
        path = self.config.get("path", "")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise ValueError("文本文件路径不能为空，且不能包含空字符")
        self.path = Path(os.path.abspath(os.path.expanduser(path)))
        self.text = self.config.get("text", "")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("待查找文本不能为空")
        self.case_sensitive = self.config.get("case_sensitive", True)
        if not isinstance(self.case_sensitive, bool):
            raise ValueError("区分大小写必须为布尔值")
        self.encoding = self.config.get("encoding", "utf-8-sig")
        try:
            b"".decode(self.encoding)
        except (LookupError, TypeError):
            raise ValueError("未知或不适用于文本的编码") from None
        self.target_state = self.config.get("state", "contains")
        if self.target_state not in ("contains", "absent", "changed"):
            raise ValueError("目标状态必须为 contains、absent 或 changed")
        try:
            self.interval = float(self.config.get("interval", 1))
            max_kib = float(self.config.get("max_kib", 1024))
        except (TypeError, ValueError):
            raise ValueError("检查间隔和文件大小上限必须是数字") from None
        if not math.isfinite(self.interval) or not 0.2 <= self.interval <= 3600:
            raise ValueError("检查间隔必须在 0.2 到 3600 秒之间")
        if not math.isfinite(max_kib) or not 1 <= max_kib <= 16384:
            raise ValueError("文件大小上限必须在 1 到 16384 KiB 之间")
        self.max_bytes = int(max_kib * 1024)

    def setup(self):
        self._previous = None
        self._read_error = False

    def poll(self):
        try:
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("监控路径不是普通文件")
            if info.st_size > self.max_bytes:
                raise ValueError("文件超过大小上限")
            with self.path.open("rb") as source:
                raw = source.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise ValueError("文件超过大小上限")
            content = raw.decode(self.encoding)
        except (OSError, UnicodeError, ValueError) as error:
            if not self._read_error:
                self.log(f"无法读取文本内容，本轮跳过：{type(error).__name__}")
            self._read_error = True
            return
        self._read_error = False
        contains = (
            self.text in content
            if self.case_sensitive
            else self.text.casefold() in content.casefold()
        )
        previous = self._previous
        self._previous = contains
        state = "contains" if contains else "absent"
        if (
            previous is not None
            and contains != previous
            and self.target_state in (state, "changed")
            and not self._stop_event.is_set()
        ):
            self.emit({"path": str(self.path), "state": state, "contains": contains})


def run(meta, config, emit_event, shutdown_event):
    FileContentTrigger(meta, config, emit_event, shutdown_event).run()
