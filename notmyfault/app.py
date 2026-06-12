import os
import threading
from typing import Any, Callable, Dict, Optional

from .config import get_config
from .engine import AutomationEngine
# from Win_toaster.show_notification import show_notification
# from Win_toaster.AUMID_Register import register_toaster


def run(shutdown_event: "threading.Event | None" = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> None:
    # register_toaster()
    # show_notification("NotmyFault 已加载", "")
    config = get_config()
    engine = AutomationEngine(config, on_event=on_event)
    engine.auto_load(os.path.dirname(__file__))
    engine.start(shutdown_event=shutdown_event)
