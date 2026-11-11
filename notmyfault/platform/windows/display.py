from __future__ import annotations

import ctypes
import subprocess
import time

from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform._run import CommandRunner, default_runner, powershell_path
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError
from notmyfault.platform.windows import _win32


class DisplayBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def set_brightness(self, percent: int) -> dict:
        if type(percent) is not int or not 0 <= percent <= 100:
            raise BackendInvalidError("亮度必须是 0–100 的整数")
        methods = []
        errors = []
        for name, setter in (("WMI", self._set_wmi_brightness), ("DDC/CI", self._set_ddc_brightness)):
            try:
                changed = setter(percent)
                methods.append(f"{name} {changed} 台")
            except Exception as error:
                errors.append(f"{name}: {error}")
        if not methods:
            raise BackendFailedError(
                "当前显示器不支持可验证的亮度控制（"
                + "；".join(errors)
                + "）。外接显示器请确认已在显示器菜单中启用 DDC/CI。"
            )
        return {"brightness": percent, "methods": methods, "warnings": errors}

    def set_power(self, action: str) -> dict:
        if action not in ("on", "off"):
            raise BackendInvalidError(f"不支持的显示器电源操作: {action}")
        with NATIVE_LOCK:
            # 接收广播的电源触发器也需要这把锁才能处理窗口消息。
            sent = _win32.user32().SendNotifyMessageW(
                0xFFFF, 0x0112, 0xF170, 2 if action == "off" else -1,
            )
            if not sent:
                raise BackendFailedError("发送显示器电源命令失败")
        return {"methods": ["SendNotifyMessageW"], "warnings": []}

    def _set_wmi_brightness(self, level: int) -> int:
        script = (
            "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false);"
            "$ErrorActionPreference='Stop';"
            "$methods=@(Get-CimInstance -Namespace root/WMI "
            "-ClassName WmiMonitorBrightnessMethods -ErrorAction Stop);"
            "if($methods.Count -eq 0){throw '未发现支持 WMI 亮度控制的显示器'};"
            f"$brightnessArgs=@{{Timeout=[uint32]1;Brightness=[byte]{level}}};"
            "$methods|ForEach-Object{"
            "Invoke-CimMethod -InputObject $_ -MethodName WmiSetBrightness "
            "-Arguments $brightnessArgs -ErrorAction Stop|Out-Null};"
            "Start-Sleep -Milliseconds 150;"
            "$levels=@(Get-CimInstance -Namespace root/WMI "
            "-ClassName WmiMonitorBrightness -ErrorAction Stop|"
            "ForEach-Object{$_.CurrentBrightness});"
            "if($levels.Count -eq 0){throw '设置后无法读取显示器亮度'};"
            "[Console]::Out.Write(($levels -join ','))"
        )
        result = self._runner.run(
            [powershell_path(), "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=8,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise BackendFailedError(detail or f"PowerShell 退出码 {result.returncode}")
        try:
            levels = [int(value) for value in result.stdout.split(",") if value.strip()]
        except ValueError as error:
            raise BackendFailedError(f"无法解析 WMI 亮度回读值: {result.stdout!r}") from error
        if not levels or not any(abs(value - level) <= 3 for value in levels):
            raise BackendFailedError(f"WMI 命令已执行，但亮度回读为 {levels or '空'}")
        return len(levels)

    def _set_ddc_brightness(self, level: int) -> int:
        with NATIVE_LOCK:
            return self._set_physical_brightness(level)

    def _set_physical_brightness(self, level: int) -> int:
        user32 = _win32.user32()
        dxva2 = _win32.dxva2()
        logical_monitors = []

        @_win32.MONITORENUMPROC
        def collect_monitor(monitor, _hdc, _rect, _data):
            logical_monitors.append(monitor)
            return True

        if not user32.EnumDisplayMonitors(None, None, collect_monitor, 0):
            raise BackendFailedError("EnumDisplayMonitors 失败")
        changed = 0
        errors = []
        for logical_monitor in logical_monitors:
            count = _win32.DWORD()
            if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                logical_monitor, ctypes.byref(count)
            ):
                continue
            physical_monitors = (_win32.PhysicalMonitor * count.value)()
            if not dxva2.GetPhysicalMonitorsFromHMONITOR(
                logical_monitor, count, physical_monitors
            ):
                continue
            try:
                for monitor in physical_monitors:
                    minimum = _win32.DWORD()
                    current = _win32.DWORD()
                    maximum = _win32.DWORD()
                    if not dxva2.GetMonitorBrightness(
                        monitor.handle,
                        ctypes.byref(minimum),
                        ctypes.byref(current),
                        ctypes.byref(maximum),
                    ):
                        errors.append(f"{monitor.description or '显示器'} 不支持 DDC/CI 亮度读取")
                        continue
                    span = maximum.value - minimum.value
                    target = minimum.value + round(span * level / 100)
                    if not dxva2.SetMonitorBrightness(monitor.handle, target):
                        errors.append(f"{monitor.description or '显示器'} 拒绝 DDC/CI 亮度设置")
                        continue
                    # 部分显示器异步应用 DDC 命令，需要稍后回读。
                    verified = False
                    for delay in (0.08, 0.2):
                        time.sleep(delay)
                        actual = _win32.DWORD()
                        if dxva2.GetMonitorBrightness(
                            monitor.handle,
                            ctypes.byref(minimum),
                            ctypes.byref(actual),
                            ctypes.byref(maximum),
                        ) and abs(actual.value - target) <= max(1, round(span * 0.03)):
                            verified = True
                            break
                    if verified:
                        changed += 1
                    else:
                        errors.append(f"{monitor.description or '显示器'} 设置后回读值未变化")
            finally:
                for monitor in physical_monitors:
                    dxva2.DestroyPhysicalMonitor(monitor.handle)
        if changed == 0:
            raise BackendFailedError("；".join(errors) or "未发现支持 DDC/CI 亮度控制的显示器")
        return changed
