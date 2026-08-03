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
        # 通知自动消失后 remove_toast() 会抛异常，记录调试日志
        print(f"[DEBUG] remove_toast failed (maybe already gone): {e}")

def show_notification(title, message, display_seconds=10):
    toast = Toast()
    toast.text_fields = [title, message]
    toaster.show_toast(toast)
    
    threading.Thread(target=_remove_toast_after_delay, args=(toast, display_seconds), daemon=True).start()
