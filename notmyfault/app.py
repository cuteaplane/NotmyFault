import os
import sys
import threading
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


def create_engine(
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> AutomationEngine:
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
