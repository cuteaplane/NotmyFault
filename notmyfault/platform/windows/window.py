import ctypes
import time
from contextlib import contextmanager
from ctypes import wintypes as wt

import psutil

from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError
from notmyfault.platform.windows import _win32


def _rect(rect):
    return {"x": rect.left, "y": rect.top,
            "width": rect.right - rect.left, "height": rect.bottom - rect.top}


class WindowBackend:
    def __init__(self, runner=None):
        self.api = _win32.user32()

    @staticmethod
    def _handle(window_id):
        if not isinstance(window_id, str) or not window_id:
            raise BackendInvalidError("窗口 ID 必须是非空字符串")
        try:
            value = int(window_id)
        except ValueError:
            raise BackendInvalidError("窗口 ID 无效") from None
        if value <= 0:
            raise BackendInvalidError("窗口 ID 无效")
        return value

    def _check(self, result, operation):
        if not result:
            code = ctypes.get_last_error()
            reason = ctypes.FormatError(code).strip() if code else "Windows 未接受请求"
            raise BackendFailedError(f"{operation}失败: {reason}")
        return result

    @contextmanager
    def _coordinates(self):
        if not hasattr(self.api, "SetThreadDpiAwarenessContext"):
            yield
            return
        previous = self.api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        self._check(previous, "设置线程 DPI 坐标")
        try:
            yield
        finally:
            self.api.SetThreadDpiAwarenessContext(previous)

    def foreground(self):
        with NATIVE_LOCK:
            window_id = self.api.GetForegroundWindow()
            return str(window_id) if window_id else None

    def _handles(self):
        found = []
        callback = _win32.WNDENUMPROC(lambda window_id, _: found.append(int(window_id)) or True)
        with NATIVE_LOCK:
            self._check(self.api.EnumWindows(callback, 0), "枚举窗口")
        return found

    def info(self, window_id):
        window_id = self._handle(window_id)
        with NATIVE_LOCK, self._coordinates():
            if not self.api.IsWindow(window_id):
                return None
            if self.api.GetAncestor(window_id, 2) != window_id:
                return None
            title = ctypes.create_unicode_buffer(self.api.GetWindowTextLengthW(window_id) + 1)
            self.api.GetWindowTextW(window_id, title, len(title))
            class_name = ctypes.create_unicode_buffer(256)
            self.api.GetClassNameW(window_id, class_name, len(class_name))
            pid, rect = wt.DWORD(), wt.RECT()
            self.api.GetWindowThreadProcessId(window_id, ctypes.byref(pid))
            if not self.api.GetWindowRect(window_id, ctypes.byref(rect)):
                if not self.api.IsWindow(window_id):
                    return None
                self._check(False, "读取窗口位置")
            style = self.api.GetWindowLongW(window_id, -20)
            opacity = 100
            if style & 0x80000:
                color, alpha, flags = wt.DWORD(), wt.BYTE(), wt.DWORD()
                opacity = None
                if self.api.GetLayeredWindowAttributes(window_id, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags)):
                    opacity = round(alpha.value * 100 / 255) if flags.value & 2 else 100
            data = {"window_id": str(window_id), "title": title.value, "class_name": class_name.value,
                    "pid": pid.value, **_rect(rect), "visible": bool(self.api.IsWindowVisible(window_id)),
                    "minimized": bool(self.api.IsIconic(window_id)), "maximized": bool(self.api.IsZoomed(window_id)),
                    "topmost": bool(style & 8), "opacity": opacity}
        try:
            data["process_name"] = psutil.Process(data["pid"]).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            data["process_name"] = ""
        return data

    def monitors(self):
        handles = []
        callback = _win32.MONITORENUMPROC(lambda monitor, *_: handles.append(monitor) or True)
        with NATIVE_LOCK, self._coordinates():
            self._check(self.api.EnumDisplayMonitors(None, None, callback, 0), "枚举显示器")
            result = []
            for handle in handles:
                info = _win32.MonitorInfo(size=ctypes.sizeof(_win32.MonitorInfo))
                self._check(self.api.GetMonitorInfoW(handle, ctypes.byref(info)), "读取显示器")
                result.append({"handle": int(handle), "device": info.device,
                               "primary": bool(info.flags & 1), "bounds": _rect(info.bounds), "work": _rect(info.work)})
        result.sort(key=lambda item: (not item["primary"], item["bounds"]["x"], item["bounds"]["y"]))
        for index, monitor in enumerate(result, 1):
            monitor["index"] = index
        return result

    def show(self, window_id: str, state: str) -> None:
        commands = {"show": 8, "hide": 0, "minimize": 6, "maximize": 3, "restore": 9}
        if not isinstance(state, str) or state not in commands:
            raise BackendInvalidError(f"不支持的窗口显示状态: {state}")
        command = commands[state]
        window_id = self._handle(window_id)
        with NATIVE_LOCK:
            self._check(self.api.ShowWindowAsync(window_id, command), "切换窗口状态")

    def activate(self, window_id):
        window_id = self._handle(window_id)
        with NATIVE_LOCK:
            if not self.api.SetForegroundWindow(window_id):
                raise BackendFailedError("Windows 拒绝切换前台窗口，请先与目标应用交互后重试")

    def move(self, window_id, rect):
        window_id = self._handle(window_id)
        with NATIVE_LOCK, self._coordinates():
            self._check(self.api.SetWindowPos(window_id, None, rect["x"], rect["y"], rect["width"], rect["height"],
                                             0x4000 | 0x10 | 0x4 | 0x200), "移动或缩放窗口")

    def _pin(self, window_id, enabled):
        with NATIVE_LOCK:
            self._check(self.api.SetWindowPos(window_id, -1 if enabled else -2, 0, 0, 0, 0,
                                             0x4000 | 0x10 | 0x1 | 0x2 | 0x200), "设置窗口置顶")

    def set_opacity(self, window_id, percent):
        window_id = self._handle(window_id)
        if type(percent) is not int or not 0 <= percent <= 100:
            raise BackendInvalidError("透明度必须是 0 到 100 的整数")
        with NATIVE_LOCK:
            style = self.api.GetWindowLongW(window_id, -20)
            color, alpha, flags = wt.DWORD(), wt.BYTE(), wt.DWORD()
            if style & 0x80000:
                self._check(self.api.GetLayeredWindowAttributes(window_id, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags)),
                            "读取窗口透明度")
            ctypes.set_last_error(0)
            previous = self.api.SetWindowLongW(window_id, -20, style | 0x80000)
            if not previous and ctypes.get_last_error():
                self._check(False, "设置透明窗口样式")
            self._check(self.api.SetLayeredWindowAttributes(window_id, color.value, round(percent * 255 / 100), (flags.value & 1) | 2),
                        "设置窗口透明度")

    def close(self, window_id):
        window_id = self._handle(window_id)
        with NATIVE_LOCK:
            self._check(self.api.PostMessageW(window_id, 0x10, 0, 0), "发送窗口关闭请求")

    def list(self):
        return [info for window_id in self._handles() if (info := self.info(str(window_id))) is not None]

    def titles(self):
        return {item["window_id"]: item["title"] for item in self.list()
                if item["visible"] and item["title"].strip()}

    def set_pinned(self, action, target, title=""):
        if action not in {"pin", "unpin", "toggle"}:
            raise BackendInvalidError(f"不支持的置顶操作: {action}")
        if target == "active":
            window_id = self.foreground()
            if window_id is None:
                raise BackendFailedError("没有活动窗口")
        elif target == "title":
            if not title:
                raise BackendInvalidError("按标题匹配时必须填写窗口标题")
            matches = [key for key, value in self.titles().items() if title.lower() in value.lower()]
            if len(matches) != 1:
                raise BackendFailedError(f"标题匹配到 {len(matches)} 个窗口，请使用更精确的标题")
            window_id = matches[0]
        else:
            self._handle(target)
            window_id = target
        window_id = self._handle(window_id)
        with NATIVE_LOCK:
            if not self.api.IsWindow(window_id):
                raise BackendFailedError("窗口已不存在")
            enabled = not bool(self.api.GetWindowLongW(window_id, -20) & 8) if action == "toggle" else action == "pin"
            self._pin(window_id, enabled)
        deadline = time.monotonic() + 1
        while True:
            with NATIVE_LOCK:
                if bool(self.api.GetWindowLongW(window_id, -20) & 8) == enabled:
                    break
            if time.monotonic() >= deadline:
                raise BackendFailedError("窗口置顶状态未生效")
            time.sleep(0.05)
        return {"state": "pinned" if enabled else "unpinned", "window_id": str(window_id)}
