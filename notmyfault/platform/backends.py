from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable


class BackendError(RuntimeError):
    kind = "backend_failed"


class BackendUnsupportedError(BackendError):
    kind = "unsupported"


class BackendMissingError(BackendError):
    kind = "backend_missing"


class BackendPermissionDeniedError(BackendError):
    kind = "permission_denied"


class BackendFailedError(BackendError):
    kind = "backend_failed"


class CommandRunner:
    def run(
        self,
        args: list[str],
        timeout: float = 10,
        input: str | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                input=input,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendFailedError(
                f"命令超时（{timeout}s）: {args[0] if args else '?'}"
            ) from exc
        except PermissionError as exc:
            raise BackendPermissionDeniedError(f"系统拒绝执行: {args[0]}") from exc
        except FileNotFoundError as exc:
            raise BackendMissingError(f"未安装 {args[0]}") from exc

    def which(self, *names: str) -> str | None:
        for name in names:
            if path := shutil.which(name):
                return path
        return None


default_runner = CommandRunner()


def _require_linux() -> None:
    if not sys.platform.startswith("linux"):
        raise BackendUnsupportedError("这个 backend 只在 Linux 上可用")


def _failed_detail(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or "").strip() or f"退出码 {result.returncode}"


_MODIFIERS = {"ctrl", "shift", "alt", "super", "win"}
_SPECIAL_KEYS = {
    "enter": "Return", "return": "Return", "tab": "Tab", "space": "space",
    "backspace": "BackSpace", "delete": "Delete", "escape": "Escape", "esc": "Escape",
    "home": "Home", "end": "End", "pgup": "Page_Up", "pgdn": "Page_Down",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "capslock": "Caps_Lock", "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
    "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10",
    "f11": "F11", "f12": "F12",
}


class InputBackend:
    """xdotool / ydotool 键盘输入"""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def _tool(self) -> tuple[str, str]:
        _require_linux()
        path = self._runner.which("xdotool", "ydotool")
        if not path:
            raise BackendMissingError("依赖缺失：模拟按键需要 xdotool 或 ydotool")
        return path, os.path.basename(path)

    def type_text(self, text: str) -> None:
        tool, name = self._tool()
        if name == "xdotool":
            args = [tool, "type", "--clearmodifiers", "--", text]
        else:
            args = [tool, "type", "--", text]
        result = self._runner.run(args, timeout=30)
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))

    def send_hotkey(self, parts: Iterable[str]) -> None:
        tool, name = self._tool()
        mapped = []
        for part in parts:
            if part in _SPECIAL_KEYS:
                mapped.append(_SPECIAL_KEYS[part])
            elif part in _MODIFIERS:
                mapped.append(part.replace("win", "super"))
            else:
                mapped.append(part)
        combo = "+".join(mapped)
        if name == "xdotool":
            args = [tool, "key", "--clearmodifiers", combo]
        else:
            args = [tool, "key", combo]
        result = self._runner.run(args, timeout=10)
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))


