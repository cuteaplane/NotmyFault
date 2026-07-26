"""系统托盘图标 — 用户接触引擎的第一界面。

提供托盘图标、右键菜单、气球通知、开机自启管理。
Windows 实现，跨平台时替换本模块即可。
"""

import os
import sys
import threading
from typing import Optional, Callable

import win32api
import win32con
import win32gui

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ICON_PATH = os.path.join(PROJECT_ROOT, "logo.ico")

WM_TASKBARCREATED = win32gui.RegisterWindowMessage("TaskbarCreated")
WM_TRAY_CALLBACK = win32con.WM_USER + 1024
ID_TRAY_ICON = 1

MID_OPEN_DASHBOARD = 1001
MID_TOGGLE_ENGINE = 1002
MID_SEPARATOR_1 = 1003
MID_AUTO_START = 1004
MID_SEPARATOR_2 = 1005
MID_EXIT = 1006

NIIF_INFO = 1
NIIF_WARNING = 2
NIIF_ERROR = 3
NIN_BALLOONUSERCLICK = 0x0400


class TrayIcon:
    """系统托盘图标（Windows）。

    独立线程 + 隐藏窗口 + 消息泵，不阻塞主线程。
    """

    def __init__(
        self,
        on_open_dashboard: Optional[Callable] = None,
        on_toggle_engine: Optional[Callable] = None,
        on_exit: Optional[Callable] = None,
    ):
        self._on_open_dashboard = on_open_dashboard
        self._on_toggle_engine = on_toggle_engine
        self._on_exit = on_exit
        self._hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()
        self._engine_running = False
        self._engine_state = "stopped"
        self._auto_start_enabled = _is_auto_start_enabled()

    def start(self):
        """启动托盘图标（后台线程）。"""
        if self._thread and self._thread.is_alive():
            return
        self._shutdown_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """停止托盘图标。"""
        self._shutdown_event.set()
        if self._hwnd:
            try:
                win32gui.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None
        if self._thread and self._thread.is_alive():
            # 避免在托盘线程自身调用 join（会抛 RuntimeError/死锁）；
            # 托盘线程退出循环后会自行清理
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=3)

    def set_engine_running(self, running: bool):
        """兼容旧调用方；新代码统一使用 set_engine_state。"""
        self.set_engine_state("running" if running else "stopped")

    def set_engine_state(self, state: str):
        """更新后台核心状态并同步托盘提示。"""
        self._engine_state = state
        self._engine_running = state == "running"
        self._update_tray_tip()

    def show_balloon(self, title: str, message: str, icon_type: int = NIIF_INFO):
        """显示气球通知。"""
        if not self._hwnd:
            return
        try:
            nid = (self._hwnd, ID_TRAY_ICON,
                   win32gui.NIF_INFO,
                   0, 0, "",
                   message, 2000, title, icon_type)
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception:
            pass

    def set_auto_start(self, enable: bool):
        self._auto_start_enabled = enable
        (_register_auto_start if enable else _unregister_auto_start)()

    def _run(self):
        hinst = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = "NotmyFaultTrayWindow"
        wc.lpfnWndProc = self._wndproc
        wc.hbrBackground = win32con.COLOR_WINDOW
        class_atom = win32gui.RegisterClass(wc)

        self._hwnd = win32gui.CreateWindow(
            class_atom, "NotmyFaultTray",
            win32con.WS_OVERLAPPEDWINDOW,
            0, 0, 0, 0, 0, 0, hinst, None,
        )

        self._add_icon()

        import ctypes
        msg = ctypes.wintypes.MSG()
        while not self._shutdown_event.is_set():
            while ctypes.windll.user32.PeekMessageW(
                ctypes.byref(msg), None, 0, 0, 1
            ):
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
            self._shutdown_event.wait(0.05)

        self._remove_icon()
        if self._hwnd:
            try:
                win32gui.DestroyWindow(self._hwnd)
            except Exception:
                pass

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int):
        if msg == WM_TASKBARCREATED:
            self._add_icon()
            return 0
        if msg == WM_TRAY_CALLBACK:
            return self._handle_tray_callback(hwnd, wparam, lparam)
        if msg == win32con.WM_COMMAND:
            return self._handle_menu_command(wparam)
        if msg == win32con.WM_DESTROY:
            self._remove_icon()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _handle_tray_callback(self, hwnd: int, wparam: int, lparam: int):
        if lparam == win32con.WM_LBUTTONUP:
            if self._on_open_dashboard:
                self._on_open_dashboard()
            return 0
        if lparam == win32con.WM_RBUTTONUP:
            self._show_context_menu(hwnd)
            return 0
        if lparam == NIN_BALLOONUSERCLICK:
            if self._on_open_dashboard:
                self._on_open_dashboard()
            return 0
        return 0

    def _handle_menu_command(self, wparam: int):
        cmd_id = win32gui.LOWORD(wparam)
        if cmd_id == MID_OPEN_DASHBOARD:
            if self._on_open_dashboard:
                self._on_open_dashboard()
        elif cmd_id == MID_TOGGLE_ENGINE:
            if self._on_toggle_engine:
                self._on_toggle_engine()
        elif cmd_id == MID_AUTO_START:
            self.set_auto_start(not self._auto_start_enabled)
        elif cmd_id == MID_EXIT:
            if self._on_exit:
                self._on_exit()
        return 0

    def _add_icon(self):
        if not self._hwnd:
            return
        try:
            icon = _load_icon()
            nid = (self._hwnd, ID_TRAY_ICON,
                   win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
                   WM_TRAY_CALLBACK, icon, "NotmyFault 引擎")
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        except Exception as e:
            print(f"[Tray] 添加图标失败: {e}")

    def _remove_icon(self):
        if not self._hwnd:
            return
        try:
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_DELETE,
                (self._hwnd, ID_TRAY_ICON, 0, 0, 0, ""),
            )
        except Exception:
            pass

    def _update_tray_tip(self):
        if not self._hwnd:
            return
        labels = {
            "starting": "NotmyFault - 正在启动自动化",
            "running": "NotmyFault - 自动化运行中",
            "stopping": "NotmyFault - 正在暂停自动化",
            "stopped": "NotmyFault - 后台在线，自动化已暂停",
        }
        tip = labels.get(self._engine_state, "NotmyFault")
        try:
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_MODIFY,
                (self._hwnd, ID_TRAY_ICON, win32gui.NIF_TIP, 0, 0, tip),
            )
        except Exception:
            pass

    def _show_context_menu(self, hwnd: int):
        menu = win32gui.CreatePopupMenu()

        win32gui.AppendMenu(menu, win32con.MF_STRING,
                            MID_OPEN_DASHBOARD, "打开控制面板")
        transient = self._engine_state in ("starting", "stopping")
        toggle_flags = win32con.MF_STRING
        if transient:
            toggle_flags |= win32con.MF_GRAYED
        toggle_label = {
            "starting": "正在启动自动化…",
            "running": "暂停自动化",
            "stopping": "正在暂停自动化…",
            "stopped": "启动自动化",
        }.get(self._engine_state, "启动自动化")
        win32gui.AppendMenu(
            menu,
            toggle_flags,
            MID_TOGGLE_ENGINE,
            toggle_label,
        )
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR,
                            MID_SEPARATOR_1, "")

        flags = win32con.MF_STRING
        if self._auto_start_enabled:
            flags |= win32con.MF_CHECKED
        win32gui.AppendMenu(menu, flags, MID_AUTO_START, "开机自启")

        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR,
                            MID_SEPARATOR_2, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING,
                            MID_EXIT, "退出")

        pos = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(hwnd)
        win32gui.TrackPopupMenu(
            menu, win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN,
            pos[0], pos[1], 0, hwnd, None,
        )
        win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)


# ================================================================
# 图标加载
# ================================================================

_ICON_CACHE = None


def _load_icon():
    global _ICON_CACHE
    if _ICON_CACHE is not None:
        return _ICON_CACHE
    if os.path.exists(ICON_PATH):
        try:
            _ICON_CACHE = win32gui.LoadImage(
                0, ICON_PATH,
                win32con.IMAGE_ICON,
                0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
            )
            return _ICON_CACHE
        except Exception:
            pass
    return 0


# ================================================================
# 开机自启
# ================================================================

AUTO_START_NAME = "NotmyFaultEngine"


def _register_auto_start():
    import winreg
    exe = sys.executable if getattr(sys, "frozen", False) else \
        os.path.abspath(os.path.join(PROJECT_ROOT, "NOTMYFAULT.pyw"))
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, AUTO_START_NAME, 0, winreg.REG_SZ, exe)
        return True
    except Exception:
        return False


def _unregister_auto_start():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, AUTO_START_NAME)
            except OSError:
                pass
        return True
    except Exception:
        return False


def _is_auto_start_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, AUTO_START_NAME)
            return True
    except Exception:
        return False
