from windows_toasts import WindowsToaster, Toast
from windows_toasts.toasters import InteractableWindowsToaster
toaster = InteractableWindowsToaster('NotmyFault', 'cuteaplane.notmyfault.app')
import time
import threading
def _remove_toast_after_delay(toast, delay):
    """在子线程中等待 delay 秒后移除通知"""
    time.sleep(delay)
    try:
        toaster.remove_toast(toast)
    except Exception as e:
        # 如果通知已经自动消失，忽略错误
        print(f"[DEBUG] remove_toast failed (maybe already gone): {e}")

def show_notification(title, message, display_seconds=10):
    toast = Toast()
    toast.text_fields = [title, message]
    toaster.show_toast(toast)
    
    # 启动后台线程，在指定延迟后移除通知
    threading.Thread(target=_remove_toast_after_delay, args=(toast, display_seconds), daemon=True).start()