"""Linux 外部命令 backend：CommandRunner 统一执行，input / audio / clipboard 三类

错误分四类，kind 字段给上层认：unsupported 是平台不对，backend_missing 是
命令没装，permission_denied 是命令在但系统拒绝执行，backend_failed 是执行
失败或输出没法用
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
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
    """外部命令统一执行入口，超时和找不到命令在这里转成结构化错误"""

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
    if os.name == "nt":
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
        if os.name == "nt":
            raise BackendUnsupportedError("这个 backend 只在 Linux 上可用")
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
