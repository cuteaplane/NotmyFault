from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import re
import shutil
import subprocess
import sys
import threading
import time
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
    def read_output(self, args: list[str], max_chars: int, timeout: float) -> subprocess.CompletedProcess:
        timed_out = threading.Event()
        try:
            with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, errors="replace") as process:
                def expire():
                    timed_out.set()
                    if process.poll() is None:
                        process.kill()

                timer = threading.Timer(timeout, expire)
                timer.daemon = True
                timer.start()
                try:
                    output = process.stdout.read(max_chars)
                    truncated = len(output) == max_chars
                    if truncated and process.poll() is None:
                        process.kill()
                    process.wait()
                finally:
                    timer.cancel()
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                if timed_out.is_set():
                    raise BackendFailedError(f"命令超时（{timeout}s）: {args[0]}")
                return subprocess.CompletedProcess(args, 0 if truncated else process.returncode, output, "")
        except PermissionError as exc:
            raise BackendPermissionDeniedError(f"系统拒绝执行: {args[0]}") from exc
        except FileNotFoundError as exc:
            raise BackendMissingError(f"未安装 {args[0]}") from exc

    def run(
        self,
        args: list[str],
        timeout: float = 10,
        input: str | None = None,
        env: dict[str, str] | None = None,
        encoding: str | None = None,
        creationflags: int = 0,
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding=encoding,
                errors="replace",
                timeout=timeout,
                input=input,
                env=env,
                check=False,
                creationflags=creationflags,
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
    "capslock": "Caps_Lock", "insert": "Insert",
    "minus": "minus", "equal": "equal", "comma": "comma", "dot": "period",
    "slash": "slash", "semicolon": "semicolon", "apostrophe": "apostrophe",
    "grave": "grave", "backslash": "backslash",
    "leftbrace": "bracketleft", "rightbrace": "bracketright",
    **{f"f{index}": f"F{index}" for index in range(1, 25)},
}

