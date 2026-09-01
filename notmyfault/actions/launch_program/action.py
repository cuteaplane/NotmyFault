import shlex
import subprocess
import sys


def _split_args(raw: str) -> list:
    """将命令行参数字符串拆分为列表，支持双引号包裹"""
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
    except FileNotFoundError as error:
        raise RuntimeError("程序不存在或不可执行") from error
    except OSError as e:
        raise RuntimeError("启动失败") from e
