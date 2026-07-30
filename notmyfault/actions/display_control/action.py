import ctypes
import math
import os
import subprocess
import time

HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_ON = -1
MONITOR_OFF = 2


def _brightness_level(params) -> int:
    """Return a validated integer brightness, including legacy action aliases."""
    action = params.get("action", "off")
    if action == "low_brightness":
        return 10
    if action == "high_brightness":
        return 90
    if action != "set_brightness":
        raise ValueError(f"不支持的亮度操作: {action}")

    raw_level = params.get("brightness", 50)
    if isinstance(raw_level, bool):
        raise ValueError("亮度必须是 0–100 的整数")
    try:
        numeric_level = float(raw_level)
    except (TypeError, ValueError) as exc:
        raise ValueError("亮度必须是 0–100 的整数") from exc
    if (
        not math.isfinite(numeric_level)
        or not numeric_level.is_integer()
        or not 0 <= numeric_level <= 100
    ):
        raise ValueError("亮度必须是 0–100 的整数")
    return int(numeric_level)


def validate_params(_action_info, params):
    action = params.get("action", "off")
    if action in ("set_brightness", "low_brightness", "high_brightness"):
        try:
            _brightness_level(params)
        except ValueError as exc:
            return [str(exc)]
    return []


def _set_wmi_brightness(level: int) -> int:
    """Set and verify brightness for panels exposed through root/WMI."""
    script = (
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
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=8,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail or f"PowerShell 退出码 {result.returncode}")

    try:
        levels = [int(value) for value in result.stdout.split(",") if value.strip()]
    except ValueError as exc:
        raise RuntimeError(f"无法解析 WMI 亮度回读值: {result.stdout!r}") from exc
    if not levels or not any(abs(value - level) <= 3 for value in levels):
        raise RuntimeError(f"WMI 命令已执行，但亮度回读为 {levels or '空'}")
    return len(levels)


def _set_ddc_brightness(level: int) -> int:
    """Set and verify brightness for DDC/CI-capable physical monitors."""
    from ctypes import wintypes

    class PhysicalMonitor(ctypes.Structure):
        _fields_ = [
            ("handle", wintypes.HANDLE),
            ("description", wintypes.WCHAR * 128),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dxva2 = ctypes.WinDLL("Dxva2", use_last_error=True)
    monitor_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        monitor_enum_proc,
        wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
        wintypes.HMONITOR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
    dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
        wintypes.HMONITOR,
        wintypes.DWORD,
        ctypes.POINTER(PhysicalMonitor),
    ]
    dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
    dxva2.GetMonitorBrightness.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    dxva2.GetMonitorBrightness.restype = wintypes.BOOL
    dxva2.SetMonitorBrightness.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dxva2.SetMonitorBrightness.restype = wintypes.BOOL
    dxva2.DestroyPhysicalMonitor.argtypes = [wintypes.HANDLE]
    dxva2.DestroyPhysicalMonitor.restype = wintypes.BOOL

    logical_monitors = []

    @monitor_enum_proc
    def collect_monitor(monitor, _hdc, _rect, _data):
        logical_monitors.append(monitor)
        return True

    if not user32.EnumDisplayMonitors(None, None, collect_monitor, 0):
        raise ctypes.WinError(ctypes.get_last_error())

    changed = 0
    errors = []
    for logical_monitor in logical_monitors:
        count = wintypes.DWORD()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
            logical_monitor, ctypes.byref(count)
        ):
            continue
        physical_monitors = (PhysicalMonitor * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(
            logical_monitor, count, physical_monitors
        ):
            continue
        try:
            for monitor in physical_monitors:
                minimum = wintypes.DWORD()
                current = wintypes.DWORD()
                maximum = wintypes.DWORD()
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

                # Some monitors apply DDC commands asynchronously. Read twice before
                # declaring success so a successful API return cannot become a false
                # positive in the rule log.
                verified = False
                for delay in (0.08, 0.2):
                    time.sleep(delay)
                    actual = wintypes.DWORD()
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
        raise RuntimeError("；".join(errors) or "未发现支持 DDC/CI 亮度控制的显示器")
    return changed


def _set_brightness(level: int):
    methods = []
    errors = []
    for name, setter in (("WMI", _set_wmi_brightness), ("DDC/CI", _set_ddc_brightness)):
        try:
            changed = setter(level)
            methods.append(f"{name} {changed} 台")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    if not methods:
        raise RuntimeError(
            "当前显示器不支持可验证的亮度控制（"
            + "；".join(errors)
            + "）。外接显示器请确认已在显示器菜单中启用 DDC/CI。"
        )
    return {"brightness": level, "methods": methods, "warnings": errors}


def run(action_info, params):
    action = params.get("action", "off")

    print(f"[Action:display_control] 执行: {action}")

    try:
        if os.name != "nt":
            from notmyfault.linux_support import desktop_environment

            if action in ("set_brightness", "low_brightness", "high_brightness"):
                brightness = _brightness_level(params)
                result = subprocess.run(
                    ["brightnessctl", "set", f"{brightness}%"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=5,
                )
            elif action in ("off", "on") and desktop_environment() == "gnome":
                result = subprocess.run(
                    [
                        "gdbus",
                        "call",
                        "--session",
                        "--dest",
                        "org.gnome.ScreenSaver",
                        "--object-path",
                        "/org/gnome/ScreenSaver",
                        "--method",
                        "org.gnome.ScreenSaver.SetActive",
                        "true" if action == "off" else "false",
                    ],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=5,
                )
            elif action in ("off", "on"):
                result = subprocess.run(
                    ["xset", "dpms", "force", action],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=5,
                )
            else:
                raise ValueError(f"不支持的显示器操作: {action}")
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "Linux 显示器命令失败")
            print(f"[Action:display_control] 操作完成: {action}")
            return {"action": action}

        user32 = ctypes.windll.user32
        if action == "off":
            user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF)
            print("[Action:display_control] 显示器已关闭")

        elif action == "on":
            user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_ON)
            print("[Action:display_control] 显示器已开启")

        elif action in ("set_brightness", "low_brightness", "high_brightness"):
            brightness = _brightness_level(params)
            result = _set_brightness(brightness)
            print(
                f"[Action:display_control] 亮度已设置为 {brightness}%（"
                + "，".join(result["methods"])
                + "）"
            )
            return result

        else:
            raise ValueError(f"不支持的显示器操作: {action}")

    except Exception as e:
        print(f"[Action:display_control] 操作失败: {e}")
        raise
