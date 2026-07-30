import os
import time
import hashlib


def _file_matches(pattern: str, name: str) -> bool:
    if not pattern or pattern == "*":
        return True
    import fnmatch
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


def run(meta, config_list, emit_event, shutdown_event):
    trigger_id = meta.get("id", "folder_monitor")

    if not config_list:
        print(f"[Trigger:{trigger_id}] 没有配置规则，退出")
        return

    watched = {}
    for cfg in config_list:
        folder = cfg.get("folder_path", "").strip()
        if not folder or not os.path.isdir(folder):
            print(f"[Trigger:{trigger_id}] 目录不存在或不可读: {folder}")
            continue
        event_type = cfg.get("event_type", "all")
        pattern = cfg.get("file_pattern", "*")
        watched[folder] = {
            "event_type": event_type,
            "pattern": pattern,
            "snapshot": _dir_snapshot(folder),
        }
        print(f"[Trigger:{trigger_id}] 开始监控: {folder} (事件={event_type}, 过滤={pattern})")

    if not watched:
        print(f"[Trigger:{trigger_id}] 没有有效监控目录，退出")
        return

    while not shutdown_event.is_set():
        for folder, info in watched.items():
            try:
                new_snap = _dir_snapshot(folder)
                old_snap = info["snapshot"]
                event_type = info["event_type"]
                pattern = info["pattern"]

                new_files = set(new_snap.keys()) - set(old_snap.keys())
                deleted_files = set(old_snap.keys()) - set(new_snap.keys())
                modified_files = {
                    k for k in set(new_snap.keys()) & set(old_snap.keys())
                    if new_snap[k] != old_snap[k]
                }

                for fpath in sorted(new_files):
                    if _file_matches(pattern, os.path.basename(fpath)):
                        if event_type in ("created", "all"):
                            print(f"[Trigger:{trigger_id}] 新增文件: {fpath}")
                            emit_event(trigger_id, {
                                "event": "created", "path": fpath,
                                "folder": folder,
                            })

                for fpath in sorted(deleted_files):
                    if _file_matches(pattern, os.path.basename(fpath)):
                        if event_type in ("deleted", "all"):
                            print(f"[Trigger:{trigger_id}] 删除文件: {fpath}")
                            emit_event(trigger_id, {
                                "event": "deleted", "path": fpath,
                                "folder": folder,
                            })

                for fpath in sorted(modified_files):
                    if _file_matches(pattern, os.path.basename(fpath)):
                        if event_type in ("modified", "all"):
                            print(f"[Trigger:{trigger_id}] 修改文件: {fpath}")
                            emit_event(trigger_id, {
                                "event": "modified", "path": fpath,
                                "folder": folder,
                            })

                info["snapshot"] = new_snap
            except Exception as e:
                print(f"[Trigger:{trigger_id}] 扫描 {folder} 出错: {e}")

        shutdown_event.wait(3)
