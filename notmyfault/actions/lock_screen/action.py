from notmyfault.plugin_api import platform


def run_with_context(action_info, params, context):
    print("[Action:lock_screen] 正在锁定屏幕...")
    platform.session.lock()
    print("[Action:lock_screen] 屏幕已锁定")
