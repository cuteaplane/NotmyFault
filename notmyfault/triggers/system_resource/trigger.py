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


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "system_resource")
    resource = config.get("resource", "cpu")
    direction = config.get("direction", "above")
    if resource not in ("cpu", "memory", "disk", "network"):
        raise ValueError(
            f"无效的资源类型: {resource!r}（可选: cpu/memory/disk/network）"
        )
    if direction not in ("above", "below"):
        raise ValueError(
            f"无效的阈值方向: {direction!r}（可选: above/below）"
        )
    try:
        threshold = float(config.get("threshold", 90))
    except (TypeError, ValueError):
        raise ValueError(
            f"threshold 必须是数字，实际: {config.get('threshold')!r}"
        ) from None
    print(f"[Trigger:{trigger_id}] 开始监控系统资源: {resource} {direction}")
    last_triggered = False
    # network 采样基线，用于计算速率而非累计字节
    net_prev = None  # (timestamp, total_bytes)
    net_rate = 0.0
    first_sample = True  # 首轮只建基线，不做判定，避免误触发

    while not shutdown_event.is_set():
        try:
            if resource == "network":
                net = psutil.net_io_counters()
                total = net.bytes_sent + net.bytes_recv
                now = time.time()
                if net_prev is not None:
                    elapsed = now - net_prev[0]
                    if elapsed > 0:
                        net_rate = (total - net_prev[1]) / elapsed / (1024 * 1024)
                net_prev = (now, total)
                value = net_rate
            else:
                value = _get_usage(resource)

            if first_sample:
                # cpu_percent(interval=0) 首次调用没有基线，返回 0；
                # network 首轮也没有速率可算。只记录状态，跳过判定。
                first_sample = False
                last_triggered = False
                continue

            triggered = (direction == "above" and value >= threshold) or (
                direction == "below" and value <= threshold
            )
            if triggered and not last_triggered:
                unit = "MB/s" if resource == "network" else "%"
                print(
                    f"[Trigger:{trigger_id}] {resource} {direction} "
                    f"{threshold}{unit} (当前: {value:.1f})"
                )
                emit_event({
                    "resource": resource,
                    "value": round(value, 1),
                    "threshold": threshold,
                    "direction": direction,
                })
            last_triggered = triggered
        except Exception as e:
            print(f"[Trigger:{trigger_id}] 检查 {resource} 出错: {e}")
            # 异常时清空触发标志，避免恢复后把同一次越界再触发一遍
            last_triggered = False

        shutdown_event.wait(5)
