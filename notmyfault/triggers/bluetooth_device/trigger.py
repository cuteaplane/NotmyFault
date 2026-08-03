r"""蓝牙设备检测触发器
Get-PnpDevice 的 Status/Present 在设备断开后仍可能是 OK/True，连接状态读取设备容器属性 {83DA6326...} 15
Windows 路径仅处理 BTHENUM 外设，并按 InstanceId 基础段合并同一物理设备
"""

import subprocess
import time
import os

# 设备容器属性：指示蓝牙设备是否真正已连接，值为 True 或 False
_DEVPKEY_CONNECTION_STATE = "{83DA6326-97A6-4088-9453-A1923F573B29} 15"


def _get_connected_devices():
    r"""返回确认已连接的蓝牙外设名集合以及错误列表"""
    if os.name != "nt":
        return _get_linux_connected_devices()

    ps = f"""
$all = Get-PnpDevice -Class Bluetooth | Where-Object {{ $_.Present }}
foreach ($dev in $all) {{
    $iid = $dev.InstanceId
    # InstanceId 以 BTHENUM 开头的条目进入后续处理
    if ($iid -notlike 'BTHENUM\\*') {{
        continue
    }}
    $name = $dev.FriendlyName
    # 读取设备容器连接状态属性
    $conn = Get-PnpDeviceProperty -InstanceId $iid -KeyName '{_DEVPKEY_CONNECTION_STATE}' -ErrorAction SilentlyContinue
    $connected = 'no'
    if ($conn -and $conn.Data -eq $true) {{
        $connected = 'yes'
    }}
    # 输出名称、连接状态和 InstanceId 基础段
    $baseIid = ($iid -split '\\\\')[2]
    Write-Host ("{{0}}|{{1}}|{{2}}" -f $name, $connected, $baseIid)
}}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return set(), ["PowerShell 超时"]
    except Exception as e:
        return set(), [f"PowerShell 启动失败: {e}"]

    errors = []
    if result.returncode != 0:
        err_msg = result.stderr.strip()[:300] if result.stderr else "无错误输出"
        errors.append(f"PowerShell 返回码={result.returncode}: {err_msg}")

    # 同一物理设备会产生主设备和多个服务 GUID 条目，使用 InstanceId 第二段作为去重键
    seen_base_ids = set()
    devices = set()

    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        parts = line.rsplit("|", 2)
        if len(parts) < 3:
            continue

        name = parts[0].strip()
        connected = parts[1].strip()
        base_iid = parts[2].strip()

        if connected != "yes":
            continue

        # 基础段已出现时保留首次条目
        if base_iid in seen_base_ids:
            continue
        seen_base_ids.add(base_iid)

        # 去掉协议传输后缀，得到基础设备名
        base_name = _strip_bluetooth_suffix(name)
        devices.add(base_name)

    return devices, errors


def _get_linux_connected_devices():
    try:
        result = subprocess.run(
            ["bluetoothctl", "devices", "Connected"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except Exception as error:
        return set(), [f"bluetoothctl 启动失败: {error}"]
    if result.returncode != 0:
        return set(), [result.stderr.strip() or "bluetoothctl 查询失败"]
    devices = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 3 and parts[0] == "Device":
            devices.add(_strip_bluetooth_suffix(parts[2]))
    return devices, []


def _strip_bluetooth_suffix(name: str) -> str:
    """去掉蓝牙协议或传输后缀，保留基础设备名"""
    suffixes = [
        " avrcp 传输", " avrcp transport",
        " hands-free ag", " hands-free",
        " stereo",
        " a2dp", " hsp", " hfp", " ble",
    ]
    name_lower = name.lower()
    for suffix in suffixes:
        if name_lower.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _device_set_hash(devices: set) -> int:
    """按排序后的设备名计算集合哈希"""
    return hash(tuple(sorted(devices)))


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "bluetooth_device")
    target = config.get("device_name", "").strip()
    target_state = config.get("state", "connected")
    if target_state not in ("connected", "disconnected"):
        raise ValueError(
            f"无效的蓝牙设备状态: {target_state!r}（可选: connected/disconnected）"
        )
    print(f"[Trigger:{trigger_id}] 蓝牙设备监控已启动")

    last_devices, errors = _get_connected_devices()
    if errors:
        for e in errors:
            print(f"[Trigger:{trigger_id}] [!!] {e}")
    print(
        f"[Trigger:{trigger_id}] 初始已连接设备 "
        f"({len(last_devices)} 个): {last_devices or '(无)'}"
    )

    last_hash = _device_set_hash(last_devices)
    scan_count = 0

    while not shutdown_event.is_set():
        try:
            shutdown_event.wait(3)
            scan_count += 1

            current_devices, errors = _get_connected_devices()

            if errors:
                for e in errors:
                    print(f"[Trigger:{trigger_id}] [!!] 扫描 #{scan_count}: {e}")
                # 查询失败时保留上一次设备集合，等待下一次成功查询
                continue

            current_hash = _device_set_hash(current_devices)
            if current_hash == last_hash:
                continue

            new_devices = current_devices - last_devices
            lost_devices = last_devices - current_devices

            if new_devices or lost_devices:
                print(
                    f"[Trigger:{trigger_id}] 检测到变化 "
                    f"(扫描 #{scan_count}): "
                    f"+{len(new_devices)} 连接 / -{len(lost_devices)} 断开"
                )
                if new_devices:
                    print(f"  + 新连接: {new_devices}")
                if lost_devices:
                    print(f"  - 已断开: {lost_devices}")

                devices = new_devices if target_state == "connected" else lost_devices
                for device in devices:
                    if not target or target.lower() in device.lower():
                        print(f"[Trigger:{trigger_id}] [OK] 设备状态变化: {device}")
                        # device_name 用用户配置值供规则匹配，actual_device 保留实际名称
                        emit_event({
                            "device_name": target,
                            "actual_device": device,
                            "state": target_state,
                        })

            last_devices = current_devices
            last_hash = current_hash

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 扫描异常 #{scan_count}: {e}")
            # 异常时保留上一次设备集合，等待下一次成功查询
