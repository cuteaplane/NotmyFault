import os
import shlex
import subprocess
import sys

from notmyfault.plugin_api import native_lock


def _split_windows_args(raw: str) -> list[str]:
    import ctypes
    from ctypes import wintypes

    with native_lock():
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        shell32.CommandLineToArgvW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_int),
        ]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL

        argc = ctypes.c_int()
        command_line = subprocess.list2cmdline(["notmyfault.exe"]) + " " + raw
        argv = shell32.CommandLineToArgvW(command_line, ctypes.byref(argc))
        if not argv:
            raise ctypes.WinError()
        try:
            return [argv[index] for index in range(1, argc.value)]
        finally:
            kernel32.LocalFree(argv)


def _windows_quotes_balanced(raw: str) -> bool:
    quoted = False
    backslashes = 0
    for char in raw:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"' and backslashes % 2 == 0:
            quoted = not quoted
        backslashes = 0
    return not quoted


def _split_args(raw: str) -> list:
    """将命令行参数字符串拆分为列表，支持双引号包裹"""
    if sys.platform == "win32":
        if not _windows_quotes_balanced(raw):
            raise ValueError("程序参数引号不完整")
        try:
            return _split_windows_args(raw)
        except OSError as error:
            raise RuntimeError("Windows 程序参数解析失败") from error
    try:
        return shlex.split(raw)
    except ValueError as error:
        raise ValueError("程序参数引号不完整") from error


def run(action_info, params):
    path = params.get("path", "").strip()
    if not path:
        raise ValueError("未指定程序路径")

    raw_args = params.get("args", "").strip()
    working_directory = params.get("working_directory", "").strip() or None
    print(f"[Action:launch_program] 启动: {path} (参数 {len(raw_args)} 字符)")

    args = [path] + _split_args(raw_args) if raw_args else [path]

    # Windows 使用 CREATE_NO_WINDOW，启动进程不创建控制台窗口
    if sys.platform == "win32":
        popen_kwargs = {
            "creationflags": subprocess.CREATE_NO_WINDOW,
            "shell": False,
        }
    else:
        popen_kwargs = {}

    try:
        subprocess.Popen(args, cwd=working_directory, **popen_kwargs)
        print(f"[Action:launch_program] 已启动: {path}")
    except OSError as error:
        association_path = (
            os.path.join(working_directory, path)
            if working_directory and not os.path.isabs(path)
            else path
        )
        if (sys.platform == "win32"
                and (getattr(error, "winerror", None) or error.errno) == 193
                and os.path.isfile(association_path)):
            try:
                os.startfile(
                    os.path.abspath(association_path),
                    arguments=raw_args,
                    cwd=working_directory,
                )
                print(
                    f"[Action:launch_program] 已通过文件关联打开: {association_path}"
                )
                return
            except OSError as fallback_error:
                raise RuntimeError("程序不存在或无法打开") from fallback_error
        if isinstance(error, FileNotFoundError):
            raise RuntimeError("程序不存在或不可执行") from error
        raise RuntimeError("启动失败") from error
