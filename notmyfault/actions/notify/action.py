def run(action_info, params):
    from notmyfault.platform.platform_support import show_notification

    title = params.get("title", "NotmyFault")
    message = params.get("message", "")
    if not isinstance(title, str):
        title = str(title)
    if not isinstance(message, str):
        message = str(message)
    # 系统 Toast 处理超长文本不可靠，标题截断为 100 字符，正文截断为 500 字符
    title = title[:100]
    message = message[:500]
    print(f"[Action:notify] 显示通知: {title} / {message}")
    ok = show_notification(title, message)
    if not ok:
        raise RuntimeError("系统通知发送失败（通知服务不可用）")
