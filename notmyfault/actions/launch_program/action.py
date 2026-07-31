import os
import shlex
import subprocess
import sys


def _split_args(raw: str) -> list:
    """将命令行参数字符串拆分为列表，支持双引号包裹"""
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def run(action_info, params):
    path = params.get("path", "").strip()
    if not path:
        raise ValueError("未指定程序路径")

    raw_args = params.get("args", "").strip()
    working_directory = params.get("working_directory", "").strip() or None
    print(f"[Action:launch_program] 启动: {path} {raw_args}")

    args = [path] + _split_args(raw_args) if raw_args else [path]

    # 在 Windows 上使用 CREATE_NO_WINDOW 避免弹出控制台
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
    except FileNotFoundError:
        # 回退：用 os.startfile（Windows，走文件关联）；非 Windows 用列表形式
        # （不经 shell，避免命令注入）。两条路径都失败则抛异常让引擎标记失败。
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                subprocess.Popen([path])
            print(f"[Action:launch_program] (回退方式) 已启动: {path}")
        except Exception as e2:
            raise RuntimeError(f"启动失败: {e2}") from e2
    except OSError as e:
        raise RuntimeError(f"启动失败: {e}") from e
