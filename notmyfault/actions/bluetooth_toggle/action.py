import subprocess

RADIO_PS = r'''
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]
$access = [Windows.Devices.Radios.Radio]::RequestAccessAsync().GetAwaiter().GetResult()
if ($access -ne [Windows.Devices.Radios.RadioAccessStatus]::Allowed) {
    Write-Host "!!ACCESS_DENIED"
    exit 1
}
$selector = [Windows.Devices.Radios.Radio]::GetDeviceSelector()
$devices = [Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector).GetAwaiter().GetResult()
$found = $false
foreach ($info in $devices) {
    $radio = [Windows.Devices.Radios.Radio]::FromIdAsync($info.Id).GetAwaiter().GetResult()
    if ($radio -and $radio.Kind -eq [Windows.Devices.Radios.RadioKind]::Bluetooth) {
        $found = $true
        Write-Host ("STATE:" + $radio.State)
        if ("__STATE__" -ne "query") {
            $radio.SetStateAsync([Windows.Devices.Radios.RadioState]::__STATE__).GetAwaiter().GetResult()
            Write-Host ("RESULT:" + [Windows.Devices.Radios.RadioState]::__STATE__)
        }
    }
}
if (-not $found) {
    Write-Host "!!NO_RADIO"
}
'''


def _run_ps(state: str) -> dict:
    ps = RADIO_PS.replace("__STATE__", state)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
        return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip(), "rc": result.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "超时", "rc": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "rc": -1}


def _toggle_via_pnp():
    """降级方案：通过 PnP 设备开关蓝牙（部分系统可用）"""
    ps = '''
$adapter = Get-PnpDevice -Class Bluetooth | Where-Object { $_.FriendlyName -match "adapter|适配" -or $_.Status -eq "Unknown" } | Select-Object -First 1
if (-not $adapter) {
    $adapter = Get-PnpDevice -Class Bluetooth | Select-Object -First 1
}
if ($adapter.Status -eq "OK") {
    Disable-PnpDevice -InstanceId $adapter.InstanceId -Confirm:$false
    Write-Host "蓝牙已禁用 (PnP)"
} else {
    Enable-PnpDevice -InstanceId $adapter.InstanceId -Confirm:$false
    Write-Host "蓝牙已启用 (PnP)"
}
'''
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
        print(f"[Action:bluetooth_toggle] PnP: {result.stdout.strip()}")
        if result.stderr:
            print(f"[Action:bluetooth_toggle] PnP 警告: {result.stderr.strip()}")
    except Exception as e:
        print(f"[Action:bluetooth_toggle] PnP 降级方案也失败了: {e}")


def run(action_info, params):
    action = params.get("action", "toggle")
    print(f"[Action:bluetooth_toggle] 操作: {action}")

    if action == "query":
        r = _run_ps("query")
        print(f"[Action:bluetooth_toggle] {r['stdout']}")
        if r["stderr"]:
            print(f"[Action:bluetooth_toggle] 错误: {r['stderr']}")
        return

    target = None
    if action == "on":
        target = "On"
    elif action == "off":
        target = "Off"
    else:
        r = _run_ps("query")
        stdout = r["stdout"]
        print(f"[Action:bluetooth_toggle] 当前状态: {stdout}")

        if "!!NO_RADIO" in stdout:
            print("[Action:bluetooth_toggle] 未检测到蓝牙适配器，尝试 PnP 降级方案")
            _toggle_via_pnp()
            return
        if "!!ACCESS_DENIED" in stdout:
            print("[Action:bluetooth_toggle] 无权限访问蓝牙 Radio，尝试 PnP 降级方案")
            _toggle_via_pnp()
            return
        if "STATE:On" in stdout:
            target = "Off"
        elif "STATE:Off" in stdout:
            target = "On"
        else:
            print("[Action:bluetooth_toggle] 无法确定当前状态，尝试 PnP 降级方案")
            _toggle_via_pnp()
            return

    r = _run_ps(target)
    stdout = r["stdout"]
    if stdout:
        print(f"[Action:bluetooth_toggle] {stdout}")
    if r["stderr"]:
        print(f"[Action:bluetooth_toggle] 警告: {r['stderr']}")

    if "!!ACCESS_DENIED" in stdout or "!!NO_RADIO" in stdout or r["rc"] != 0:
        print(f"[Action:bluetooth_toggle] Radio API 失败 (rc={r['rc']})，尝试 PnP 降级")
        _toggle_via_pnp()
