"""文档归档前的目录变化、文件读取和编辑窗口检查"""
import math
import os
import time
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from notmyfault.plugin_api import action_values, platform


_MAX_FILES = 5000
_MAX_OBSERVATIONS = 64
_observations: Dict[str, Tuple[Tuple[Tuple[str, int, int], ...], float]] = {}
_observations_lock = threading.Lock()

_GENERIC_TITLES = {
    "word", "microsoft word", "excel", "microsoft excel", "powerpoint",
    "microsoft powerpoint", "wps office", "wps", "adobe acrobat", "adobe reader",
}
_EDITING_PROCESS_NAMES = {
    "winword.exe", "excel.exe", "powerpnt.exe", "wps.exe", "et.exe", "wpp.exe",
    "acrobat.exe", "acrord32.exe",
    "libreoffice", "soffice.bin", "onlyoffice-desktopeditors", "wps", "et", "wpp",
}

def _snapshot(folder: Path) -> Tuple[Tuple[str, int, int], ...]:
    files: List[Tuple[str, int, int]] = []

    def unreadable_directory(error):
        raise error

    for root, _dirs, names in os.walk(folder, onerror=unreadable_directory):
        for name in names:
            path = Path(root, name)
            stat = path.stat()
            files.append((str(path), stat.st_size, stat.st_mtime_ns))
            if len(files) > _MAX_FILES:
                raise RuntimeError(f"目录文件数超过 {_MAX_FILES}，请缩小待归档范围")
    return tuple(sorted(files))


def _can_open_exclusively(path: str) -> bool:
    return not platform.desktop.file_in_use(path)


def _visible_editing_windows() -> Iterable[str]:
    """返回疑似正在编辑文档的可见窗口标题，后台进程没有文档窗口时不返回标题"""
    return [f"{item['process_name']}: {item['title']}"
            for item in platform.window.list()
            if item["visible"] and item["process_name"].lower() in _EDITING_PROCESS_NAMES
            and item["title"].strip() and item["title"].strip().lower() not in _GENERIC_TITLES]


def check_precondition(_meta: Dict[str, Any], params: Dict[str, Any], _context: Dict[str, Any]):
    """返回包含 ok、reason 和 retry_after_seconds 的前置检查结果，未知状态返回 ok=False"""
    for name, label in (("check_file_locks", "检查文件占用"), ("check_document_windows", "检查可见编辑窗口")):
        if not isinstance(params.get(name, True), bool):
            return {"ok": False, "reason": f"{label}必须是布尔值", "retry_after_seconds": 300}
    raw_folder = str(params.get("source_folder", ""))
    if not raw_folder:
        return {"ok": False, "reason": "未配置待归档目录", "retry_after_seconds": 300}
    try:
        folder = Path(raw_folder).expanduser()
        if not folder.is_dir():
            return {"ok": False, "reason": f"待归档目录不可用: {folder}", "retry_after_seconds": 300}
        quiet_seconds = float(params.get("quiet_seconds", 120) or 0)
        if not math.isfinite(quiet_seconds):
            raise ValueError("文件静默时长必须是有限数字")
        quiet_seconds = max(quiet_seconds, 0)
        with _observations_lock:
            snapshot = _snapshot(folder)
            key = os.path.normcase(str(folder.resolve()))
            now = time.monotonic()
            previous = _observations.get(key)
            changed = previous is None or previous[0] != snapshot
            if changed:
                _observations[key] = (snapshot, now)
                if len(_observations) > _MAX_OBSERVATIONS:
                    _observations.pop(next(iter(_observations)), None)
    except Exception as exc:
        return {"ok": False, "reason": f"无法确认目录状态: {exc}", "retry_after_seconds": 120}

    if changed:
        return {
            "ok": False,
            "reason": f"目录文件刚发生变化，等待静默 {int(quiet_seconds)} 秒",
            "retry_after_seconds": max(quiet_seconds, 5),
        }
    elapsed = now - previous[1]
    if elapsed < quiet_seconds:
        return {
            "ok": False,
            "reason": f"目录仍在静默观察期（还需 {int(quiet_seconds - elapsed) + 1} 秒）",
            "retry_after_seconds": max(quiet_seconds - elapsed, 5),
        }

    if params.get("check_file_locks", True):
        for path, _size, _mtime in snapshot:
            if not _can_open_exclusively(path):
                return {"ok": False, "reason": f"无法独占读取文件，请检查占用状态和读取权限: {path}", "retry_after_seconds": 60}

    if params.get("check_document_windows", True) and platform.capability("window.enumerate").available:
        try:
            editing = list(_visible_editing_windows())
        except Exception as exc:
            return {"ok": False, "reason": f"无法确认文档编辑状态: {exc}", "retry_after_seconds": 60}
        if editing:
            return {
                "ok": False,
                "reason": "检测到正在编辑的文档窗口: " + "；".join(editing[:3]),
                "retry_after_seconds": 60,
            }
    return {"ok": True}


def run_with_context(meta: Dict[str, Any], params: Dict[str, Any], context):
    params = action_values(params)
    result = check_precondition(meta, params, {})
    if not result.get("ok"):
        raise RuntimeError(result.get("reason") or "目录尚未静默")
    return {"ok": True}
