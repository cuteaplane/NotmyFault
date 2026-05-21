# all of import
import os
import sys
import subprocess
import ctypes
import winreg


def register_aumid_registry(aumid: str, display_name: str, icon_path: str | None) -> bool:
    key_path = f"SOFTWARE\\Classes\\AppUserModelId\\{aumid}"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as master_key:
            winreg.SetValueEx(master_key, "DisplayName", 0, winreg.REG_SZ, display_name)
            if icon_path:
                winreg.SetValueEx(master_key, "IconUri", 0, winreg.REG_SZ, icon_path)
        return True
    except OSError as e:
        print(f"直接写注册表失败：{e}")
        return False


def register_toaster():
    aumid = 'cuteaplane.notmyfault.app'
    display_name = 'NotmyFault'   # 可自定义
    icon_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'logo.ico'))
    icon_uri = None
    if os.path.isfile(icon_path) and icon_path.lower().endswith('.ico'):
        icon_uri = icon_path
    else:
        if not os.path.isfile(icon_path):
            print(f"图标文件不存在：{icon_path}，将跳过 IconUri 注册。")
        elif not icon_path.lower().endswith('.ico'):
            print(f"图标文件不是 .ico：{icon_path}，将跳过 IconUri 注册。")

    # 1. 检查是否已注册
    check_command = f'powershell -Command "Get-StartApps | Where-Object {{$_.AppUserModelId -eq \'{aumid}\'}}"'
    result_check = subprocess.run(check_command, capture_output=True, text=True, shell=True)
    if aumid in result_check.stdout:
        print(f"AUMID '{aumid}' 已注册，跳过。")
        return

    # 2. 首选直接写注册表
    print("尝试直接写注册表以注册 AUMID...")
    if register_aumid_registry(aumid, display_name, icon_uri):
        print(f"已直接写入注册表：{aumid}")
        return

    print("直接写注册表失败，回退到注册工具执行逻辑。")

    # 3. 获取当前 Python 解释器所在目录
    python_exe = sys.executable
    python_root = os.path.dirname(python_exe)
    if os.path.basename(python_root).lower() == 'scripts':
        python_root = os.path.dirname(python_root)

    # 4. 定位 Scripts 目录
    scripts_dir = os.path.join(python_root, 'Scripts')
    register_exe = os.path.join(scripts_dir, 'register_hkey_aumid.exe')

    # 5. 优先尝试：执行 register_hkey_aumid.exe
    if not os.path.isfile(register_exe):
        print(f"未在 {scripts_dir} 中找到 register_hkey_aumid.exe")
        where_reg = subprocess.run('where register_hkey_aumid', capture_output=True, text=True, shell=True)
        if where_reg.returncode == 0 and where_reg.stdout.strip():
            register_exe = where_reg.stdout.strip().splitlines()[0]
            print(f"通过 where 找到：{register_exe}")
        else:
            register_exe = None

    if register_exe and os.path.isfile(register_exe):
        args = f'--app_id "{aumid}" --name "{display_name}"'
        if icon_uri:
            args += f' --icon "{icon_uri}"'
        print(f"尝试以管理员方式执行：{register_exe} {args}")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(None, 'runas', register_exe, args, None, 1)
            succeeded = False
            try:
                succeeded = int(ret) > 32
            except Exception:
                succeeded = False
            if succeeded:
                print(f"通过 register_hkey_aumid.exe 成功注册 AUMID：{aumid}")
                return
            print(f"以管理员运行 register_hkey_aumid.exe 失败，返回值：{ret}，将回退到 python -m register_hkey_aumid。")
        except Exception as e:
            print(f"以管理员运行 register_hkey_aumid.exe 时出现异常：{e}，将回退到 python -m register_hkey_aumid。")
    else:
        print('未找到 register_hkey_aumid.exe，准备回退到 python -m register_hkey_aumid。')

    # 6. 回退到 python -m register_hkey_aumid
    python_cmd = python_exe
    py_params = f'-m register_hkey_aumid --app_id "{aumid}" --name "{display_name}"'
    if icon_uri:
        py_params += f' --icon "{icon_uri}"'
    print(f"尝试以管理员方式执行：{python_cmd} {py_params}")
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(None, 'runas', python_cmd, py_params, None, 1)
        succeeded = False
        try:
            succeeded = int(ret) > 32
        except Exception:
            succeeded = False
        if succeeded:
            print(f"通过 python -m register_hkey_aumid 成功注册 AUMID：{aumid}")
            return
        print(f"以管理员运行 python -m register_hkey_aumid 失败，返回值：{ret}。请检查 python 环境和模块安装。")
    except Exception as e:
        print(f"以管理员运行 python -m register_hkey_aumid 时出现异常：{e}。请检查 python 环境和模块安装。")
