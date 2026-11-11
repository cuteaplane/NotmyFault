from __future__ import annotations

import os
import subprocess

from notmyfault.platform._keys import parse_hotkey
from notmyfault.platform._run import CommandRunner, default_runner, failed_detail
from notmyfault.platform.errors import (
    BackendFailedError,
    BackendInvalidError,
    BackendMissingError,
)
from notmyfault.platform.linux import desktop


_SPECIAL_KEYS = {
    "enter": "Return", "return": "Return", "tab": "Tab", "space": "space",
    "backspace": "BackSpace", "delete": "Delete", "escape": "Escape", "esc": "Escape",
    "home": "Home", "end": "End", "pgup": "Page_Up", "pgdn": "Page_Down",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "capslock": "Caps_Lock", "insert": "Insert",
    "minus": "minus", "equal": "equal", "comma": "comma", "dot": "period",
    "slash": "slash", "semicolon": "semicolon", "apostrophe": "apostrophe",
    "grave": "grave", "backslash": "backslash",
    "leftbrace": "bracketleft", "rightbrace": "bracketright",
    **{f"f{index}": f"F{index}" for index in range(1, 25)},
}

_EVDEV_KEYS = {
    "ctrl": 29, "shift": 42, "alt": 56, "super": 125,
    "enter": 28, "return": 28, "tab": 15, "esc": 1, "escape": 1,
    "space": 57, "backspace": 14, "delete": 111, "insert": 110,
    "home": 102, "end": 107, "pgup": 104, "pgdn": 109,
    "up": 103, "down": 108, "left": 105, "right": 106, "capslock": 58,
    "minus": 12, "equal": 13, "leftbrace": 26, "rightbrace": 27,
    "semicolon": 39, "apostrophe": 40, "grave": 41, "backslash": 43,
    "comma": 51, "dot": 52, "slash": 53,
    **dict(zip("1234567890", range(2, 12))),
    **dict(zip("qwertyuiop", range(16, 26))),
    **dict(zip("asdfghjkl", range(30, 39))),
    **dict(zip("zxcvbnm", range(44, 51))),
    **{f"f{index}": 58 + index for index in range(1, 11)},
    "f11": 87, "f12": 88,
    **{f"f{index}": 170 + index for index in range(13, 25)},
}


def _ydotool_env() -> dict[str, str]:
    """补齐 YDOTOOL_SOCKET：未设置时尝试常见套接字路径"""
    env = os.environ.copy()
    current = (env.get("YDOTOOL_SOCKET") or "").strip()
    if current:
        return env
    runtime = (env.get("XDG_RUNTIME_DIR") or "").rstrip("/")
    candidates = []
    if runtime:
        candidates.extend(
            (
                f"{runtime}/.ydotool_socket",
                f"{runtime}/ydotool.socket",
            )
        )
    candidates.append("/tmp/.ydotool_socket")
    for path in candidates:
        if os.path.exists(path):
            env["YDOTOOL_SOCKET"] = path
            break
    return env


def _ydotool_failure_detail(result: subprocess.CompletedProcess, env: dict[str, str]) -> str:
    detail = failed_detail(result)
    socket = env.get("YDOTOOL_SOCKET") or "(未设置)"
    lower = detail.lower()
    if any(token in lower for token in ("connect", "socket", "no such file", "connection refused", "找不到")):
        return (
            f"{detail}；请确认 ydotoold 已启动，YDOTOOL_SOCKET 当前为 {socket}"
        )
    return detail


class KeyboardBackend:
    """xdotool / ydotool 键盘输入"""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def _tool(self) -> tuple[str, str]:
        path = self._runner.which(*(("ydotool",) if desktop.session_type() == "wayland" else ("xdotool", "ydotool")))
        if not path:
            raise BackendMissingError("依赖缺失：模拟键鼠需要 xdotool 或 ydotool")
        return path, os.path.basename(path)

    def _run_input(self, args: list[str], name: str, timeout: float) -> None:
        env = _ydotool_env() if name == "ydotool" else None
        result = self._runner.run(args, timeout=timeout, env=env)
        if result.returncode != 0:
            if name == "ydotool":
                raise BackendFailedError(_ydotool_failure_detail(result, env or {}))
            raise BackendFailedError(failed_detail(result))

    def type_text(self, text: str) -> None:
        if not isinstance(text, str) or not text:
            raise BackendInvalidError("没有要输入的文本")
        tool, name = self._tool()
        if name == "xdotool":
            args = [tool, "type", "--clearmodifiers", "--", text]
        else:
            args = [tool, "type", "--", text]
        self._run_input(args, name, timeout=30)

    def send_hotkey(self, keys: list[str]) -> None:
        parts = parse_hotkey(keys)
        tool, name = self._tool()
        combo = "+".join(_SPECIAL_KEYS.get(key, key) for key in parts)
        if name == "xdotool":
            args = [tool, "key", "--clearmodifiers", combo]
        else:
            try:
                codes = [_EVDEV_KEYS[part.lower()] for part in parts]
            except KeyError as error:
                raise BackendInvalidError(f"ydotool 不支持此按键名称: {error.args[0]}") from None
            args = [tool, "key", *[f"{code}:1" for code in codes],
                    *[f"{code}:0" for code in reversed(codes)]]
        self._run_input(args, name, timeout=10)
