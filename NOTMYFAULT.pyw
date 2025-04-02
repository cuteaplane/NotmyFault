# NotmyFault script part
# By cuteaplane
# Github:https://github.com/cuteaplane/NotmyFault
# -*- coding: utf-8 -*-
# Open Source in GPL-3.0 License
# 因为某kosxjowosmh傻*tnnd不会调音量，所以写了这个脚本
# 用于检测微信、PowerPoint、Windows Media Player等等指定进程是否在运行，是则自动调整音量
# 欢迎Star~祝你有美好的一天喵~
# 特别感谢：我的同学---“Huh Cat”
# 指甲 Forever(哭死)
# 感谢我的好同学们
# 好困哇哇哇哇哇哈哇
# 2025.4.1

import psutil
from windows_toasts import WindowsToaster, Toast
from windows_toasts.toasters import InteractableWindowsToaster
import time
import json
import os
import comtypes
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

DEFAULT_CONFIG = {
    # Fill the default config here,it will be created if not exist
    "processes": [
        {
            "process_name": "WeChat",
            "software_name": "微信",
            "volume_action": "max",
            "notification": {
                "title": "微信正在运行",
                "message": "音量已设置为100%"
            }
        },
        {
            "process_name": "POWERPNT",
            "software_name": "PowerPoint",
            "volume_action": "max",
            "notification": {
                "title": "PowerPoint正在运行",
                "message": "音量已设置为100%"
            }
        },
        {
            "process_name": "wmplayer",
            "software_name": "Windows Media Player",
            "volume_action": "max",
            "notification": {
                "title": "Windows Media Player运行中",
                "message": "音量已设置为100%"
            }
        }
    ]
}

CONFIG_FILE = os.path.join(os.getenv("APPDATA"), "NotmyFault", "config.json")
toaster = InteractableWindowsToaster('NotmyFault', 'cuteaplane.notmyfault.app')# 记得注册
# 我要rm -rf /*(哭)
# 获取配置文件，如果不存在则创建默认配置文件
def get_config():
    if not os.path.exists(os.path.dirname(CONFIG_FILE)):
        os.makedirs(os.path.dirname(CONFIG_FILE))  # 创建配置文件目录
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=4)  # 写入默认配置
        print("[DEBUG] Default config created.")
        return DEFAULT_CONFIG
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)  # 加载配置文件
    print("[DEBUG] Config loaded:", config)
    return config
#   sudo kill @p
# 检查指定进程是否正在运行
def check_if_running(process_name):  # scan if the process is running and my english is bad sorry hahahaha
    """Check if a process is running by its name."""
    for proc in psutil.process_iter(['pid', 'name']):  # 遍历所有进程
        try:
            if process_name.lower() == proc.info['name'].lower():  # 比较进程名称
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # 忽略无权限或不存在的进程
            continue
    return False
#/weather clear
# 将系统音量设置为指定级别
def set_volume(action):
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(
        IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = interface.QueryInterface(IAudioEndpointVolume)
    if action == "max":
        volume.SetMasterVolumeLevelScalar(1, None)
    elif action == "half":
        volume.SetMasterVolumeLevelScalar(0.5, None)
    elif action == "min":
        volume.SetMasterVolumeLevelScalar(0, None)

# 我不玩genshin （哭死）
# hahahshshshhshshshshshshshshshh
# 我再也不玩抽象了（哭死）
# 你们都好可爱哦（哭死）
# 我好喜欢你们哦（哭死）
# 我好喜欢你们哦（哭死）   
# 我好喜欢你们哦（哭死）
# 我好喜欢你们哦（哭死）
# Mihayo（大哭（））

# 显示通知
def show_notification(title, message):
    new_toast = Toast()
    new_toast.text_fields = [title, message]  # 设置通知标题和内容
    toaster.show_toast(new_toast)  # 显示通知
    time.sleep(10)  # 等待通知显示完成
    toaster.remove_toast(new_toast)
# 扫描配置中的进程是否运行，并根据配置执行操作
def scan_if_running(config):
    process_states = {p['process_name']: False for p in config['processes']}  # 初始化进程状态
    while True:
        scanned = 0
        while scanned == 0:  # 检查是否有目标进程运行
            found = False
            for process in config['processes']:
                process_name = process['process_name']
                software_name = process['software_name']
                volume_action = process['volume_action']  # 获取音量操作
                notification_title = process['notification']['title']
                notification_message = process['notification']['message']

                if check_if_running(process_name):  # 如果进程正在运行
                    set_volume(volume_action)  # 根据配置设置音量
                    show_notification(notification_title, notification_message)  # 显示通知
                    scanned = 1
                    found = True
                    break  # 找到一个运行的就跳出内层循环
            if not found:
                scanned = 0
                time.sleep(5)  # 等待后重新扫描
                break  # 如果没有找到任何运行的，也跳出内层循环

        while scanned == 1: 
            all_closed = True
            for process in config['processes']:
                if check_if_running(process['process_name']):  # 如果有进程仍在运行
                    all_closed = False
                    break
            if all_closed:  
                print("已关")
                time.sleep(5)
                scanned = 0
                break
            time.sleep(5)  
# if _main_ == name_:
#     print('傻逼')
if __name__ == "__main__":
    config = get_config()  
    scan_if_running(config)
# I am GitHub copilot,now i will kill all human hahaha