class AudioBackend:
    """wpctl / pactl 音量控制和默认设备查询"""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def _wpctl(self) -> str:
        _require_linux()
        path = self._runner.which("wpctl")
        if not path:
            raise BackendMissingError("依赖缺失：音量控制需要 wpctl")
        return path

    def set_volume(self, percent: int) -> None:
        wpctl = self._wpctl()
        result = self._runner.run(
            [wpctl, "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent}%"],
            timeout=5,
        )
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))

    def set_mute(self, muted: bool) -> None:
        wpctl = self._wpctl()
        result = self._runner.run(
            [wpctl, "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if muted else "0"],
            timeout=5,
        )
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))

    def default_devices(self) -> dict[str, str]:
        """默认播放 / 录音设备 id，命令缺失或查询失败返回空字典"""
        _require_linux()
        if self._runner.which("wpctl"):
            return self._devices_from_wpctl()
        if self._runner.which("pactl"):
            return self._devices_from_pactl()
        return {}

    def _devices_from_wpctl(self) -> dict[str, str]:
        wpctl = self._runner.which("wpctl") or "wpctl"
        try:
            result = self._runner.run([wpctl, "status"], timeout=5)
        except BackendError:
            return {}
        if result.returncode != 0:
            return {}
        devices: dict[str, str] = {}
        # 按标题行切 Sinks / Sources 段，带星号的行是该段的默认设备
        section = None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Sinks"):
                section = "render"
                continue
            if stripped.startswith("Sources"):
                section = "capture"
                continue
            if section is None or ("*+" not in stripped and "* " not in stripped):
                continue
            match = re.search(r"(\d+)\.\s", stripped)
            if match:
                devices.setdefault(section, match.group(1))
        return devices

    def _devices_from_pactl(self) -> dict[str, str]:
        pactl = self._runner.which("pactl") or "pactl"
        devices: dict[str, str] = {}
        for flow, args in (
            ("render", [pactl, "get-default-sink"]),
            ("capture", [pactl, "get-default-source"]),
        ):
            try:
                result = self._runner.run(args, timeout=5)
            except BackendError:
                continue
            if result.returncode == 0:
                devices[flow] = result.stdout.strip()
        return devices


class ClipboardBackend:
    """wl-clipboard 或 xclip / xsel 剪贴板，Wayland 和 X11 都认"""

    _READ_ARGS = {
        "wl-paste": ["--no-newline"],
        "xclip": ["-selection", "clipboard", "-o"],
        "xsel": ["--clipboard", "--output"],
    }
    _WRITE_ARGS = {
        "wl-copy": [],
        "xclip": ["-selection", "clipboard"],
        "xsel": ["--clipboard", "--input"],
    }

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def _pick(self, write: bool) -> str | None:
        # 按会话类型选命令：X11 会话装了 wl-clipboard 也不优先走它，
        # wl-paste 在 X11 下会失败，失败还不能回退，否则剪贴板触发器静默失效
        from notmyfault.platform.linux_support import session_type

        if session_type() == "wayland":
            order = ("wl-copy",) if write else ("wl-paste",)
        else:
            order = ("xclip", "xsel")
        for name in order:
            if path := self._runner.which(name):
                return path
        # 会话类型拿不准时按装了哪个算哪个
        if session_type() not in ("wayland", "x11"):
            for name in self._WRITE_ARGS if write else self._READ_ARGS:
                if path := self._runner.which(name):
                    return path
        return None

    def read_text(self) -> str | None:
        _require_linux()
        path = self._pick(write=False)
        if not path:
            raise BackendMissingError(
                "依赖缺失：读取剪贴板需要 wl-clipboard（Wayland）或 xclip/xsel（X11）"
            )
        args = self._READ_ARGS[Path(path).name]
        result = self._runner.run([path, *args], timeout=3)
        return result.stdout if result.returncode == 0 else None

    def write_text(self, text: str) -> None:
        _require_linux()
        path = self._pick(write=True)
        if not path:
            raise BackendMissingError(
                "依赖缺失：写入剪贴板需要 wl-clipboard（Wayland）或 xclip/xsel（X11）"
            )
        args = self._WRITE_ARGS[Path(path).name]
        result = self._runner.run([path, *args], timeout=3, input=text)
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))


class WindowBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def _wmctrl(self) -> str:
        _require_linux()
        path = self._runner.which("wmctrl")
        if not path:
            raise BackendMissingError("依赖缺失：窗口置顶需要 wmctrl")
        return path

    def _find_window(self, wmctrl: str, target: str, title: str) -> str:
        result = self._runner.run([wmctrl, "-l"], timeout=5)
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))

        lines = result.stdout.strip().splitlines()
        if target == "title":
            for line in lines:
                if title.lower() in line.lower():
                    return line.split()[0]
            raise BackendFailedError(f'未找到标题包含 "{title}" 的窗口')
        if not lines:
            raise BackendFailedError("没有可见窗口")
        return lines[0].split()[0]

    def set_pinned(self, action: str, target: str, title: str = "") -> dict[str, str]:
        if target == "title" and not title:
            raise ValueError("按标题匹配时必须填写窗口标题")

        wmctrl = self._wmctrl()
        window_id = self._find_window(wmctrl, target, title)
        if action == "toggle":
            pinned = False
            if xprop := self._runner.which("xprop"):
                result = self._runner.run(
                    [xprop, "-id", window_id, "_NET_WM_STATE"],
                    timeout=5,
                )
                if result.returncode == 0:
                    pinned = "_NET_WM_STATE_ABOVE" in result.stdout
            action = "unpin" if pinned else "pin"

        change = "add,above" if action == "pin" else "remove,above"
        result = self._runner.run(
            [wmctrl, "-i", "-r", window_id, "-b", change],
            timeout=5,
        )
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))
        return {
            "state": "pinned" if action == "pin" else "unpinned",
            "window_id": window_id,
        }


class DisplayBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def set_brightness(self, percent: int) -> None:
        _require_linux()
        brightnessctl = self._runner.which("brightnessctl")
        if not brightnessctl:
            raise BackendMissingError("依赖缺失：亮度控制需要 brightnessctl")
        result = self._runner.run(
            [brightnessctl, "set", f"{percent}%"],
            timeout=5,
        )
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))

    def set_power(self, action: str) -> None:
        _require_linux()
        from notmyfault.platform.linux_support import desktop_environment

        if desktop_environment() == "gnome":
            executable = self._runner.which("gdbus")
            if not executable:
                raise BackendMissingError("依赖缺失：显示器开关需要 gdbus")
            args = [
                executable,
                "call",
                "--session",
                "--dest",
                "org.gnome.ScreenSaver",
                "--object-path",
                "/org/gnome/ScreenSaver",
                "--method",
                "org.gnome.ScreenSaver.SetActive",
                "true" if action == "off" else "false",
            ]
        else:
            executable = self._runner.which("xset")
            if not executable:
                raise BackendMissingError("依赖缺失：显示器开关需要 xset")
            args = [executable, "dpms", "force", action]

        result = self._runner.run(args, timeout=5)
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))


class ScreenshotBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def capture(self, output_path: str, mode: str, fmt: str) -> str:
        _require_linux()
        from notmyfault.platform.linux_support import session_type

        destination = Path(output_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        capture_path = destination
        if fmt.lower() in ("jpg", "jpeg"):
            capture_path = destination.with_suffix(".capture.png")

        if session_type() == "wayland":
            from notmyfault.platform.portal_screenshot import take_screenshot

            take_screenshot(
                str(capture_path),
                interactive=mode == "active_window",
            )
        else:
            command = self._x11_command(capture_path, mode)
            result = self._runner.run(command, timeout=30)
            if result.returncode != 0:
                raise BackendFailedError(_failed_detail(result))

        if capture_path != destination:
            from PIL import Image

            with Image.open(capture_path) as image:
                image.convert("RGB").save(destination, "JPEG", quality=92)
            capture_path.unlink(missing_ok=True)
        return str(destination)

    def _x11_command(self, capture_path: Path, mode: str) -> list[str]:
        if executable := self._runner.which("gnome-screenshot"):
            command = [executable, "-f", str(capture_path)]
            if mode == "active_window":
                command.insert(1, "-w")
            return command
        if executable := self._runner.which("spectacle"):
            return [
                executable,
                "-b",
                "-n",
                "-a" if mode == "active_window" else "-f",
                "-o",
                str(capture_path),
            ]
        if executable := self._runner.which("import"):
            return [executable, "-window", "root", str(capture_path)]
        raise BackendMissingError(
            "依赖缺失：屏幕截图需要 gnome-screenshot、spectacle 或 ImageMagick import"
        )
