#all of import 
import os
import sys
import subprocess
import ctypes

def register_toaster():
    aumid = 'cuteaplane.notmyfault.app'
    display_name = 'NotmyFault'   # 可自定义
    icon_path = os.path.join(os.path.dirname(__file__), 'logo.ico') 
    # 1. 检查是否已注册
    check_command = f'powershell -Command "Get-StartApps | Where-Object {{$_.AppUserModelId -eq \'{aumid}\'}}"'
    result_check = subprocess.run(check_command, capture_output=True, text=True, shell=True)
    if aumid in result_check.stdout:
        print(f"AUMID '{aumid}' 已注册，跳过。")
        return
    
    # 2. 获取当前 Python 解释器所在目录（真实路径，不是 WindowsApps 存根）
    python_exe = sys.executable          # 例如：C:\Users\cuteaplane\AppData\Local\Python\pythoncore-3.14-64\python.exe
    python_root = os.path.dirname(python_exe)   # 得到 ...\pythoncore-3.14-64
    
    # 3. 定位 Scripts 目录
    scripts_dir = os.path.join(python_root, 'Scripts')
    register_exe = os.path.join(scripts_dir, 'register_hkey_aumid.exe')
    
    if not os.path.isfile(register_exe):
        print(f"未找到注册程序：{register_exe}")
        print("请确认 windows-toasts 库已正确安装：pip install windows-toasts")
        # 可选：尝试在虚拟环境或其他常见位置查找
        # 如果还找不到，可以尝试使用 where 命令
        where_reg = subprocess.run('where register_hkey_aumid', capture_output=True, text=True, shell=True)
        if where_reg.returncode == 0 and where_reg.stdout.strip():
            register_exe = where_reg.stdout.strip().splitlines()[0]
            print(f"通过 where 找到：{register_exe}")
        else:
            return
    
    # 4. 执行注册命令（需要管理员权限）
    cmd = f'"{register_exe}" --app_id "{aumid}" --name "{display_name}" --icon "{icon_path}"'
    print(f"执行命令：{cmd}")
    try:
        # register_hkey_aumid.exe 没有请求管理员权限的 manifest，需要在这里请求管理员
        ctypes.windll.shell32.ShellExecuteW(None, "runas", register_exe, f'--app_id "{aumid}" --name "{display_name}" --icon "{icon_path}"', None, 1)

        print(f"成功注册 AUMID：{aumid}")
    except subprocess.CalledProcessError as e:
        print(f"注册失败，错误码：{e.returncode}")
        print("请确认：1) 以管理员身份运行此脚本 2) 图标路径正确且为 .ico 格式")