import os
import threading
from typing import Any, Callable, Dict, Optional

from .config import get_config
from .engine import AutomationEngine


def create_engine(
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> AutomationEngine:
    """创建并加载插件，但不启动（返回 engine 供调用方持有引用）。"""
    config = get_config()
    engine = AutomationEngine(config, on_event=on_event)
    engine.auto_load(os.path.dirname(__file__))
    return engine


def run(
    shutdown_event: "threading.Event | None" = None,
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> None:
    """快捷入口：创建 + 加载 + 启动。"""
    engine = create_engine(on_event=on_event)
    engine.start(shutdown_event=shutdown_event)
