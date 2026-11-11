from __future__ import annotations

import ctypes
import subprocess
import uuid

from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform._run import CommandRunner, default_runner
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError
from notmyfault.platform.windows import _win32


# Windows 电源计划 GUID 在不同语言系统上保持一致。
_PLANS = {
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "high_performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "power_saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
}


class PowerBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def set_profile(self, plan: str, custom_guid: str = "") -> str:
        if plan == "custom":
            guid = str(custom_guid or "").strip()
            if not guid:
                raise BackendInvalidError("自定义电源计划必须提供 GUID")
            try:
                guid = str(uuid.UUID(guid))
            except ValueError as error:
                raise BackendInvalidError("自定义电源计划必须提供有效的 GUID") from error
        elif plan in _PLANS:
            guid = _PLANS[plan]
        else:
            raise BackendInvalidError(
                f"未知电源计划: {plan!r}"
                "（可选: balanced/high_performance/power_saver/custom）"
            )
        result = self._runner.run(
            ["powercfg", "/setactive", guid],
            timeout=30,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-200:]
            raise BackendFailedError(
                f"切换电源计划失败 (code={result.returncode}): {detail}"
            )
        return guid

    def battery(self) -> dict:
        status = _win32.SYSTEM_POWER_STATUS()
        with NATIVE_LOCK:
            if not _win32.kernel32().GetSystemPowerStatus(ctypes.byref(status)):
                raise BackendFailedError("读取系统电源状态失败")
        if status.BatteryFlag == 128 or status.ACLineStatus == 255:
            return {"on_battery": False, "percent": None}
        if status.ACLineStatus not in (0, 1):
            raise BackendFailedError("系统交流电源状态无效")
        percent = status.BatteryLifePercent
        if percent != 255 and percent > 100:
            raise BackendFailedError("系统电池电量无效")
        return {
            "on_battery": status.ACLineStatus == 0,
            "percent": None if percent == 255 else int(percent),
        }

    def resume_monitor(self):
        return SleepResumeBackend()

    def system(self, action: str, force: bool = False) -> None:
        if action not in ("shutdown", "restart", "logoff", "hibernate", "sleep"):
            raise BackendInvalidError(f"不支持的系统电源操作: {action!r}")
        if not isinstance(force, bool):
            raise BackendInvalidError("force 必须为布尔值")
        with NATIVE_LOCK:
            if action != "logoff":
                self._enable_shutdown_privilege()
            if action in ("hibernate", "sleep"):
                ok = _win32.powrprof().SetSuspendState(action == "hibernate", False, False)
            else:
                flags = {
                    "shutdown": _win32.EWX_SHUTDOWN | _win32.EWX_POWEROFF,
                    "restart": _win32.EWX_REBOOT,
                    "logoff": _win32.EWX_LOGOFF,
                }[action]
                if force:
                    flags |= _win32.EWX_FORCE
                ok = _win32.user32().ExitWindowsEx(flags, 0)
        if not ok:
            raise BackendFailedError(f"系统未能执行 {action}（可能被其他程序阻止或权限不足）")

    def _enable_shutdown_privilege(self) -> None:
        advapi = _win32.advapi32()
        kernel = _win32.kernel32()
        token = ctypes.c_void_p()
        if not advapi.OpenProcessToken(
            kernel.GetCurrentProcess(), _win32.TOKEN_ADJUST_PRIVILEGES | _win32.TOKEN_QUERY,
            ctypes.byref(token),
        ):
            raise BackendFailedError("无法打开当前进程的关机权限令牌")
        try:
            luid = _win32.LUID()
            if not advapi.LookupPrivilegeValueW(None, _win32.SE_SHUTDOWN_NAME, ctypes.byref(luid)):
                raise BackendFailedError("无法查询 Windows 关机权限")
            privileges = _win32.TOKEN_PRIVILEGES(1, luid, _win32.SE_PRIVILEGE_ENABLED)
            kernel.SetLastError(0)
            if not advapi.AdjustTokenPrivileges(
                token, False, ctypes.byref(privileges), 0, None, None,
            ) or kernel.GetLastError():
                raise BackendFailedError("无法启用 Windows 关机权限")
        finally:
            kernel.CloseHandle(token)


class SleepResumeBackend:
    def __init__(self) -> None:
        self._hwnd = None
        self._class_name = None
        self._hinstance = None
        self._callback = None
        self._resumed = False

    def start(self) -> None:
        with NATIVE_LOCK:
            if self._hwnd is not None:
                return
            user = _win32.user32()
            kernel = _win32.kernel32()

            def wnd_proc(hwnd, message, wparam, lparam):
                with NATIVE_LOCK:
                    if message == _win32.WM_POWERBROADCAST:
                        if wparam in (_win32.PBT_APMRESUMEAUTOMATIC, _win32.PBT_APMRESUMESUSPEND):
                            self._resumed = True
                        return 0
                    return user.DefWindowProcW(hwnd, message, wparam, lparam)

            # WNDPROC 回调必须保持引用存活。
            self._callback = _win32.POWER_WNDPROC(wnd_proc)
            class_name = f"NotmyFaultPowerState_{kernel.GetCurrentThreadId()}"
            window_class = _win32.POWER_WNDCLASSW()
            window_class.lpfnWndProc = self._callback
            window_class.hInstance = kernel.GetModuleHandleW(None)
            window_class.lpszClassName = class_name
            if not user.RegisterClassW(ctypes.byref(window_class)):
                self._callback = None
                raise BackendFailedError("无法注册睡眠恢复事件监听窗口类")
            self._class_name = class_name
            self._hinstance = window_class.hInstance
            try:
                self._hwnd = user.CreateWindowExW(
                    0, class_name, class_name, 0, 0, 0, 0, 0,
                    None, None, self._hinstance, None,
                )
                if not self._hwnd:
                    raise BackendFailedError("无法创建睡眠恢复事件监听窗口")
            except Exception:
                self.stop()
                raise

    def consume_resume(self) -> bool:
        with NATIVE_LOCK:
            if self._hwnd is None:
                return False
            user = _win32.user32()
            message = _win32.POWER_MSG()
            while user.PeekMessageW(ctypes.byref(message), self._hwnd, 0, 0, 1):
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
            resumed = self._resumed
            self._resumed = False
            return resumed

    def stop(self) -> None:
        with NATIVE_LOCK:
            user = _win32.user32()
            try:
                if self._hwnd:
                    user.DestroyWindow(self._hwnd)
            finally:
                self._hwnd = None
                try:
                    if self._class_name:
                        user.UnregisterClassW(self._class_name, self._hinstance)
                finally:
                    self._class_name = None
                    self._hinstance = None
                    self._callback = None
                    self._resumed = False
