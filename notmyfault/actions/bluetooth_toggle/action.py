import subprocess


def _radio_powershell(state: str):
    """通过 WinRT Radio API 设置蓝牙状态（state: On / Off）"""
    ps = f'''
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]
$selector = [Windows.Devices.Radios.Radio]::GetDeviceSelector()
$devices = [Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector).GetAwaiter().GetResult()
foreach ($info in $devices) {{
    $radio = [Windows.Devices.Radios.Radio]::FromIdAsync($info.Id).GetAwaiter().GetResult()
    if ($radio -and $radio.Kind -eq [Windows.Devices.Radios.RadioKind]::Bluetooth) {{
        $radio.SetStateAsync([Windows.Devices.Radios.RadioState]::{state}).GetAwaiter().GetResult()
        Write-Host "蓝牙已切换至 {state}"
    }}
}}
'''
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
        print(f"[Action:bluetooth_toggle] {result.stdout.strip()}")
        if result.stderr:
            print(f"[Action:bluetooth_toggle] 警告: {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        print("[Action:bluetooth_toggle] 操作超时（蓝牙 API 无响应）")
    except Exception as e:
        print(f"[Action:bluetooth_toggle] 执行失败: {e}")


def _get_current_state() -> str:
    """查询当前蓝牙开关状态，返回 'On' / 'Off' / 'Unknown'"""
    ps = '''
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]
$selector = [Windows.Devices.Radios.Radio]::GetDeviceSelector()
$devices = [Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector).GetAwaiter().GetResult()
foreach ($info in $devices) {
    $radio = [Windows.Devices.Radios.Radio]::FromIdAsync($info.Id).GetAwaiter().GetResult()
    if ($radio -and $radio.Kind -eq [Windows.Devices.Radios.RadioKind]::Bluetooth) {
        $radio.State
    }
}
'''
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.strip()
        if "On" in output:
            return "On"
        elif "Off" in output:
            return "Off"
        return "Unknown"
    except Exception:
        return "Unknown"


def run(action_info, params):
    action = params.get("action", "toggle")
    print(f"[Action:bluetooth_toggle] 操作: {action}")

    if action == "on":
        _radio_powershell("On")
    elif action == "off":
        _radio_powershell("Off")
    else:  # toggle
        current = _get_current_state()
        print(f"[Action:bluetooth_toggle] 当前状态: {current}")
        if current == "On":
            _radio_powershell("Off")
        else:
            _radio_powershell("On")
