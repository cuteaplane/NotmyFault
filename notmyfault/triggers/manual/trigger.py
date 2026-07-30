def run(meta, config_list, emit_event, shutdown_event):
    """手动触发由 Dashboard API 精确分发，后台不需要轮询。"""
    if config_list:
        shutdown_event.wait()
