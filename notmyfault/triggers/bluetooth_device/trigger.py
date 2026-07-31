r"""
蓝牙设备检测触发器
------------------
通过 Windows PnP 设备属性检测蓝牙外设的真正连接状态。

原理：
  配对 ≠ 连接。Get-PnpDevice 的 Status/Present 在设备断开后仍为 OK/True。
  真正可靠的连接状态由设备容器属性 {83DA6326...} 15 提供 ——
  这是一个 boolean，表示设备当前是否建立了有效的蓝牙连接。

来源：https://superuser.com/revisions/dabcdfca-44ec-4281-a9b9-9cccd8327f05/view-source
"""

import subprocess
import time
import os

# 设备容器属性：指示蓝牙设备是否真正已连接（True/False）
_DEVPKEY_CONNECTION_STATE = "{83DA6326-97A6-4088-9453-A1923F573B29} 15"


def _get_connected_devices():
    r"""返回当前**真正已连接**的蓝牙外设名集合。

    只返回 InstanceId 前缀为 BTHENUM 的外设（排除适配器、协议栈等系统设备）。
    每个设备通过 {83DA6326...} 15 属性确认真实连接状态。

    返回 (devices: set[str], errors: list[str])
    """
    if os.name != "nt":
        return _get_linux_connected_devices()

    ps = f"""
$all = Get-PnpDevice -Class Bluetooth | Where-Object {{ $_.Present }}
foreach ($dev in $all) {{
    $iid = $dev.InstanceId
    # 只处理蓝牙外设（BTHENUM 前缀），跳过系统设备（BTH_MS, USB_VID 前缀）
    if ($iid -notlike 'BTHENUM\\*') {{
        continue
    }}
    $name = $dev.FriendlyName
    # 查询真正的连接状态
    $conn = Get-PnpDeviceProperty -InstanceId $iid -KeyName '{_DEVPKEY_CONNECTION_STATE}' -ErrorAction SilentlyContinue
    $connected = 'no'
    if ($conn -and $conn.Data -eq $true) {{
        $connected = 'yes'
    }}
    # 输出：名称|已连接|InstanceId前缀（去重key）
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

    # 去重用：同一物理设备可能产生多条 BTHENUM 条目
    # （主设备 + 多个服务 GUID），用 InstanceId 的第二段去重
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

        # 去重：同一物理设备只保留一个条目
        if base_iid in seen_base_ids:
            continue
        seen_base_ids.add(base_iid)

        # 基础设备名（去掉协议传输后缀）
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
    """剥离蓝牙协议/传输后缀，保留基础设备名。

    "OPPO Enco Free4 Avrcp 传输" → "OPPO Enco Free4"
    """
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
    """计算设备集合的稳定哈希。"""
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
                # 发生错误时不更新 last_devices，防止误报
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
                        # device_name 用用户配置值供规则匹配，actual_device 保留实际名称。
                        emit_event({
                            "device_name": target,
                            "actual_device": device,
                            "state": target_state,
                        })

            last_devices = current_devices
            last_hash = current_hash

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 扫描异常 #{scan_count}: {e}")
            # 不更新 last_devices，防止瞬时错误产生误报
