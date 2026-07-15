import os
import sys
import threading
from typing import Any, Callable, Dict, Optional

from .config import get_config
from .engine import AutomationEngine
import glob
import subprocess as _sp


def _ensure_first_run_build() -> None:
    if getattr(sys, "frozen", False):
        return
    src_dir = os.path.dirname(__file__)
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
    commands = [
        ([sys.executable, build_py, "init-keys", "--builtin"], "init-keys"),
        ([sys.executable, build_py, "sign"], "sign"),
        ([sys.executable, build_py, "build"], "build"),
    ]
    for cmd, name in commands:
        try:
            result = _sp.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(build_py), timeout=60)
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
        paths.append((os.path.dirname(__file__), "builtin"))
    user_dir = os.path.join(os.environ.get("APPDATA", ""), "NotmyFault", "plugins")
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
    engine = create_engine(on_event=on_event)
    engine.start(shutdown_event=shutdown_event)