_EVDEV_KEYS = {
    "ctrl": 29, "shift": 42, "alt": 56, "super": 125, "win": 125,
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
    detail = _failed_detail(result)
    socket = env.get("YDOTOOL_SOCKET") or "(未设置)"
    lower = detail.lower()
    if any(token in lower for token in ("connect", "socket", "no such file", "connection refused", "找不到")):
        return (
            f"{detail}；请确认 ydotoold 已启动，YDOTOOL_SOCKET 当前为 {socket}"
        )
    return detail


_MOUSE_BUTTONS = frozenset({"left", "right", "middle"})
_XDOT_MOUSE_BUTTON = {"left": "1", "middle": "2", "right": "3"}
# ydotool：低 4 位是按键，0x40 按下，0x80 抬起，二者组合即单击
_YDOT_MOUSE_BASE = {"left": 0x00, "right": 0x01, "middle": 0x02}


def _require_int_coord(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    return value


def _normalize_mouse_button(button: str) -> str:
    value = str(button or "").strip().lower()
    if value not in _MOUSE_BUTTONS:
        raise ValueError(f"不支持的鼠标按键: {button!r}（可选: left/right/middle）")
    return value


class InputBackend:
    """xdotool / ydotool 键盘与鼠标输入"""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def _tool(self) -> tuple[str, str]:
        _require_linux()
        from notmyfault.platform.linux_support import session_type

        path = self._runner.which(*(("ydotool",) if session_type() == "wayland" else ("xdotool", "ydotool")))
        if not path:
            raise BackendMissingError("依赖缺失：模拟键鼠需要 xdotool 或 ydotool")
        return path, os.path.basename(path)

    def _run_input(self, args: list[str], name: str, timeout: float) -> None:
        env = _ydotool_env() if name == "ydotool" else None
        result = self._runner.run(args, timeout=timeout, env=env)
        if result.returncode != 0:
            if name == "ydotool":
                raise BackendFailedError(_ydotool_failure_detail(result, env or {}))
            raise BackendFailedError(_failed_detail(result))

    def type_text(self, text: str) -> None:
        tool, name = self._tool()
        if name == "xdotool":
            args = [tool, "type", "--clearmodifiers", "--", text]
        else:
            args = [tool, "type", "--", text]
        self._run_input(args, name, timeout=30)

    def send_hotkey(self, parts: Iterable[str]) -> None:
        tool, name = self._tool()
        parts = list(parts)
        mapped = []
        for part in parts:
            key = part.lower()
            if key in _SPECIAL_KEYS:
                mapped.append(_SPECIAL_KEYS[key])
            elif key in _MODIFIERS:
                mapped.append(key.replace("win", "super"))
            else:
                mapped.append(part)
        combo = "+".join(mapped)
        if name == "xdotool":
            args = [tool, "key", "--clearmodifiers", combo]
        else:
            try:
                codes = [_EVDEV_KEYS[part.lower()] for part in parts]
            except KeyError as error:
                raise ValueError(f"ydotool 不支持此按键名称: {error.args[0]}") from None
            args = [tool, "key", *[f"{code}:1" for code in codes],
                    *[f"{code}:0" for code in reversed(codes)]]
        self._run_input(args, name, timeout=10)

    def move_mouse(self, x: int, y: int, absolute: bool = True) -> None:
        x = _require_int_coord("横坐标", x)
        y = _require_int_coord("纵坐标", y)
        tool, name = self._tool()
        if name == "xdotool":
            verb = "mousemove" if absolute else "mousemove_relative"
            args = [tool, verb, "--", str(x), str(y)]
        elif absolute:
            args = [tool, "mousemove", "--absolute", "-x", str(x), "-y", str(y)]
        else:
            args = [tool, "mousemove", "-x", str(x), "-y", str(y)]
        self._run_input(args, name, timeout=10)

    def click_mouse(self, button: str = "left", clicks: int = 1) -> None:
        button = _normalize_mouse_button(button)
        if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 1:
            raise ValueError("点击次数必须是不小于 1 的整数")
        tool, name = self._tool()
        if name == "xdotool":
            args = [tool, "click", "--repeat", str(clicks), _XDOT_MOUSE_BUTTON[button]]
        else:
            code = 0xC0 | _YDOT_MOUSE_BASE[button]
            args = [tool, "click", "--repeat", str(clicks), f"0x{code:02X}"]
        self._run_input(args, name, timeout=10)

    def mouse_button(self, button: str, pressed: bool) -> None:
        """按下或抬起指定鼠标键；pressed 为 True 表示按下"""
        button = _normalize_mouse_button(button)
        tool, name = self._tool()
        if name == "xdotool":
            verb = "mousedown" if pressed else "mouseup"
            args = [tool, verb, _XDOT_MOUSE_BUTTON[button]]
        else:
            mask = 0x40 if pressed else 0x80
            code = mask | _YDOT_MOUSE_BASE[button]
            args = [tool, "click", f"0x{code:02X}"]
        self._run_input(args, name, timeout=10)


class AudioBackend:
    """wpctl / pactl 音量控制、默认设备查询与切换"""

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

    def set_default_device(self, flow: str, device_id: str) -> None:
        """把默认播放或录音设备设为 device_id；flow 为 render 或 capture"""
        _require_linux()
        flow = str(flow or "").strip().lower()
        if flow not in ("render", "capture"):
            raise ValueError(f"无效的音频流向: {flow!r}（可选: render/capture）")
        device_id = str(device_id or "").strip()
        if not device_id:
            raise ValueError("必须填写目标音频设备 id")
        if self._runner.which("wpctl"):
            wpctl = self._runner.which("wpctl") or "wpctl"
            result = self._runner.run(
                [wpctl, "set-default", device_id],
                timeout=5,
            )
            if result.returncode != 0:
                raise BackendFailedError(_failed_detail(result))
            return
        if self._runner.which("pactl"):
            pactl = self._runner.which("pactl") or "pactl"
            verb = "set-default-sink" if flow == "render" else "set-default-source"
            result = self._runner.run([pactl, verb, device_id], timeout=5)
            if result.returncode != 0:
                raise BackendFailedError(_failed_detail(result))
            return
        raise BackendMissingError("依赖缺失：设置默认音频设备需要 wpctl 或 pactl")

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
        # wl-clipboard 依赖 Wayland 连接，X11 会话使用 xclip 或 xsel。
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

    def read_text(self, max_chars: int | None = None) -> str | None:
        _require_linux()
        path = self._pick(write=False)
        if not path:
            raise BackendMissingError(
                "依赖缺失：读取剪贴板需要 wl-clipboard（Wayland）或 xclip/xsel（X11）"
            )
        args = self._READ_ARGS[Path(path).name]
        if max_chars is None:
            result = self._runner.run([path, *args], timeout=3)
        else:
            if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
                raise ValueError("max_chars 必须是正整数")
            result = self._runner.read_output([path, *args], max_chars=max_chars, timeout=3)
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
    """X11 窗口枚举与控制，走 wmctrl / xprop / xdotool / xrandr"""

    _SW_HIDE = 0
    _SW_MAXIMIZE = 3
    _SW_MINIMIZE = 6
    _SW_SHOW = 8
    _SW_RESTORE = 9

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def _require_x11(self) -> None:
        _require_linux()
        from notmyfault.platform.linux_support import session_type

        if session_type() == "wayland":
            raise BackendUnsupportedError(
                "Wayland 下普通应用无法枚举或控制其他窗口"
            )

    def _tool(self, *names: str, purpose: str) -> str:
        path = self._runner.which(*names)
        if not path:
            raise BackendMissingError(
                f"依赖缺失：{purpose}需要 " + " 或 ".join(names)
            )
        return path

    def _wmctrl(self) -> str:
        return self._tool("wmctrl", purpose="窗口控制")

    def _xprop(self) -> str:
        return self._tool("xprop", purpose="读取窗口属性")

    def _xrandr(self) -> str:
        return self._tool("xrandr", purpose="读取显示器布局")

    def _checked(self, args: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
        result = self._runner.run(args, timeout=timeout)
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))
        return result

    @staticmethod
    def _hex_id(hwnd: int) -> str:
        return hex(int(hwnd))

    @staticmethod
    def _parse_id(text: str) -> int:
        return int(text, 16)

    def _wm_state(self, hwnd: int) -> str:
        result = self._checked(
            [self._xprop(), "-id", self._hex_id(hwnd), "_NET_WM_STATE"],
            timeout=5,
        )
        return result.stdout

    def _enumerate(self) -> list[dict]:
        self._require_x11()
        result = self._checked([self._wmctrl(), "-lGpx"], timeout=5)
        windows: list[dict] = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 9)
            if len(parts) < 9:
                continue
            wid, _desktop, pid_text, x_text, y_text, w_text, h_text, wm_class, _host = parts[:9]
            title = parts[9] if len(parts) > 9 else ""
            try:
                hwnd = self._parse_id(wid)
                pid = int(pid_text)
                x, y = int(x_text), int(y_text)
                width, height = int(w_text), int(h_text)
            except ValueError:
                continue
            class_name = wm_class.rsplit(".", 1)[-1] if wm_class else ""
            process_name = ""
            if pid > 0:
                try:
                    import psutil

                    process_name = psutil.Process(pid).name()
                except Exception:
                    process_name = ""
            windows.append({
                "hwnd": hwnd,
                "title": title,
                "class_name": class_name,
                "pid": pid,
                "process_name": process_name,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "visible": True,
                "minimized": False,
                "maximized": False,
                "topmost": False,
                "opacity": None,
            })
        return windows

    def list_titles(self) -> dict[int, str]:
        """窗口 id 到标题；优先 wmctrl，否则 xdotool"""
        self._require_x11()
        if self._runner.which("wmctrl"):
            return {
                item["hwnd"]: item["title"]
                for item in self._enumerate()
                if item["title"].strip()
            }
        xdotool = self._tool("xdotool", purpose="窗口标题枚举")
        result = self._runner.run(
            [xdotool, "search", "--name", "", "getwindowname", "%@"],
            timeout=5,
        )
        if result.returncode != 0:
            if result.returncode == 1 and not (result.stderr or "").strip() and not result.stdout.strip():
                return {}
            raise BackendFailedError(_failed_detail(result) or "窗口标题查询失败")
        titles: dict[int, str] = {}
        for index, title in enumerate(result.stdout.splitlines()):
            if title.strip():
                titles[index] = title
        return titles

    def foreground(self) -> int:
        self._require_x11()
        result = self._checked(
            [self._xprop(), "-root", "_NET_ACTIVE_WINDOW"], timeout=5
        )
        match = re.search(r"0x[0-9a-fA-F]+", result.stdout)
        if not match or int(match[0], 16) == 0:
            return 0
        return int(match[0], 16)

    def exists(self, hwnd: int) -> bool:
        self._require_x11()
        result = self._runner.run(
            [self._xprop(), "-id", self._hex_id(hwnd), "WM_CLASS"],
            timeout=5,
        )
        return result.returncode == 0

    def handles(self) -> list[int]:
        return [item["hwnd"] for item in self._enumerate()]

    def info(self, hwnd: int) -> dict | None:
        self._require_x11()
        base = next((item for item in self._enumerate() if item["hwnd"] == hwnd), None)
        if base is None:
            # windowunmap 后 wmctrl 列表没有它，但 xprop 还能读到
            if not self.exists(hwnd):
                return None
            return {
                "hwnd": int(hwnd),
                "title": "",
                "class_name": "",
                "pid": 0,
                "process_name": "",
                "x": 0,
                "y": 0,
                "width": 0,
                "height": 0,
                "visible": False,
                "minimized": False,
                "maximized": False,
                "topmost": False,
                "opacity": None,
            }
        data = dict(base)
        try:
            state = self._wm_state(hwnd)
        except BackendFailedError:
            return None
        data["minimized"] = "_NET_WM_STATE_HIDDEN" in state
        data["maximized"] = (
            "_NET_WM_STATE_MAXIMIZED_VERT" in state
            and "_NET_WM_STATE_MAXIMIZED_HORZ" in state
        )
        data["topmost"] = "_NET_WM_STATE_ABOVE" in state
        # wmctrl 列表里的窗口仍算可见，最小化只反映在 minimized
        data["visible"] = True
        data["opacity"] = None
        return data

    def monitors(self) -> list[dict]:
        self._require_x11()
        result = self._checked([self._xrandr(), "--listmonitors"], timeout=5)
        monitors: list[dict] = []
        pattern = re.compile(
            r"^\s*(\d+):\s+\+?(\*)?(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)\s*$"
        )
        for line in result.stdout.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            index = int(match.group(1)) + 1
            primary = match.group(2) == "*"
            width, height = int(match.group(4)), int(match.group(5))
            x, y = int(match.group(6)), int(match.group(7))
            device = match.group(8)
            rect = {"x": x, "y": y, "width": width, "height": height}
            # EWMH 工作区要逐显示器另查，这里先用整屏矩形
            monitors.append({
                "handle": index,
                "device": device,
                "primary": primary,
                "index": index,
                "bounds": dict(rect),
                "work": dict(rect),
            })
        monitors.sort(key=lambda item: (not item["primary"], item["bounds"]["x"], item["bounds"]["y"]))
        for index, monitor in enumerate(monitors, 1):
            monitor["index"] = index
            monitor["handle"] = index
        return monitors

    def monitor_for(self, hwnd: int) -> int:
        data = self.info(hwnd)
        monitors = self.monitors()
        if not data or not monitors:
            return monitors[0]["handle"] if monitors else 0
        cx = data["x"] + data["width"] // 2
        cy = data["y"] + data["height"] // 2
        for monitor in monitors:
            bounds = monitor["bounds"]
            if (
                bounds["x"] <= cx < bounds["x"] + bounds["width"]
                and bounds["y"] <= cy < bounds["y"] + bounds["height"]
            ):
                return monitor["handle"]
        primary = next((item for item in monitors if item["primary"]), monitors[0])
        return primary["handle"]

    def show(self, hwnd: int, command: int) -> None:
        self._require_x11()
        wmctrl = self._wmctrl()
        wid = self._hex_id(hwnd)
        if command == self._SW_MINIMIZE:
            xdotool = self._runner.which("xdotool")
            if xdotool:
                self._checked([xdotool, "windowminimize", wid], timeout=5)
            else:
                self._checked(
                    [wmctrl, "-i", "-r", wid, "-b", "add,hidden"], timeout=5
                )
            return
        if command == self._SW_MAXIMIZE:
            self._checked(
                [wmctrl, "-i", "-r", wid, "-b", "add,maximized_vert,maximized_horz"],
                timeout=5,
            )
            return
        if command == self._SW_RESTORE:
            self._checked(
                [
                    wmctrl,
                    "-i",
                    "-r",
                    wid,
                    "-b",
                    "remove,hidden,maximized_vert,maximized_horz",
                ],
                timeout=5,
            )
            return
        if command == self._SW_SHOW:
            xdotool = self._runner.which("xdotool")
            if xdotool:
                self._checked([xdotool, "windowmap", wid], timeout=5)
            else:
                self._checked(
                    [wmctrl, "-i", "-r", wid, "-b", "remove,hidden"], timeout=5
                )
            return
        if command == self._SW_HIDE:
            xdotool = self._runner.which("xdotool")
            if not xdotool:
                raise BackendUnsupportedError(
                    "隐藏窗口需要 xdotool（windowunmap）"
                )
            self._checked([xdotool, "windowunmap", wid], timeout=5)
            return
        raise ValueError(f"不支持的窗口显示命令: {command}")

    def activate(self, hwnd: int) -> None:
        self._require_x11()
        self._checked(
            [self._wmctrl(), "-i", "-a", self._hex_id(hwnd)], timeout=5
        )

    def position(self, hwnd: int, rect: dict) -> None:
        self._require_x11()
        geometry = (
            f"0,{rect['x']},{rect['y']},{rect['width']},{rect['height']}"
        )
        self._checked(
            [self._wmctrl(), "-i", "-r", self._hex_id(hwnd), "-e", geometry],
            timeout=5,
        )

    def pin(self, hwnd: int, enabled: bool) -> None:
        self._require_x11()
        change = "add,above" if enabled else "remove,above"
        self._checked(
            [self._wmctrl(), "-i", "-r", self._hex_id(hwnd), "-b", change],
            timeout=5,
        )

    def opacity(self, hwnd: int, percent: int) -> None:
        raise BackendUnsupportedError("当前 Linux 会话没有统一的窗口透明度接口")

    def close(self, hwnd: int) -> None:
        self._require_x11()
        self._checked(
            [self._wmctrl(), "-i", "-c", self._hex_id(hwnd)], timeout=5
        )

    def _find_window(self, target: str, title: str) -> int:
        if target == "active":
            hwnd = self.foreground()
            if not hwnd:
                raise BackendFailedError("没有活动窗口")
            return hwnd
        if target == "title":
            matches = [
                item
                for item in self._enumerate()
                if title.lower() in item["title"].lower()
            ]
            if not matches:
                raise BackendFailedError(f'未找到标题包含 "{title}" 的窗口')
            if len(matches) > 1:
                raise BackendFailedError(
                    f"标题匹配到 {len(matches)} 个窗口，请使用更精确的标题"
                )
            return matches[0]["hwnd"]
        raise ValueError(f"不支持的窗口目标: {target}")

    def set_pinned(self, action: str, target: str, title: str = "") -> dict[str, str]:
        if target == "title" and not title:
            raise ValueError("按标题匹配时必须填写窗口标题")
        if action not in ("pin", "unpin", "toggle"):
            raise ValueError(f"不支持的置顶操作: {action}")

        self._require_x11()
        hwnd = self._find_window(target, title)

        def is_pinned() -> bool:
            return "_NET_WM_STATE_ABOVE" in self._wm_state(hwnd)

        if action == "toggle":
            action = "unpin" if is_pinned() else "pin"
        self.pin(hwnd, action == "pin")
        deadline = time.monotonic() + 1
        while is_pinned() != (action == "pin"):
            if time.monotonic() >= deadline:
                raise BackendFailedError("窗口管理器尚未应用置顶状态")
            time.sleep(0.05)
        return {
            "state": "pinned" if action == "pin" else "unpinned",
            "window_id": self._hex_id(hwnd),
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
        from notmyfault.platform.linux_support import desktop_environment, session_type

        if action not in ("on", "off"):
            raise ValueError(f"不支持的显示器电源操作: {action}")

        desktop = desktop_environment()
        session = session_type()
        active = "true" if action == "off" else "false"
        errors: list[str] = []

        def run_screensaver(dest: str, object_path: str, method: str) -> bool:
            executable = self._runner.which("gdbus")
            if not executable:
                errors.append("未安装 gdbus")
                return False
            result = self._runner.run(
                [
                    executable,
                    "call",
                    "--session",
                    "--dest",
                    dest,
                    "--object-path",
                    object_path,
                    "--method",
                    method,
                    active,
                ],
                timeout=5,
            )
            if result.returncode == 0:
                return True
            errors.append(f"{dest}: {_failed_detail(result)}")
            return False

        def run_xset() -> bool:
            executable = self._runner.which("xset")
            if not executable:
                errors.append("未安装 xset")
                return False
            result = self._runner.run(
                [executable, "dpms", "force", action],
                timeout=5,
            )
            if result.returncode == 0:
                return True
            errors.append(f"xset: {_failed_detail(result)}")
            return False

        if desktop == "gnome":
            if run_screensaver(
                "org.gnome.ScreenSaver",
                "/org/gnome/ScreenSaver",
                "org.gnome.ScreenSaver.SetActive",
            ):
                return
            raise BackendFailedError(
                "；".join(errors) or "GNOME 显示器开关失败"
            )

        if desktop == "kde":
            for dest, object_path in (
                ("org.kde.screensaver", "/ScreenSaver"),
                ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"),
            ):
                if run_screensaver(
                    dest,
                    object_path,
                    "org.freedesktop.ScreenSaver.SetActive",
                ):
                    return
            if session != "wayland" and run_xset():
                return
            raise BackendFailedError(
                "；".join(errors)
                or "KDE 显示器开关失败，需要可用的 ScreenSaver 接口或 xset"
            )

        if session != "wayland" and run_xset():
            return

        if session == "wayland":
            raise BackendUnsupportedError(
                f"当前桌面（{desktop}）在 Wayland 下没有可用的显示器开关接口；"
                "GNOME 走 org.gnome.ScreenSaver，KDE 走 org.kde.screensaver，"
                "其他桌面请使用 X11 会话上的 xset"
            )
        raise BackendMissingError(
            "；".join(errors) or "依赖缺失：显示器开关需要 xset"
        )


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


_OCR_LANGUAGE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\+[A-Za-z][A-Za-z0-9_]*)*$"
)


