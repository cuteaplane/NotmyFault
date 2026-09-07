import os
import fnmatch
import re

from notmyfault.triggers.base import PollingTrigger


def _file_matches(pattern: str, name: str) -> bool:
    if not pattern or pattern == "*":
        return True
    if os.name == "nt":
        # Windows 文件系统大小写不敏感，fnmatch 默认大小写敏感会漏匹配
        regex = fnmatch.translate(pattern)
        return re.match(regex, name, re.IGNORECASE) is not None
    return fnmatch.fnmatch(name, pattern)


def _dir_snapshot(folder: str):
    snap = {}
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"监控目录不存在或不可读: {folder}")
    def failed(error):
        raise error
    for root, dirs, files in os.walk(folder, onerror=failed):
        for f in files:
            fpath = os.path.join(root, f)
            stat = os.stat(fpath)
            snap[fpath] = (stat.st_size, stat.st_mtime)
    return snap


class FolderMonitorTrigger(PollingTrigger):
    interval = 3.0

    def validate(self):
        self.event_type = self.config.get("event_type", "all")
        if self.event_type not in ("created", "modified", "deleted", "all"):
            raise ValueError(
                f"无效的事件类型: {self.event_type!r}"
                "（可选: created/modified/deleted/all）"
            )
        self.folder = self.config.get("folder_path", "").strip()
        if not self.folder or not os.path.isdir(self.folder):
            raise FileNotFoundError(f"监控目录不存在或不可读: {self.folder}")
        self.pattern = self.config.get("file_pattern", "*")

    def setup(self):
        self._snapshot = _dir_snapshot(self.folder)
        self.log(f"开始监控: {self.folder} (事件={self.event_type}, 过滤={self.pattern})")

    def poll(self):
        new_snapshot = _dir_snapshot(self.folder)
        new_files = set(new_snapshot) - set(self._snapshot)
        deleted_files = set(self._snapshot) - set(new_snapshot)
        modified_files = {
            path for path in set(new_snapshot) & set(self._snapshot)
            if new_snapshot[path] != self._snapshot[path]
        }
        for changed_path, change in (
            *((path, "created") for path in sorted(new_files)),
            *((path, "deleted") for path in sorted(deleted_files)),
            *((path, "modified") for path in sorted(modified_files)),
        ):
            if self._stop_event.is_set():
                return
            if self.event_type in (change, "all") and _file_matches(self.pattern, os.path.basename(changed_path)):
                self.log(f"{change}: {changed_path}")
                self.emit({"event": change, "path": changed_path, "folder": self.folder})
        self._snapshot = new_snapshot


def run(meta, config, emit_event, shutdown_event):
    FolderMonitorTrigger(meta, config, emit_event, shutdown_event).run()
