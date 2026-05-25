import os

from .config import get_config
from .engine import AutomationEngine
from Win_toaster.show_notification import show_notification
from Win_toaster.AUMID_Register import register_toaster


def run() -> None:
    register_toaster()
    show_notification("NotmyFault 已加载", "")
    config = get_config()
    engine = AutomationEngine(config)
    engine.auto_load(os.path.dirname(__file__))
    engine.start()
