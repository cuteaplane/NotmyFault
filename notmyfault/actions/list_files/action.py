import fnmatch
import os
from pathlib import Path


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    directory = str(params.get("directory", "") or "").strip()
    if not directory:
        raise ValueError("未指定目录")
    root = Path(directory).expanduser().absolute()
    if not root.is_dir():
        raise NotADirectoryError(f"目录不存在或不是目录: {root}")
    pattern = str(params.get("pattern", "*") or "*").replace("\\", "/")
    recursive = bool(params.get("recursive", False))
    max_results = int(params.get("max_results", 1000))
    if max_results < 1 or max_results > 100000:
        raise ValueError("结果数量上限必须在 1 到 100000 之间")
    cancellation = context.get("runtime", {}).get("cancellation")
    files = []

    def raise_walk_error(error):
        raise error

    for current, directories, filenames in os.walk(
        root, followlinks=False, onerror=raise_walk_error
    ):
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        directories.sort()
        for name in sorted(filenames):
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if not fnmatch.fnmatch(relative, pattern):
                continue
            if len(files) == max_results:
                return {
                    "directory": str(root),
                    "files": files,
                    "count": len(files),
                    "truncated": True,
                }
            files.append(str(path))
        if not recursive:
            break
    return {
        "directory": str(root),
        "files": files,
        "count": len(files),
        "truncated": False,
    }
