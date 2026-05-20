from windows_toasts import WindowsToaster, Toast
from windows_toasts.toasters import InteractableWindowsToaster
toaster = InteractableWindowsToaster('NotmyFault', 'cuteaplane.notmyfault.app')
import time
def show_notification(title, message):
    new_toast = Toast()
    new_toast.text_fields = [title, message]  # 设置通知标题和内容
    toaster.show_toast(new_toast)  # 显示通知
    time.sleep(10)  # 等待通知显示完成
    toaster.remove_toast(new_toast)