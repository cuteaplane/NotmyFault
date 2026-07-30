import os
import subprocess
import sys


def run(action_info, params):
    path = params.get("path", "").strip()
    if not path:
        print("[Action:launch_program] 未指定程序路径，跳过")
        return

    raw_args = params.get("args", "").strip()
    working_directory = params.get("working_directory", "").strip() or None
    print(f"[Action:launch_program] 启动: {path} {raw_args}")

    try:
        if raw_args:
            # 拆分参数（支持带引号的参数）
            args = [path] + _split_args(raw_args)
        else:
            args = [path]

        # 在 Windows 上使用 CREATE_NO_WINDOW 避免弹出控制台
        if sys.platform == "win32":
            subprocess.Popen(
                args,
                cwd=working_directory,
                creationflags=subprocess.CREATE_NO_WINDOW,
                shell=False
            )
        else:
            subprocess.Popen(args, cwd=working_directory)
        print(f"[Action:launch_program] 已启动: {path}")
    except FileNotFoundError:
        # 回退：用 os.startfile（Windows）；非 Windows 用列表形式（不经 shell）
        # PoC-10 修复：回退路径不再用 shell 模式，避免命令注入
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                subprocess.Popen([path])
            print(f"[Action:launch_program] (回退方式) 已启动: {path}")
        except Exception as e2:
            print(f"[Action:launch_program] 启动失败: {e2}")
    except Exception as e:
        print(f"[Action:launch_program] 启动失败: {e}")


def _split_args(raw: str) -> list:
    """将命令行参数字符串拆分为列表，支持双引号包裹"""
    import shlex
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()
