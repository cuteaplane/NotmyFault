import subprocess
import time


def get_connected_devices():
    """通过 Get-PnpDevice 获取当前已连接的蓝牙设备名列表"""
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-PnpDevice -Class Bluetooth -Status OK | Select-Object -ExpandProperty FriendlyName",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        devices = set()
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                devices.add(line)
        return devices
    except Exception as e:
        print(f"[Trigger:bluetooth_device] 扫描失败: {e}")
        return set()


def run(meta, config_list, emit_event):
    trigger_id = meta.get("id", "bluetooth_device")
    print(f"[Trigger:{trigger_id}] 蓝牙设备监控已启动（共 {len(config_list)} 条规则）")

    # 初始扫描，排除已有设备防止误报
    last_devices = get_connected_devices()
    print(f"[Trigger:{trigger_id}] 当前已连接: {last_devices or '无'}")

    while True:
        try:
            current_devices = get_connected_devices()

            new_devices = current_devices - last_devices
            lost_devices = last_devices - current_devices

            if new_devices or lost_devices:
                for config in config_list:
                    target = config.get("device_name", "").strip()
                    target_state = config.get("state", "connected")

                    # 连接事件
                    if target_state == "connected":
                        for device in new_devices:
                            if not target or target.lower() in device.lower():
                                print(f"[Trigger:{trigger_id}] 设备已连接: {device}")
                                emit_event(trigger_id, {
                                    "device_name": device,
                                    "state": "connected",
                                })

                    # 断开事件
                    if target_state == "disconnected":
                        for device in lost_devices:
                            if not target or target.lower() in device.lower():
                                print(f"[Trigger:{trigger_id}] 设备已断开: {device}")
                                emit_event(trigger_id, {
                                    "device_name": device,
                                    "state": "disconnected",
                                })

            last_devices = current_devices

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 扫描异常: {e}")

        time.sleep(3)
