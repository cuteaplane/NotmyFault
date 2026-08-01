"""Windows 蓝牙无线电开关。

Windows 并没有一个在所有硬件、驱动和系统版本上都可靠的“蓝牙总开关”。
优先使用 WinRT Radio API；它被策略或驱动拒绝时，才使用需要 UAC 的 PnP
适配器回退方案。两条路径都要返回可验证的结果，不能只打印“成功”。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any

from notmyfault.security.sudo import run_as_admin


_RADIO_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]
$access = [Windows.Devices.Radios.Radio]::RequestAccessAsync().GetAwaiter().GetResult()
if ($access -ne [Windows.Devices.Radios.RadioAccessStatus]::Allowed) {
    throw "WinRT Radio API access denied: $access"
}
$radios = @([Windows.Devices.Radios.Radio]::GetRadiosAsync().GetAwaiter().GetResult() |
    Where-Object { $_.Kind -eq [Windows.Devices.Radios.RadioKind]::Bluetooth })
if ($radios.Count -eq 0) { throw 'No Bluetooth radio found through WinRT' }
if ('__STATE__' -ne 'query') {
    $wanted = [Windows.Devices.Radios.RadioState]::__STATE__
    foreach ($radio in $radios) {
        $result = $radio.SetStateAsync($wanted).GetAwaiter().GetResult()
        if ($result -ne [Windows.Devices.Radios.RadioAccessStatus]::Allowed) {
            throw "Radio state change denied: $result"
        }
    }
}
[pscustomobject]@{
    method = 'winrt_radio'
    radios = @($radios | ForEach-Object {
        [pscustomobject]@{ name = $_.Name; state = $_.State.ToString() }
    })
} | ConvertTo-Json -Compress -Depth 4
'''


def _ps_literal(value: str) -> str:
    """生成 PowerShell 单引号字面量（临时结果路径也不能裸拼）。"""
    return "'" + value.replace("'", "''") + "'"


def _pnp_control_script(action: str, result_path: str) -> str:
    requested = _ps_literal(action)
    destination = _ps_literal(result_path)
    return rf'''
$ErrorActionPreference = 'Stop'
$adapters = @(
    Get-PnpDevice -Class Bluetooth -ErrorAction Stop |
    Where-Object {{
        $_.InstanceId -match '^(USB|PCI|BTH)\\' -and
        $_.InstanceId -notmatch '^BTHENUM\\'
    }}
)
if ($adapters.Count -eq 0) {{ throw 'No physical Bluetooth adapter found through PnP' }}
$before = @($adapters | ForEach-Object {{
    [pscustomobject]@{{ instance_id=$_.InstanceId; name=$_.FriendlyName; status=$_.Status; problem=$_.Problem }}
}})
$beforeStates = @($before | ForEach-Object {{ $_.status }})
$current = if (($beforeStates | Where-Object {{ $_ -eq 'OK' }}).Count -eq $beforeStates.Count) {{ 'on' }} else {{ 'off' }}
$target = if ({requested} -eq 'toggle') {{ if ($current -eq 'on') {{ 'off' }} else {{ 'on' }} }} else {{ {requested} }}
if ($target -ne 'query') {{
    $command = if ($target -eq 'on') {{ 'Enable-PnpDevice' }} else {{ 'Disable-PnpDevice' }}
    foreach ($adapter in $adapters) {{
        & $command -InstanceId $adapter.InstanceId -Confirm:$false -ErrorAction Stop
    }}
}}
$afterAdapters = @(
    Get-PnpDevice -Class Bluetooth -ErrorAction Stop |
    Where-Object {{
        $_.InstanceId -match '^(USB|PCI|BTH)\\' -and
        $_.InstanceId -notmatch '^BTHENUM\\'
    }}
)
$after = @($afterAdapters | ForEach-Object {{
    [pscustomobject]@{{ instance_id=$_.InstanceId; name=$_.FriendlyName; status=$_.Status; problem=$_.Problem }}
}})
$afterStates = @($after | ForEach-Object {{ $_.status }})
$actual = if (($afterStates | Where-Object {{ $_ -eq 'OK' }}).Count -eq $afterStates.Count) {{ 'on' }} else {{ 'off' }}
if ($target -ne 'query' -and $actual -ne $target) {{
    throw "PnP requested Bluetooth $target, but adapters are still $actual"
}}
[pscustomobject]@{{
    method = 'pnp_adapter'
    state = if ($target -eq 'query') {{ $current }} else {{ $actual }}
    before = $before
    adapters = $after
}} | ConvertTo-Json -Compress -Depth 5 | Set-Content -LiteralPath {destination} -Encoding utf8
'''


def _run_powershell(script: str) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
        )
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "PowerShell timeout after 20 seconds"
    except Exception as exc:
        return -1, "", str(exc)


def _decode_json(output: str) -> Any:
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PowerShell returned invalid JSON: {output[:200]}") from exc


def _radio_state(payload: Any) -> str:
    radios = payload.get("radios", []) if isinstance(payload, dict) else []
    states = {str(item.get("state", "")).lower() for item in radios if isinstance(item, dict)}
    if not states:
        raise RuntimeError("WinRT did not return a Bluetooth radio state")
    if states == {"on"}:
        return "on"
    if states == {"off"}:
        return "off"
    return "mixed"


def _run_radio(target: str) -> dict[str, Any]:
    state = {"on": "On", "off": "Off", "query": "query"}[target]
    rc, stdout, stderr = _run_powershell(_RADIO_SCRIPT.replace("__STATE__", state))
    if rc != 0:
        raise RuntimeError(stderr or stdout or "WinRT Radio API failed")
    result = _decode_json(stdout)
    if not isinstance(result, dict):
        raise RuntimeError("WinRT returned an unexpected result")
    return result


def _run_pnp(action: str) -> dict[str, Any]:
    """通过一个已提权 PowerShell 进程完成 PnP 查询、操作和验证。

    部分 Windows 策略连 ``Get-PnpDevice`` 查询也要求管理员权限；不能在 UAC
    之前先探测。管理员进程把 JSON 写入当前用户创建的临时文件，主进程再读取。
    """
    fd, result_path = tempfile.mkstemp(prefix="notmyfault-bluetooth-", suffix=".json")
    os.close(fd)
    try:
        launched = run_as_admin(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", _pnp_control_script(action, result_path),
            ],
            timeout=35,
        )
        if launched.returncode != 0:
            raise RuntimeError(
                f"UAC launch failed: {launched.stderr.strip() or launched.stdout.strip()}"
            )
        try:
            with open(result_path, encoding="utf-8-sig") as result_file:
                result = json.load(result_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "PnP command did not return a verified result; UAC may have been cancelled or denied"
            ) from exc
        if not isinstance(result, dict) or result.get("method") != "pnp_adapter":
            raise RuntimeError("PnP returned an unexpected result")
        return result
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass


def run(_action_info, params):
    action = str(params.get("action", "toggle")).lower()
    if action not in {"toggle", "on", "off", "query"}:
        raise ValueError(f"Unsupported Bluetooth action: {action}")

    if os.name != "nt":
        return _run_linux_bluetooth(action)

    radio_error = ""
    try:
        radio = _run_radio("query")
        current = _radio_state(radio)
        target = {"toggle": "off" if current == "on" else "on"}.get(action, action)
        if target == "query":
            return {"ok": True, "method": "winrt_radio", "state": current, "radios": radio["radios"]}
        changed = _run_radio(target)
        actual = _radio_state(changed)
        if actual != target:
            raise RuntimeError(f"WinRT requested {target}, but reported {actual}")
        return {"ok": True, "method": "winrt_radio", "state": actual, "radios": changed["radios"]}
    except Exception as exc:
        radio_error = str(exc)

    # WinRT 在某些 OEM 驱动、远程桌面会话或企业策略下必定拒绝访问。此时用
    # PnP 适配器回退；它需要 UAC，且只挑物理适配器，绝不误禁用耳机服务条目。
    result = _run_pnp(action)
    result.update({"ok": True, "winrt_error": radio_error})
    return result


def _run_linux_bluetooth(action: str) -> dict[str, Any]:
    def query_state() -> str:
        result = subprocess.run(
            ["bluetoothctl", "show"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "bluetoothctl show 失败")
        return "on" if "Powered: yes" in result.stdout else "off"

    current = query_state()
    target = ("off" if current == "on" else "on") if action == "toggle" else action
    if target != "query":
        result = subprocess.run(
            ["bluetoothctl", "power", target],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"蓝牙切换到 {target} 失败")
    actual = query_state()
    expected = current if target == "query" else target
    if actual != expected:
        raise RuntimeError(f"请求蓝牙 {expected}，实际状态为 {actual}")
    return {"ok": True, "method": "bluetoothctl", "state": actual}
