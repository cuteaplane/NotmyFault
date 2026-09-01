def run(meta, config, emit_event, shutdown_event):
    """手动触发由 Dashboard API 分发，run 只等待 shutdown_event"""
    shutdown_event.wait()
