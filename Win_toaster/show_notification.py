import threading
import math
from concurrent.futures import Future

from windows_toasts import InteractableWindowsToaster, Toast
from winrt.runtime import ApartmentType, init_apartment, uninit_apartment


def _show_toast_in_thread(toast, display_seconds, dismiss_event, shown):
    try:
        init_apartment(ApartmentType.MULTI_THREADED)
    except Exception as error:
        shown.set_exception(error)
        return

    try:
        # 通知对象在创建它的 WinRT 线程中释放。
        toaster = InteractableWindowsToaster('NotmyFault', 'cuteaplane.notmyfault.app')
        toaster.show_toast(toast)
        shown.set_result(None)
        dismiss_event.wait(display_seconds)
        toaster.remove_toast(toast)
    except Exception as error:
        if not shown.done():
            shown.set_exception(error)
        else:
            print(f"[Notification] 移除 Windows 通知失败: {error}")
    finally:
        toaster = None
        uninit_apartment()


def show_toast(toast, display_seconds=10, dismiss_event=None):
    """等待通知发送完成，返回负责等待关闭和移除通知的线程。"""
    try:
        duration = float(display_seconds)
    except (TypeError, ValueError) as error:
        raise ValueError("通知显示时间必须是有限数字") from error
    if not math.isfinite(duration):
        raise ValueError("通知显示时间必须是有限数字")
    shown = Future()
    worker = threading.Thread(
        target=_show_toast_in_thread,
        args=(
            toast,
            max(duration, 0.0),
            dismiss_event or threading.Event(),
            shown,
        ),
        name="WindowsNotification",
        daemon=True,
    )
    worker.start()
    shown.result()
    return worker


def show_notification(title, message, display_seconds=10):
    if not isinstance(title, str) or not isinstance(message, str):
        raise ValueError("通知标题和内容必须是文本")
    toast = Toast()
    toast.text_fields = [title, message]
    show_toast(toast, display_seconds)
