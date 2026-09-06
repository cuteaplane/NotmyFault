import stat
from datetime import datetime, timezone
from pathlib import Path


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    file_path = str(params.get("file_path", "") or "").strip()
    if not file_path:
        raise ValueError("未指定文件或目录路径")
    path = Path(file_path).expanduser().absolute()
    result = {
        "path": str(path),
        "name": path.name,
        "extension": path.suffix,
        "exists": False,
        "is_file": False,
        "is_directory": False,
        "is_symlink": path.is_symlink(),
        "size_bytes": 0,
        "modified_at": "",
        "modified_time": None,
    }
    try:
        info = path.stat()
    except FileNotFoundError:
        return result
    result.update({
        "exists": True,
        "is_file": stat.S_ISREG(info.st_mode),
        "is_directory": stat.S_ISDIR(info.st_mode),
        "size_bytes": info.st_size if stat.S_ISREG(info.st_mode) else 0,
        "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
        "modified_time": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
    })
    return result
