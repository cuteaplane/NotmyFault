# NotmyFault By cuteaplane
# My Github: https://github.com/cuteaplane
# -*- coding: utf-8 -*-
# Open Source in GPL-3（after complete）

import psutil
from windows_toasts import WindowsToaster, Toast
from windows_toasts.toasters import InteractableWindowsToaster
import time
import win32api
import json
import os

# Windows系统下的音量命令常量
WM_APPCOMMAND = 0x319
APPCOMMAND_VOLUME_MAX = 0x0a
APPCOMMAND_VOLUME_MIN = 0x09
APPCOMMAND_VOLUME_HALF = 0x08

# 默认配置数据
DEFAULT_CONFIG = {
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

# ----------------- 配置文件路径相关函数 -----------------
def get_config_file():
    r"""
    获取配置文件的完整路径，存放在当前用户的 AppData/Roaming 目录下。
    具体路径为：%APPDATA%\NotmyFault\config.json
    如果 NotmyFault 目录不存在，则自动创建。
    """
    # 获取当前用户的 AppData/Roaming 目录（环境变量 APPDATA 对应的就是此路径）
    appdata = os.getenv("APPDATA")
    # 定义配置存储的目录
    config_dir = os.path.join(appdata, "NotmyFault")
    # 如果目录不存在则创建
    if not os.path.exists(config_dir):
        os.makedirs(config_dir)
    # 返回配置文件的完整路径
    return os.path.join(config_dir, "config.json")

# 定义全局的配置文件路径
CONFIG_FILE = get_config_file()

# ----------------- 初始化通知系统 -----------------
# 使用 InteractableWindowsToaster 来实现通知功能
toaster = InteractableWindowsToaster('NotmyFault', 'cuteaplane.notmyfault.app')

# ----------------- 配置加载函数 -----------------
def load_config():
    """
    加载配置文件。如果配置文件不存在，则自动创建并写入默认配置，然后返回默认配置数据。
    """
    if not os.path.exists(CONFIG_FILE):
        # 若配置文件不存在，则写入默认设置到 CONFIG_FILE
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=4)
        print("[DEBUG] Default config created.")
        return DEFAULT_CONFIG

    # 如果配置文件存在，则读取配置内容
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    print("[DEBUG] Config loaded:", config)
    return config

# ----------------- 辅助函数 -----------------
def check_if_running(process_name):
    """
    检查指定的进程是否正在运行。
    遍历当前系统进程，并对比进程名（不区分大小写）。
    """
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if process_name.lower() == proc.info['name'].lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False

def set_volume(action_type):
    """
    根据配置中的 volume_action 调整系统音量。
    使用 win32api.SendMessage 发送音量控制消息。
    
    参数:
        action_type: "max"、"half" 或 "min"
    """
    action_map = {
        "max": APPCOMMAND_VOLUME_MAX,
        "half": APPCOMMAND_VOLUME_HALF,
        "min": APPCOMMAND_VOLUME_MIN
    }
    if action_type in action_map:
        print(f"[DEBUG] Setting volume to '{action_type}' using command {action_map[action_type]}.")
        # 发送消息时，乘以 0x10000 是为了将命令值放到高位参数中
        win32api.SendMessage(-1, WM_APPCOMMAND, 0x30292, action_map[action_type] * 0x10000)

def show_notification(title, message):
    """
    显示系统通知，利用 Windows Toast 通知。
    
    参数:
        title: 通知标题
        message: 通知正文
    """
    new_toast = Toast()
    new_toast.text_fields = [title, message]
    toaster.show_toast(new_toast)
    time.sleep(2)

# ----------------- 进程监控类 -----------------
class ProcessMonitor:
    def __init__(self):
        # 加载配置文件
        self.config = load_config()
        # 初始化进程状态，记录每个配置进程的运行状态，
        # 避免重复触发（即在进程已运行时不会重复触发通知和音量设置）
        self.process_states = {p['process_name']: check_if_running(p['process_name']) 
                               for p in self.config['processes']}

    def scan_processes(self):
        """
        扫描所有配置中的进程，根据状态变化（启动或关闭）执行对应操作：
        - 进程启动：设置音量，并显示通知
        - 进程关闭：更新状态记录
        """
        for process in self.config['processes']:
            print(f"[DEBUG] Scanning process: {process['process_name']}")
            is_running = check_if_running(process['process_name'])
            
            # 状态变化检测：如果进程新启动，则触发操作
            if is_running and not self.process_states[process['process_name']]:
                print(f"[DEBUG] Process '{process['process_name']}' started.")
                set_volume(process['volume_action'])
                show_notification(
                    process['notification']['title'],
                    process['notification']['message']
                )
                self.process_states[process['process_name']] = True
                
            # 如果进程停止，更新状态，但这里只打印信息
            elif not is_running and self.process_states[process['process_name']]:
                print(f"[DEBUG] Process '{process['process_name']}' stopped.")
                print(f"{process['software_name']} 已关闭")
                self.process_states[process['process_name']] = False

    def run(self):
        """主循环，每5秒扫描一次配置中的所有进程。"""
        while True:
            print("[DEBUG] Starting process scan cycle.")
            self.scan_processes()
            time.sleep(5)

if __name__ == "__main__":
    monitor = ProcessMonitor()
    monitor.run()
