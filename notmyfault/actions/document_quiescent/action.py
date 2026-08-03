"""工作流前置条件：确认目录在指定时间内未变化，文件未被占用且没有文档编辑窗口"""
import ctypes
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


_MAX_FILES = 5000
_observations: Dict[str, Tuple[Tuple[Tuple[str, int, int], ...], float]] = {}

_GENERIC_TITLES = {
    "word", "microsoft word", "excel", "microsoft excel", "powerpoint",
    "microsoft powerpoint", "wps office", "wps", "adobe acrobat", "adobe reader",
}
_EDITING_PROCESS_NAMES = {
    "winword.exe", "excel.exe", "powerpnt.exe", "wps.exe", "et.exe", "wpp.exe",
    "acrobat.exe", "acrord32.exe",
    "libreoffice", "soffice.bin", "onlyoffice-desktopeditors", "wps", "et", "wpp",
}
_DOCUMENT_EXTENSIONS = {
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
    ".pdf",
}


def _snapshot(folder: Path) -> Tuple[Tuple[str, int, int], ...]:
    files: List[Tuple[str, int, int]] = []
    for root, _dirs, names in os.walk(folder):
        for name in names:
            path = Path(root, name)
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((str(path), stat.st_size, stat.st_mtime_ns))
            if len(files) > _MAX_FILES:
                raise RuntimeError(f"目录文件数超过 {_MAX_FILES}，请缩小待归档范围")
    return tuple(sorted(files))


def _can_open_exclusively(path: str) -> bool:
    """Windows 下以零共享模式打开文件，失败时返回 False"""
    if os.name != "nt":
        try:
            with open(path, "rb"):
                return True
        except OSError:
            return False

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        path,
        0x80000000,  # GENERIC_READ
        0,           # 共享参数为 0，其他进程持有句柄时 CreateFileW 会失败
        None,
        3,           # OPEN_EXISTING
        0x80,        # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        return False
    try:
        return True
    finally:
        kernel32.CloseHandle(handle)


def _visible_editing_windows() -> Iterable[str]:
    """返回疑似正在编辑文档的可见窗口标题，后台进程没有文档窗口时不返回标题"""
    if os.name != "nt":
        try:
            import psutil
        except ImportError as exc:
            raise RuntimeError("缺少 psutil，无法检查文档进程") from exc
        editing: List[str] = []
        for process in psutil.process_iter(["pid", "name", "open_files"]):
            try:
                process_name = (process.info["name"] or "").lower()
                if process_name not in _EDITING_PROCESS_NAMES:
                    continue
                document_paths = [
                    item.path
                    for item in (process.info["open_files"] or [])
                    if Path(item.path).suffix.lower() in _DOCUMENT_EXTENSIONS
                ]
                if document_paths:
                    editing.append(
                        f"{process_name}: {Path(document_paths[0]).name}"
                    )
            except (psutil.Error, OSError):
                continue
        return editing
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError("缺少 psutil，无法检查文档窗口") from exc

    user32 = ctypes.windll.user32
    titles: List[str] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        pid = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            process_name = psutil.Process(pid.value).name().lower()
        except (psutil.Error, OSError):
            return True
        title = buffer.value.strip()
        if process_name in _EDITING_PROCESS_NAMES and title and title.lower() not in _GENERIC_TITLES:
            titles.append(f"{process_name}: {title}")
        return True

    if not user32.EnumWindows(callback_type(visit), None):
        raise RuntimeError("EnumWindows 失败")
    return titles


def check_precondition(_meta: Dict[str, Any], params: Dict[str, Any], _context: Dict[str, Any]):
    """返回包含 ok、reason 和 retry_after_seconds 的前置检查结果，未知状态返回 ok=False"""
    raw_folder = str(params.get("source_folder", "")).strip()
    if not raw_folder:
        return {"ok": False, "reason": "未配置待归档目录", "retry_after_seconds": 300}
    folder = Path(raw_folder).expanduser()
    if not folder.is_dir():
        return {"ok": False, "reason": f"待归档目录不可用: {folder}", "retry_after_seconds": 300}
    try:
        quiet_seconds = max(float(params.get("quiet_seconds", 120) or 0), 0)
        snapshot = _snapshot(folder)
    except Exception as exc:
        return {"ok": False, "reason": f"无法确认目录状态: {exc}", "retry_after_seconds": 120}

    key = str(folder.resolve()).lower()
    now = time.monotonic()
    previous = _observations.get(key)
    if previous is None or previous[0] != snapshot:
        _observations[key] = (snapshot, now)
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
                return {"ok": False, "reason": f"文件仍被占用: {path}", "retry_after_seconds": 60}

    if params.get("check_document_windows", True):
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


def run(_meta: Dict[str, Any], _params: Dict[str, Any]):
    """动作插件入口不执行操作，工作流通过 check_precondition 检查目录状态"""
    return None
