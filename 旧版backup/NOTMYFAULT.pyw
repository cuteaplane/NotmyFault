# NotmyFault By cuteaplane
# My Github:https://github.com/cuteaplane
# -*- coding: utf-8 -*-
# Open Source in GPL-3（after complete)
# 因为某kosxjowosmh傻*tnnd不会调音量，所以写了这个脚本
# 用于检测微信、PowerPoint、Windows Media Player是否在运行，是则自动调整音量
# 欢迎Star~祝你有美好的一天喵~
# 特别感谢：Huh Cat
import psutil
from windows_toasts import WindowsToaster, Toast,ToastActivatedEventArgs,ToastButton
import time
import win32api
from windows_toasts.toasters import InteractableWindowsToaster

WM_APPCOMMAND = 0x319

APPCOMMAND_VOLUME_MAX = 0x0a
APPCOMMAND_VOLUME_MIN = 0x09
APPCOMMAND_VOLUME_HALF = 0x08  
toaster = InteractableWindowsToaster('NotmyFault','cuteaplane.notmyfault.app')# 记得注册

def check_if_running(process_name):
    for proc in psutil.process_iter(['pid', 'name']):
        if process_name.lower() in proc.info['name'].lower():
            return True
    return False
6
def set_volume_to_max():
    # 调整音量至100%
    win32api.SendMessage(-1, WM_APPCOMMAND, 0x30292, APPCOMMAND_VOLUME_MAX * 0x10000)

def set_volume_to_half():
    # 调整音量至50%
    win32api.SendMessage(-1, WM_APPCOMMAND, 0x30292, APPCOMMAND_VOLUME_HALF * 0x10000)

def activated_callback(activatedEventArgs: ToastActivatedEventArgs):
    # 不会用，在研究
    time.sleep(10)
    print(activatedEventArgs.arguments) 
    if activatedEventArgs.arguments == 'yes':
        set_volume_to_half()
        show_notification('音量已调整至50%')
    else:
        print('未知的参数')

def show_notification(title, message):
    # 通知function
    new_toast = Toast()
    #new_toast.AddAction(ToastButton('调节至50%', 'yes'))
    new_toast.text_fields = [title, message]
    toaster.show_toast(new_toast)
    #new_toast.on_activated = activated_callback
    #new_toast.on_activated(ToastButton)
    time.sleep(10)
    toaster.remove_toast(new_toast)
def scan_if_running(scanned):
    while scanned == 2:
        scanned = 0
    while scanned == 0:
        if check_if_running("WeChat"):
            set_volume_to_max()
            show_notification('微信正在运行', '音量已设置为100%')
            scanned = 1
            time.sleep(5)
            continue
        elif check_if_running("POWERPNT"):
            set_volume_to_max()
            show_notification('PowerPoint正在运行', '音量已设置为100%')
            scanned = 1
            time.sleep(5)
            continue
        elif check_if_running("wmplayer"):
            set_volume_to_max()
            show_notification('Windows Media Player运行中', '音量已设置为100%')
            scanned = 1
            time.sleep(5)
            continue
        else:
            scanned = 0
            time.sleep(5)
            break
    while scanned == 1:
        if check_if_running("WeChat"):
            scanned = 1
            time.sleep(5)
            continue
        elif check_if_running("POWERPNT"):
            scanned = 1
            time.sleep(5)
            continue
        elif check_if_running("wmplayer"):
            scanned = 1
            time.sleep(5)
            continue
        else:
            print("WeChat/PowerPoint/Windows Media Player已关")
            time.sleep(5)
            scanned = 0
            break
    time.sleep(5)
while True:
    scan_if_running(2)
