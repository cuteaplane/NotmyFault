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


def show_message_dialog(title: str, message: str) -> bool:
    """显示仅含确定按钮的消息框；无可用桌面对话框时返回 False"""
    title = str(title or "NotmyFault")
    message = str(message or "")
    if sys.platform == "win32":
        try:
            import ctypes

            MB_OK = 0x00000000
            MB_ICONINFORMATION = 0x00000040
            MB_SETFOREGROUND = 0x00010000
            MB_TOPMOST = 0x00040000
            ctypes.windll.user32.MessageBoxW(
                None,
                message,
                title,
                MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST,
            )
            return True
        except Exception as error:
            print(f"[Dialog] Windows 消息框失败: {error}", file=sys.stderr)
            return False

    for command in _linux_message_commands(title, message):
        if _run_dialog_command(command):
            return True
    print(f"[Dialog] 无可用消息框后端: {title}: {message}", file=sys.stderr)
    return False


def ask_text_dialog(title: str, message: str, default: str = "") -> dict:
    """显示文本输入框。

    返回字典：
    - available: 是否找到对话框后端
    - confirmed: 用户是否确认
    - text: 确认时的输入文本
    """
    title = str(title or "NotmyFault")
    message = str(message or "")
    default = str(default or "")
    if sys.platform == "win32":
        try:
            # VisualBasic InputBox：取消与空输入都返回空串，空串按取消处理
            script = (
                "Add-Type -AssemblyName Microsoft.VisualBasic; "
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "$r = [Microsoft.VisualBasic.Interaction]::InputBox("
                f"{_ps_quote(message)}, {_ps_quote(title)}, {_ps_quote(default)}); "
                "[Console]::Out.Write($r)"
            )
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", script],
                capture_output=True,
                timeout=600,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", "replace")
                print(f"[Dialog] Windows 输入框失败: {detail}", file=sys.stderr)
                return {"available": False, "confirmed": False, "text": ""}
            text = completed.stdout.decode("utf-8", "replace")
            if text == "":
                return {"available": True, "confirmed": False, "text": ""}
            return {"available": True, "confirmed": True, "text": text}
        except Exception as error:
            print(f"[Dialog] Windows 输入框失败: {error}", file=sys.stderr)
            return {"available": False, "confirmed": False, "text": ""}

    saw_backend = False
    for command in _linux_input_commands(title, message, default):
        code, stdout = _run_dialog_command_capture(command)
        if code is None:
            continue
        saw_backend = True
        if code == 0:
            return {
                "available": True,
                "confirmed": True,
                "text": stdout.rstrip("\n"),
            }
        return {"available": True, "confirmed": False, "text": ""}
    if not saw_backend:
        print(f"[Dialog] 无可用输入框后端: {title}", file=sys.stderr)
    return {"available": False, "confirmed": False, "text": ""}


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _linux_message_commands(title: str, message: str) -> list[list[str]]:
    commands: list[list[str]] = []
    zenity = shutil.which("zenity")
    if zenity:
        commands.append(
            [zenity, "--info", f"--title={title}", f"--text={message}", "--width=360"]
        )
    kdialog = shutil.which("kdialog")
    if kdialog:
        commands.append([kdialog, "--title", title, "--msgbox", message])
    return commands


def _linux_input_commands(title: str, message: str, default: str) -> list[list[str]]:
    commands: list[list[str]] = []
    zenity = shutil.which("zenity")
    if zenity:
        args = [
            zenity,
            "--entry",
            f"--title={title}",
            f"--text={message}",
            "--width=360",
        ]
        if default:
            args.append(f"--entry-text={default}")
        commands.append(args)
    kdialog = shutil.which("kdialog")
    if kdialog:
        commands.append([kdialog, "--title", title, "--inputbox", message, default])
    return commands


def _run_dialog_command(command: list[str]) -> bool:
    try:
        completed = subprocess.run(command, timeout=600, check=False)
        return completed.returncode == 0
    except OSError as error:
        print(f"[Dialog] 启动对话框失败: {error}", file=sys.stderr)
        return False


def _run_dialog_command_capture(command: list[str]) -> tuple[int | None, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=600,
            check=False,
        )
        stdout = completed.stdout.decode("utf-8", "replace")
        return completed.returncode, stdout
    except OSError as error:
        print(f"[Dialog] 启动对话框失败: {error}", file=sys.stderr)
        return None, ""