class OcrBackend:
    """用本机 tesseract 识别一张图片里的文字"""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def recognize(self, image_path: str, language: str) -> str:
        if not isinstance(image_path, str) or not image_path.strip():
            raise ValueError("缺少图片路径")
        if not isinstance(language, str) or not _OCR_LANGUAGE_RE.fullmatch(language):
            raise ValueError(f"无法识别的语言代码: {language!r}")
        executable = self._runner.which("tesseract")
        if not executable:
            raise BackendMissingError("依赖缺失：屏幕文字识别需要 tesseract")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        result = self._runner.run(
            [executable, image_path, "stdout", "-l", language],
            timeout=60,
            encoding="utf-8",
            creationflags=flags,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise BackendFailedError(detail or f"tesseract 退出码 {result.returncode}")
        return (result.stdout or "").lstrip("\ufeff").strip()


_LINUX_POWER_PROFILES = {
    "balanced": "balanced",
    "high_performance": "performance",
    "power_saver": "power-saver",
}


class PowerProfileBackend:
    """powerprofilesctl 电源模式切换"""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def set_profile(self, plan: str) -> str:
        _require_linux()
        profile = _LINUX_POWER_PROFILES.get(plan)
        if profile is None:
            raise ValueError(
                f"未知电源计划: {plan!r}"
                "（可选: balanced/high_performance/power_saver）"
            )
        path = self._runner.which("powerprofilesctl")
        if not path:
            raise BackendMissingError("依赖缺失：电源计划需要 powerprofilesctl")
        result = self._runner.run([path, "set", profile], timeout=10)
        if result.returncode != 0:
            raise BackendFailedError(_failed_detail(result))
        return profile


class SleepResumeBackend:
    """监听 logind PrepareForSleep(False)，在睡眠恢复时置位"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resumed = False
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        _require_linux()
        if self._thread is not None:
            return
        try:
            import dbus_next  # noqa: F401
        except ImportError as error:
            raise BackendMissingError("依赖缺失：睡眠恢复监听需要 dbus-next") from error
        self._stop.clear()
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run, name="nmf-sleep-resume", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            self.stop()
            raise BackendFailedError("连接 logind 睡眠恢复信号超时")
        if self._start_error is not None:
            error = self._start_error
            self.stop()
            raise BackendFailedError(f"无法监听睡眠恢复: {error}") from error

    def consume_resume(self) -> bool:
        with self._lock:
            value = self._resumed
            self._resumed = False
            return value

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None
        self._loop = None

    def _mark_resumed(self) -> None:
        with self._lock:
            self._resumed = True

    def _run(self) -> None:
        try:
            asyncio.run(self._watch())
        except Exception as error:
            self._start_error = error
            self._ready.set()

    async def _watch(self) -> None:
        from dbus_next import BusType, Message, MessageType
        from dbus_next.aio import MessageBus

        self._loop = asyncio.get_running_loop()
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            match_rule = (
                "type='signal',"
                "sender='org.freedesktop.login1',"
                "interface='org.freedesktop.login1.Manager',"
                "member='PrepareForSleep',"
                "path='/org/freedesktop/login1'"
            )

            def on_message(message: Message) -> bool:
                if (
                    message.message_type == MessageType.SIGNAL
                    and message.interface == "org.freedesktop.login1.Manager"
                    and message.member == "PrepareForSleep"
                    and message.body
                    and message.body[0] is False
                ):
                    self._mark_resumed()
                    return True
                return False

            bus.add_message_handler(on_message)
            match_reply = await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[match_rule],
                )
            )
            if match_reply.message_type == MessageType.ERROR:
                raise BackendFailedError("无法订阅 logind PrepareForSleep 信号")
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.2)
        finally:
            with suppress(Exception):
                bus.disconnect()
