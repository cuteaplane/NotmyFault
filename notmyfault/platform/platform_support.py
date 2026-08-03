"""提供配置目录、独立脚本、桌面通知和 Linux 自启的跨平台函数。"""

from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import sys
from pathlib import Path


def get_config_dir() -> str:
    """返回当前平台的用户配置目录"""
    if sys.platform == "win32":
        base_dir = os.environ.get("APPDATA")
        if base_dir:
            return os.path.join(base_dir, "NotmyFault")
        return str(Path.home() / "AppData" / "Roaming" / "NotmyFault")

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return posixpath.join(xdg_config_home, "notmyfault")
    return posixpath.join(str(Path.home()).replace("\\", "/"), ".config", "notmyfault")


def launch_python_entry(entry_path: str) -> None:
    """使用当前 Python 启动独立入口脚本"""
    popen_options: dict[str, object] = {}
    if sys.platform == "win32":
        popen_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        popen_options["start_new_session"] = True
    subprocess.Popen([sys.executable, entry_path], **popen_options)


def show_notification(title: str, message: str) -> bool:
    """发送桌面通知；后端不可用时返回 False"""
    if sys.platform == "win32":
        try:
            from Win_toaster.show_notification import show_notification as show_windows_notification

            show_windows_notification(title, message)
            return True
        except Exception as error:
            print(f"[Notification] Windows 通知发送失败: {error}", file=sys.stderr)
            return False

    notify_send = shutil.which("notify-send")
    if not notify_send:
        print(f"[Notification] {title}: {message}")
        return False
    try:
        subprocess.Popen(
            [notify_send, "--app-name=NotmyFault", title, message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError as error:
        print(f"[Notification] Linux 通知发送失败: {error}", file=sys.stderr)
        return False


def linux_autostart_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base_dir = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base_dir / "autostart" / "notmyfault.desktop"


def set_linux_autostart(enabled: bool, project_root: str) -> bool:
    """启用或关闭 XDG 开机自启"""
    if sys.platform == "win32":
        raise RuntimeError("Linux autostart API cannot be used on Windows")
    desktop_file = linux_autostart_path()
    if not enabled:
        desktop_file.unlink(missing_ok=True)
        return True

    def quote_exec(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    entry_path = posixpath.join(project_root.replace("\\", "/"), "NOTMYFAULT.pyw")
    desktop_file.parent.mkdir(parents=True, exist_ok=True)
    desktop_file.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=NotmyFault Engine\n"
        f"Exec={quote_exec(sys.executable)} {quote_exec(entry_path)}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-KDE-autostart-after=panel\n",
        encoding="utf-8",
    )
    return True
