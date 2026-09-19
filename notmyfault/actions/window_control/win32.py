import ctypes
from contextlib import contextmanager
from ctypes import wintypes as wt

import psutil

from notmyfault.plugin_api import native_lock


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("bounds", wt.RECT), ("work", wt.RECT),
                ("flags", wt.DWORD), ("device", wt.WCHAR * 32)]


_ENUM_WINDOW = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_ENUM_MONITOR = ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)


def _rect(rect):
    return {"x": rect.left, "y": rect.top,
            "width": rect.right - rect.left, "height": rect.bottom - rect.top}


class Windows:
    def __init__(self):
        # WinDLL 实例单独保存函数签名，不修改其他插件使用的 ctypes.windll 对象。
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        declarations = {
            "IsWindow": (wt.BOOL, [wt.HWND]),
            "IsWindowVisible": (wt.BOOL, [wt.HWND]),
            "IsIconic": (wt.BOOL, [wt.HWND]),
            "IsZoomed": (wt.BOOL, [wt.HWND]),
            "GetForegroundWindow": (wt.HWND, []),
            "GetAncestor": (wt.HWND, [wt.HWND, wt.UINT]),
            "GetWindowTextLengthW": (ctypes.c_int, [wt.HWND]),
            "GetWindowTextW": (ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
            "GetClassNameW": (ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
            "GetWindowThreadProcessId": (wt.DWORD, [wt.HWND, ctypes.POINTER(wt.DWORD)]),
            "GetWindowRect": (wt.BOOL, [wt.HWND, ctypes.POINTER(wt.RECT)]),
            "EnumWindows": (wt.BOOL, [_ENUM_WINDOW, wt.LPARAM]),
            "GetWindowLongW": (wt.LONG, [wt.HWND, ctypes.c_int]),
            "SetWindowLongW": (wt.LONG, [wt.HWND, ctypes.c_int, wt.LONG]),
            "ShowWindowAsync": (wt.BOOL, [wt.HWND, ctypes.c_int]),
            "SetForegroundWindow": (wt.BOOL, [wt.HWND]),
            "SetWindowPos": (wt.BOOL, [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wt.UINT]),
            "PostMessageW": (wt.BOOL, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
            "SetLayeredWindowAttributes": (wt.BOOL, [wt.HWND, wt.DWORD, wt.BYTE, wt.DWORD]),
            "GetLayeredWindowAttributes": (wt.BOOL, [wt.HWND, ctypes.POINTER(wt.DWORD),
                                                    ctypes.POINTER(wt.BYTE), ctypes.POINTER(wt.DWORD)]),
            "MonitorFromWindow": (wt.HANDLE, [wt.HWND, wt.DWORD]),
            "EnumDisplayMonitors": (wt.BOOL, [wt.HDC, ctypes.POINTER(wt.RECT), _ENUM_MONITOR, wt.LPARAM]),
            "GetMonitorInfoW": (wt.BOOL, [wt.HANDLE, ctypes.POINTER(MonitorInfo)]),
            "SetThreadDpiAwarenessContext": (ctypes.c_void_p, [ctypes.c_void_p]),
        }
        for name, (result, arguments) in declarations.items():
            function = getattr(self.api, name, None)
            if function is None and name == "SetThreadDpiAwarenessContext":
                continue
            if function is None:
                raise RuntimeError(f"Windows 缺少窗口接口: {name}")
            function.restype, function.argtypes = result, arguments

    def _check(self, result, operation):
        if not result:
            code = ctypes.get_last_error()
            reason = ctypes.FormatError(code).strip() if code else "Windows 未接受请求"
            raise RuntimeError(f"{operation}失败: {reason}")
        return result

    @contextmanager
    def coordinates(self):
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
        with native_lock():
            return int(self.api.GetForegroundWindow() or 0)

    def exists(self, hwnd):
        with native_lock():
            return bool(self.api.IsWindow(hwnd))

    def handles(self):
        found = []
        callback = _ENUM_WINDOW(lambda hwnd, _: found.append(int(hwnd)) or True)
        with native_lock():
            self._check(self.api.EnumWindows(callback, 0), "枚举窗口")
        return found

    def info(self, hwnd):
        with native_lock(), self.coordinates():
            if not self.api.IsWindow(hwnd):
                return None
            if self.api.GetAncestor(hwnd, 2) != hwnd:
                return None
            title = ctypes.create_unicode_buffer(self.api.GetWindowTextLengthW(hwnd) + 1)
            self.api.GetWindowTextW(hwnd, title, len(title))
            class_name = ctypes.create_unicode_buffer(256)
            self.api.GetClassNameW(hwnd, class_name, len(class_name))
            pid, rect = wt.DWORD(), wt.RECT()
            self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not self.api.GetWindowRect(hwnd, ctypes.byref(rect)):
                if not self.api.IsWindow(hwnd):
                    return None
                self._check(False, "读取窗口位置")
            style = self.api.GetWindowLongW(hwnd, -20)
            opacity = 100
            if style & 0x80000:
                color, alpha, flags = wt.DWORD(), wt.BYTE(), wt.DWORD()
                opacity = None
                if self.api.GetLayeredWindowAttributes(hwnd, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags)):
                    opacity = round(alpha.value * 100 / 255) if flags.value & 2 else 100
            data = {"hwnd": hwnd, "title": title.value, "class_name": class_name.value,
                    "pid": pid.value, **_rect(rect), "visible": bool(self.api.IsWindowVisible(hwnd)),
                    "minimized": bool(self.api.IsIconic(hwnd)), "maximized": bool(self.api.IsZoomed(hwnd)),
                    "topmost": bool(style & 8), "opacity": opacity}
        try:
            data["process_name"] = psutil.Process(data["pid"]).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            data["process_name"] = ""
        return data

    def monitors(self):
        handles = []
        callback = _ENUM_MONITOR(lambda monitor, *_: handles.append(monitor) or True)
        with native_lock(), self.coordinates():
            self._check(self.api.EnumDisplayMonitors(None, None, callback, 0), "枚举显示器")
            result = []
            for handle in handles:
                info = MonitorInfo(size=ctypes.sizeof(MonitorInfo))
                self._check(self.api.GetMonitorInfoW(handle, ctypes.byref(info)), "读取显示器")
                result.append({"handle": int(handle), "device": info.device,
                               "primary": bool(info.flags & 1), "bounds": _rect(info.bounds), "work": _rect(info.work)})
        result.sort(key=lambda item: (not item["primary"], item["bounds"]["x"], item["bounds"]["y"]))
        for index, monitor in enumerate(result, 1):
            monitor["index"] = index
        return result

    def monitor_for(self, hwnd):
        with native_lock():
            return int(self.api.MonitorFromWindow(hwnd, 2) or 0)

    def show(self, hwnd, command):
        with native_lock():
            self._check(self.api.ShowWindowAsync(hwnd, command), "切换窗口状态")

    def activate(self, hwnd):
        with native_lock():
            if not self.api.SetForegroundWindow(hwnd):
                raise RuntimeError("Windows 拒绝切换前台窗口，请先与目标应用交互后重试")

    def position(self, hwnd, rect):
        with native_lock(), self.coordinates():
            self._check(self.api.SetWindowPos(hwnd, None, rect["x"], rect["y"], rect["width"], rect["height"],
                                             0x4000 | 0x10 | 0x4 | 0x200), "移动或缩放窗口")

    def pin(self, hwnd, enabled):
        with native_lock():
            self._check(self.api.SetWindowPos(hwnd, -1 if enabled else -2, 0, 0, 0, 0,
                                             0x4000 | 0x10 | 0x1 | 0x2 | 0x200), "设置窗口置顶")

    def opacity(self, hwnd, percent):
        with native_lock():
            style = self.api.GetWindowLongW(hwnd, -20)
            color, alpha, flags = wt.DWORD(), wt.BYTE(), wt.DWORD()
            if style & 0x80000:
                self._check(self.api.GetLayeredWindowAttributes(hwnd, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags)),
                            "读取窗口透明度")
            ctypes.set_last_error(0)
            previous = self.api.SetWindowLongW(hwnd, -20, style | 0x80000)
            if not previous and ctypes.get_last_error():
                self._check(False, "设置透明窗口样式")
            self._check(self.api.SetLayeredWindowAttributes(hwnd, color.value, round(percent * 255 / 100), (flags.value & 1) | 2),
                        "设置窗口透明度")

    def close(self, hwnd):
        with native_lock():
            self._check(self.api.PostMessageW(hwnd, 0x10, 0, 0), "发送窗口关闭请求")
