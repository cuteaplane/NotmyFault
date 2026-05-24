from Win_toaster.show_notification import show_notification


def run(action_info, params):
    title = params.get("title", "NotmyFault")
    message = params.get("message", "")
    print(f"[Action:notify] 显示通知: {title} / {message}")
    show_notification(title, message)
