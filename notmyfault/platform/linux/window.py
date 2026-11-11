from __future__ import annotations

import re
import subprocess
import time

from notmyfault.platform._run import CommandRunner, default_runner, failed_detail
from notmyfault.platform.errors import (
    BackendFailedError,
    BackendInvalidError,
    BackendMissingError,
    BackendUnsupportedError,
)
from notmyfault.platform.linux import desktop


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
        if desktop.session_type() == "wayland":
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
            raise BackendFailedError(failed_detail(result))
        return result

    @staticmethod
    def _hex_id(window_id: str) -> str:
        if not isinstance(window_id, str) or not window_id:
            raise BackendInvalidError("窗口 ID 必须是非空字符串")
        try:
            value = int(window_id, 16)
        except ValueError:
            raise BackendInvalidError("窗口 ID 无效") from None
        if value <= 0:
            raise BackendInvalidError("窗口 ID 无效")
        return hex(value)

    @staticmethod
    def _parse_id(text: str) -> int:
        return int(text, 16)

    def _wm_state(self, window_id: str) -> str:
        result = self._checked(
            [self._xprop(), "-id", self._hex_id(window_id), "_NET_WM_STATE"],
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
                window_id = hex(self._parse_id(wid))
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
                "window_id": window_id,
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

    def titles(self) -> dict[str, str]:
        """窗口 id 到标题；优先 wmctrl，否则 xdotool"""
        self._require_x11()
        if self._runner.which("wmctrl"):
            return {
                item["window_id"]: item["title"]
                for item in self._enumerate()
                if item["title"].strip()
            }
        xdotool = self._tool("xdotool", purpose="窗口标题枚举")
        result = self._runner.run(
            [xdotool, "search", "--onlyvisible", "--name", ""],
            timeout=5,
        )
        if result.returncode != 0:
            if result.returncode == 1 and not (result.stderr or "").strip() and not result.stdout.strip():
                return {}
            raise BackendFailedError(failed_detail(result) or "窗口标题查询失败")
        titles: dict[str, str] = {}
        for raw_id in result.stdout.splitlines():
            try:
                window_id = hex(int(raw_id))
            except ValueError:
                raise BackendFailedError("窗口查询返回了无效窗口 ID") from None
            title = self._checked([xdotool, "getwindowname", raw_id], timeout=5).stdout.rstrip("\n")
            if title.strip():
                titles[window_id] = title
        return titles

    def foreground(self) -> str | None:
        self._require_x11()
        result = self._checked(
            [self._xprop(), "-root", "_NET_ACTIVE_WINDOW"], timeout=5
        )
        match = re.search(r"0x[0-9a-fA-F]+", result.stdout)
        if not match or int(match[0], 16) == 0:
            return None
        return hex(int(match[0], 16))

    def _exists(self, window_id: str) -> bool:
        self._require_x11()
        result = self._runner.run(
            [self._xprop(), "-id", self._hex_id(window_id), "WM_CLASS"],
            timeout=5,
        )
        return result.returncode == 0

    def list(self) -> list[dict]:
        return [info for item in self._enumerate()
                if (info := self._with_state(item)) is not None]

    def info(self, window_id: str) -> dict | None:
        self._require_x11()
        base = next((item for item in self._enumerate() if item["window_id"] == window_id), None)
        if base is None:
            # windowunmap 后 wmctrl 列表没有它，但 xprop 还能读到
            if not self._exists(window_id):
                return None
            return {
                "window_id": window_id,
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
        return self._with_state(base)

    def _with_state(self, data: dict) -> dict | None:
        try:
            state = self._wm_state(data["window_id"])
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

    def show(self, window_id: str, state: str) -> None:
        commands = {"show": self._SW_SHOW, "hide": self._SW_HIDE,
                    "minimize": self._SW_MINIMIZE, "maximize": self._SW_MAXIMIZE,
                    "restore": self._SW_RESTORE}
        if not isinstance(state, str) or state not in commands:
            raise BackendInvalidError(f"不支持的窗口显示状态: {state}")
        command = commands[state]
        self._require_x11()
        wmctrl = self._wmctrl()
        wid = self._hex_id(window_id)
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

    def activate(self, window_id: str) -> None:
        self._require_x11()
        self._checked(
            [self._wmctrl(), "-i", "-a", self._hex_id(window_id)], timeout=5
        )

    def move(self, window_id: str, rect: dict) -> None:
        self._require_x11()
        geometry = (
            f"0,{rect['x']},{rect['y']},{rect['width']},{rect['height']}"
        )
        self._checked(
            [self._wmctrl(), "-i", "-r", self._hex_id(window_id), "-e", geometry],
            timeout=5,
        )

    def _pin(self, window_id: str, enabled: bool) -> None:
        self._require_x11()
        change = "add,above" if enabled else "remove,above"
        self._checked(
            [self._wmctrl(), "-i", "-r", self._hex_id(window_id), "-b", change],
            timeout=5,
        )

    def set_opacity(self, window_id: str, percent: int) -> None:
        if type(percent) is not int or not 0 <= percent <= 100:
            raise BackendInvalidError("透明度必须是 0 到 100 的整数")
        raise BackendUnsupportedError("当前 Linux 会话没有统一的窗口透明度接口")

    def close(self, window_id: str) -> None:
        self._require_x11()
        self._checked(
            [self._wmctrl(), "-i", "-c", self._hex_id(window_id)], timeout=5
        )

    def _find_window(self, target: str, title: str) -> str:
        if target == "active":
            window_id = self.foreground()
            if not window_id:
                raise BackendFailedError("没有活动窗口")
            return window_id
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
            return matches[0]["window_id"]
        self._hex_id(target)
        return target

    def set_pinned(self, action: str, target: str, title: str = "") -> dict[str, str]:
        if target == "title" and not title:
            raise BackendInvalidError("按标题匹配时必须填写窗口标题")
        if action not in ("pin", "unpin", "toggle"):
            raise BackendInvalidError(f"不支持的置顶操作: {action}")

        self._require_x11()
        window_id = self._find_window(target, title)

        def is_pinned() -> bool:
            return "_NET_WM_STATE_ABOVE" in self._wm_state(window_id)

        if action == "toggle":
            action = "unpin" if is_pinned() else "pin"
        self._pin(window_id, action == "pin")
        deadline = time.monotonic() + 1
        while is_pinned() != (action == "pin"):
            if time.monotonic() >= deadline:
                raise BackendFailedError("窗口管理器尚未应用置顶状态")
            time.sleep(0.05)
        return {
            "state": "pinned" if action == "pin" else "unpinned",
            "window_id": self._hex_id(window_id),
        }
