import os
import time
import fnmatch
import re


def _file_matches(pattern: str, name: str) -> bool:
    if not pattern or pattern == "*":
        return True
    if os.name == "nt":
        # Windows 文件系统大小写不敏感，fnmatch 默认大小写敏感会漏匹配。
        regex = fnmatch.translate(pattern)
        return re.match(regex, name, re.IGNORECASE) is not None
    return fnmatch.fnmatch(name, pattern)


def _dir_snapshot(folder: str):
    snap = {}
    if not os.path.isdir(folder):
        return snap
    for root, dirs, files in os.walk(folder):
        for f in files:
            fpath = os.path.join(root, f)
            try:
                stat = os.stat(fpath)
                snap[fpath] = (stat.st_size, stat.st_mtime)
            except OSError:
                continue
    return snap


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "folder_monitor")
    folder = config.get("folder_path", "").strip()
    if not folder or not os.path.isdir(folder):
        print(f"[Trigger:{trigger_id}] 目录不存在或不可读: {folder}")
        return
    event_type = config.get("event_type", "all")
    pattern = config.get("file_pattern", "*")
    snapshot = _dir_snapshot(folder)
    print(f"[Trigger:{trigger_id}] 开始监控: {folder} (事件={event_type}, 过滤={pattern})")

    while not shutdown_event.is_set():
        try:
            new_snapshot = _dir_snapshot(folder)
            new_files = set(new_snapshot) - set(snapshot)
            deleted_files = set(snapshot) - set(new_snapshot)
            modified_files = {
                path for path in set(new_snapshot) & set(snapshot)
                if new_snapshot[path] != snapshot[path]
            }

            for changed_path, change in (
                *((path, "created") for path in sorted(new_files)),
                *((path, "deleted") for path in sorted(deleted_files)),
                *((path, "modified") for path in sorted(modified_files)),
            ):
                if event_type in (change, "all") and _file_matches(pattern, os.path.basename(changed_path)):
                    print(f"[Trigger:{trigger_id}] {change}: {changed_path}")
                    emit_event({"event": change, "path": changed_path, "folder": folder})
            snapshot = new_snapshot
        except Exception as e:
            print(f"[Trigger:{trigger_id}] 扫描 {folder} 出错: {e}")

        shutdown_event.wait(3)
