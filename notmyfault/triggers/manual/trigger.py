def run(meta, config, emit_event, shutdown_event):
    """手动触发由 Dashboard API 精确分发，后台不需要轮询。"""
    shutdown_event.wait()
