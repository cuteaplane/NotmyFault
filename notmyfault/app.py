import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .config import get_config
from .engine import AutomationEngine


def _get_plugin_paths():
    paths = []
    if getattr(sys, "frozen", False):
        paths.append((os.path.join(sys._MEIPASS, "notmyfault"), "builtin"))
    else:
        paths.append((os.path.dirname(__file__), "builtin"))
    user_dir = os.path.join(os.environ.get("APPDATA", ""), "NotmyFault", "plugins")
    if os.path.isdir(user_dir):
        paths.append((user_dir, "user"))
    return paths


def _ensure_first_run_build() -> None:
    """源码首次运行时自动初始化签名并签署内置插件。"""
    if getattr(sys, "frozen", False):
        return

    root = Path(__file__).resolve().parent.parent
    build_py = root / "build.py"
    if not build_py.exists():
        return

    plugin_jsons = list((root / "notmyfault" / "actions").glob("*/action.json"))
    plugin_jsons += list((root / "notmyfault" / "triggers").glob("*/trigger.json"))
    if not plugin_jsons:
        return

    unsigned = [p for p in plugin_jsons if not (p.parent / "signature.sig").exists()]
    build_json = root / "build.json"
    if not unsigned and build_json.exists():
        return

    commands = []
    if not (root / ".private" / "signing_private_key.pem").exists():
        commands.append([sys.executable, str(build_py), "init-keys", "--builtin"])
    commands.append([sys.executable, str(build_py), "sign"])
    if not build_json.exists():
        commands.append([sys.executable, str(build_py), "build"])

    for cmd in commands:
        try:
            subprocess.run(cmd, cwd=str(root), check=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[App] 首次运行自动构建失败: {' '.join(cmd)}: {exc}")
            os.environ.setdefault("NOTMYFAULT_MODE", "develop")
            return


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
    engine = create_engine(on_event=on_event)
    engine.start(shutdown_event=shutdown_event)
