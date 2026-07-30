import socket
import time


def _is_connected(host="8.8.8.8", port=53, timeout=2):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except (socket.error, OSError):
        return False


def run(meta, config_list, emit_event, shutdown_event):
    trigger_id = meta.get("id", "network_status")

    target_states = set()
    for cfg in config_list:
        s = cfg.get("state", "disconnected")
        target_states.add(s)

    if not target_states:
        print(f"[Trigger:{trigger_id}] 没有配置目标状态，退出")
        return

    print(f"[Trigger:{trigger_id}] 开始监控网络状态，目标: {target_states}")

    last_connected = _is_connected()
    print(f"[Trigger:{trigger_id}] 初始网络状态: {'已连接' if last_connected else '已断开'}")

    while not shutdown_event.is_set():
        try:
            current = _is_connected()
            if current != last_connected:
                new_state = "connected" if current else "disconnected"
                if new_state in target_states:
                    print(f"[Trigger:{trigger_id}] 网络状态变化: {new_state}")
                    emit_event(trigger_id, {"state": new_state})
                last_connected = current
        except Exception as e:
            print(f"[Trigger:{trigger_id}] 检查网络出错: {e}")

        shutdown_event.wait(5)
