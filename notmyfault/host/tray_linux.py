"""Linux 系统托盘实现（可选 pystray 后端）。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

import pystray
from PIL import Image

from notmyfault.platform.platform_support import (
    linux_autostart_path,
    set_linux_autostart,
    show_notification,
)
from notmyfault.platform.linux_support import session_type

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICON_PATH = PROJECT_ROOT / "logo.png"
AUTO_START_FILE = linux_autostart_path()


def is_tray_supported() -> bool:
    """pystray 的 Linux 后端依赖 XEmbed，Wayland 会话不具备该协议。"""
    return session_type() == "x11"


class TrayIcon:
    """通过 pystray 提供 Linux 系统托盘。"""

    def __init__(
        self,
        on_open_dashboard: Optional[Callable] = None,
        on_toggle_engine: Optional[Callable] = None,
        on_exit: Optional[Callable] = None,
    ):
        self._on_open_dashboard = on_open_dashboard
        self._on_toggle_engine = on_toggle_engine
        self._on_exit = on_exit
        self._engine_state = "stopped"
        self._icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._icon = pystray.Icon(
            "notmyfault",
            Image.open(ICON_PATH),
            "NotmyFault",
            menu=pystray.Menu(
                pystray.MenuItem("打开控制面板", self._open_dashboard, default=True),
                pystray.MenuItem(self._toggle_label, self._toggle_engine),
                pystray.MenuItem(
                    "开机自启",
                    self._toggle_auto_start,
                    checked=lambda _item: AUTO_START_FILE.exists(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._exit),
            ),
        )
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._icon:
            self._icon.stop()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def set_engine_running(self, running: bool) -> None:
        self.set_engine_state("running" if running else "stopped")

    def set_engine_state(self, state: str) -> None:
        self._engine_state = state
        if self._icon:
            self._icon.title = {
                "starting": "NotmyFault - Starting",
                "running": "NotmyFault - Running",
                "stopping": "NotmyFault - Stopping",
                "stopped": "NotmyFault - Paused",
            }.get(state, "NotmyFault")
            self._icon.update_menu()

    def show_balloon(self, title: str, message: str, icon_type: int = 1) -> None:
        del icon_type
        show_notification(title, message)

    def _toggle_label(self, _item) -> str:
        return {
            "starting": "正在启动自动化…",
            "running": "暂停自动化",
            "stopping": "正在暂停自动化…",
            "stopped": "启动自动化",
        }.get(self._engine_state, "启动自动化")

    def _open_dashboard(self, _icon=None, _item=None) -> None:
        if self._on_open_dashboard:
            self._on_open_dashboard()

    def _toggle_engine(self, _icon=None, _item=None) -> None:
        if self._engine_state not in ("starting", "stopping") and self._on_toggle_engine:
            self._on_toggle_engine()

    def _toggle_auto_start(self, _icon=None, _item=None) -> None:
        set_linux_autostart(not AUTO_START_FILE.exists(), str(PROJECT_ROOT))
        if self._icon:
            self._icon.update_menu()

    def _exit(self, _icon=None, _item=None) -> None:
        if self._on_exit:
            self._on_exit()
