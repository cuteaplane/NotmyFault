import os
import sys
import threading
from typing import Any, Callable, Dict, Optional

from notmyfault.config import get_config
from notmyfault.core.engine import AutomationEngine
from notmyfault.platform.platform_support import get_config_dir
import glob
import subprocess as _sp


def _run_build_command(cmd: list[str], build_dir: str):
    """以 UTF-8 运行 build.py，避免 Windows 控制台代码页污染首次启动日志。"""
    env = os.environ.copy()
    # build.py 会输出中文。capture_output 时没有控制台代码页可借，必须明确
    # 规定子进程和父进程都按 UTF-8 处理，不能再让 subprocess 猜 GBK。
    env["PYTHONUTF8"] = "1"
    return _sp.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=build_dir,
        env=env,
        timeout=60,
    )


# 本模块位于 notmyfault/host/ 下，包根（插件目录/构建文件所在）是其上一级。
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_first_run_build() -> None:
    if getattr(sys, "frozen", False):
        return
    src_dir = _PKG_ROOT
    build_json = os.path.join(src_dir, "..", "build.json")
    build_py = os.path.join(src_dir, "..", "build.py")
    sig_files = glob.glob(os.path.join(src_dir, "actions", "*", "signature.sig"))
    sig_files += glob.glob(os.path.join(src_dir, "triggers", "*", "signature.sig"))
    plugin_dirs = glob.glob(os.path.join(src_dir, "actions", "*"))
    plugin_dirs += glob.glob(os.path.join(src_dir, "triggers", "*"))
    plugin_dirs = [d for d in plugin_dirs if os.path.isdir(d) and not d.endswith("__pycache__")]
    needs_build = not os.path.exists(build_json) or len(sig_files) < len(plugin_dirs)
    if not needs_build:
        return
    print("[FirstRun] Detected missing signatures or build file, running first-time build...")
    # 第一次从 GUI 启动没有可用终端来输入私钥口令。一次完成 permissive 构建：
    # 生成本机开发签名键、签内置插件并写 build.json；正式发布仍由开发者显式
    # 执行 strict 构建，不能把交互式口令提示藏进后台子进程。
    commands = [
        ([sys.executable, build_py, "build", "--security-mode=permissive"], "build"),
    ]
    for cmd, name in commands:
        try:
            result = _run_build_command(cmd, os.path.dirname(build_py))
            if result.returncode != 0:
                print(f"[FirstRun] build {name} failed (code={result.returncode}): {result.stderr.strip()[:500]}")
                _degrade_security_mode()
                return
            print(f"[FirstRun] build {name} OK")
        except Exception as e:
            print(f"[FirstRun] build {name} exception: {e}")
            _degrade_security_mode()
            return
    print("[FirstRun] First-time build complete")


def _degrade_security_mode() -> None:
    env_mode = os.environ.get("NOTMYFAULT_MODE", "")
    if env_mode:
        return
    os.environ["NOTMYFAULT_MODE"] = "develop"
    print("[FirstRun] Degraded to development mode (NOTMYFAULT_MODE=develop)")


def _get_plugin_paths():
    paths = []
    if getattr(sys, "frozen", False):
        paths.append((os.path.join(sys._MEIPASS, "notmyfault"), "builtin"))
    else:
        paths.append((_PKG_ROOT, "builtin"))
    user_dir = os.path.join(get_config_dir(), "plugins")
    if os.path.isdir(user_dir):
        paths.append((user_dir, "user"))
    return paths


def create_engine(
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> AutomationEngine:
    _ensure_first_run_build()
    config = get_config()
    engine = AutomationEngine(config, on_event=on_event)
    engine.auto_load(_get_plugin_paths())
    return engine


def run(
    shutdown_event: "threading.Event | None" = None,
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> None:
    # 与 NOTMYFAULT.pyw 入口一致：拒绝以管理员身份启动，插件提权必须走
    # notmyfault.security.sudo 的 UAC 受控通道，而不是整个引擎带着提升令牌运行。
    from notmyfault.security.security import is_admin_process
    if is_admin_process():
        print(
            "[Engine] [!!] NotmyFault 拒绝以管理员身份启动：请用普通用户权限运行。"
            "插件需要提权时请通过 notmyfault.security.sudo.run_as_admin 弹出 UAC 授权。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    engine = create_engine(on_event=on_event)
    engine.start(shutdown_event=shutdown_event)
