import time
import psutil


def _get_usage(resource: str) -> float:
    if resource == "cpu":
        return psutil.cpu_percent(interval=0)
    elif resource == "memory":
        return psutil.virtual_memory().percent
    elif resource == "disk":
        return psutil.disk_usage("/").percent
    elif resource == "network":
        net = psutil.net_io_counters()
        return (net.bytes_sent + net.bytes_recv) / (1024 * 1024)
    return 0.0


def run(meta, config_list, emit_event, shutdown_event):
    trigger_id = meta.get("id", "system_resource")

    if not config_list:
        print(f"[Trigger:{trigger_id}] 没有配置规则，退出")
        return

    resources = {}
    for cfg in config_list:
        key = (cfg.get("resource", "cpu"), cfg.get("direction", "above"))
        resources[key] = cfg.get("threshold", 90)

    print(f"[Trigger:{trigger_id}] 开始监控系统资源: {list(resources.keys())}")
    last_triggered = {}
    # network 采样基线，用于计算速率而非累计字节
    net_prev = None  # (timestamp, total_bytes)
    net_rate = 0.0

    while not shutdown_event.is_set():
        net_sampled = False
        for (resource, direction), threshold in resources.items():
            try:
                if resource == "network":
                    # 每轮只采样一次，计算 MB/s 速率（阈值语义为 MB/s）
                    if not net_sampled:
                        net = psutil.net_io_counters()
                        total = net.bytes_sent + net.bytes_recv
                        now = time.time()
                        if net_prev is not None:
                            elapsed = now - net_prev[0]
                            if elapsed > 0:
                                net_rate = (total - net_prev[1]) / elapsed / (1024 * 1024)
                        net_prev = (now, total)
                        net_sampled = True
                    value = net_rate
                else:
                    value = _get_usage(resource)
                triggered = (direction == "above" and value >= threshold) or \
                            (direction == "below" and value <= threshold)
                prev = last_triggered.get((resource, direction), False)
                if triggered and not prev:
                    unit = "MB/s" if resource == "network" else "%"
                    print(f"[Trigger:{trigger_id}] {resource} {direction} {threshold}{unit} (当前: {value:.1f})")
                    emit_event(trigger_id, {
                        "resource": resource,
                        "value": round(value, 1),
                        "threshold": threshold,
                        "direction": direction,
                    })
                last_triggered[(resource, direction)] = triggered
            except Exception as e:
                print(f"[Trigger:{trigger_id}] 检查 {resource} 出错: {e}")

        shutdown_event.wait(5)
