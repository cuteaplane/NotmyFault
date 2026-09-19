import os
import sys
import subprocess
import ctypes
import winreg


def register_protocol() -> bool:
    """注册 notmyfault:// 协议并启动 dashboard.pyw，注册表写入 HKCU"""
    protocol = "notmyfault"
    key_path = f"SOFTWARE\\Classes\\{protocol}"

    dashboard_pyw = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "dashboard.pyw")
    )
    if not os.path.exists(dashboard_pyw):
        print(f"[Protocol] 找不到 dashboard.pyw: {dashboard_pyw}")
        return False

    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable  # 当前解释器没有 pythonw.exe 时使用原路径

    command = f'"{pythonw}" "{dashboard_pyw}" --protocol "%1"'

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValue(key, "", winreg.REG_SZ, "URL:NotmyFault Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, f"{key_path}\\shell\\open\\command"
        ) as key:
            winreg.SetValue(key, "", winreg.REG_SZ, command)

        print(f"[Protocol] 已注册协议: {protocol}:// → dashboard.pyw")
        return True
    except OSError as e:
        print(f"[Protocol] 注册协议失败: {e}")
        return False


def _record_install_location(key):
    app_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    install_root = os.path.dirname(app_root)
    if os.path.basename(app_root).lower() == "app" and os.path.isfile(
        os.path.join(install_root, ".notmyfault-install")
    ):
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_root)


def register_aumid_registry(aumid: str, display_name: str, icon_path: str | None) -> bool:
    key_path = f"SOFTWARE\\Classes\\AppUserModelId\\{aumid}"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as master_key:
            winreg.SetValueEx(master_key, "DisplayName", 0, winreg.REG_SZ, display_name)
            if icon_path:
                winreg.SetValueEx(master_key, "IconUri", 0, winreg.REG_SZ, icon_path)
            _record_install_location(master_key)
        return True
    except OSError as e:
        print(f"[AUMID_Register] 直接写注册表失败：{e}")
        return False


def register_toaster():
    register_protocol()

    aumid = 'cuteaplane.notmyfault.app'
    display_name = 'NotmyFault'
    icon_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'logo.ico'))
    icon_uri = None
    if os.path.isfile(icon_path) and icon_path.lower().endswith('.ico'):
        icon_uri = icon_path
    else:
        if not os.path.isfile(icon_path):
            print(f"[AUMID_Register] 图标文件不存在：{icon_path}，将跳过 IconUri 注册。")
        elif not icon_path.lower().endswith('.ico'):
            print(f"[AUMID_Register] 图标文件不是 .ico：{icon_path}，将跳过 IconUri 注册。")

    # 直接查询注册表确认 AUMID，Get-StartApps 不列出仅写入注册表的 AUMID
    key_path = f"SOFTWARE\\Classes\\AppUserModelId\\{aumid}"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        ) as check_key:
            winreg.QueryValueEx(check_key, "DisplayName")
            _record_install_location(check_key)
        print(f"[AUMID_Register] AUMID '{aumid}' 已注册（注册表检测），跳过。")
        return
    except OSError:
        pass  # 注册表键不存在，继续注册

    print("[AUMID_Register] 尝试直接写注册表以注册 AUMID...")
    if register_aumid_registry(aumid, display_name, icon_uri):
        print(f"[AUMID_Register] 已直接写入注册表：{aumid}")
        print("[AUMID_Register] 开始创建快捷方式以触发系统识别新 AUMID...")
        import Win_toaster.create_shortcut_with_aumid as create_shortcut_with_aumid
        create_shortcut_with_aumid.create_shortcut()
        return

    print("[AUMID_Register] 直接写注册表失败，回退到注册工具执行逻辑。")

    python_exe = sys.executable
    python_root = os.path.dirname(python_exe)
    if os.path.basename(python_root).lower() == 'scripts':
        python_root = os.path.dirname(python_root)

    scripts_dir = os.path.join(python_root, 'Scripts')
    register_exe = os.path.join(scripts_dir, 'register_hkey_aumid.exe')

    if not os.path.isfile(register_exe):
        print(f"[AUMID_Register] 未在 {scripts_dir} 中找到 register_hkey_aumid.exe")
        where_reg = subprocess.run('where register_hkey_aumid', capture_output=True, text=True, shell=True)
        if where_reg.returncode == 0 and where_reg.stdout.strip():
            register_exe = where_reg.stdout.strip().splitlines()[0]
            print(f"[AUMID_Register] 通过 where 找到：{register_exe}")
        else:
            register_exe = None

    arguments = ["--app_id", aumid, "--name", display_name]
    if icon_uri:
        arguments += ["--icon", icon_uri]
    candidates = []
    if register_exe and os.path.isfile(register_exe):
        candidates.append((register_exe, arguments))
    candidates.append((python_exe, ["-m", "register_hkey_aumid", *arguments]))
    for executable, command_args in candidates:
        parameters = subprocess.list2cmdline(command_args)
        try:
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, parameters, None, 1)
            if int(result) > 32:
                print(f"[AUMID_Register] 已启动注册工具: {executable}")
                return
            print(f"[AUMID_Register] 注册工具启动失败: {executable}，返回值 {result}")
        except Exception as error:
            print(f"[AUMID_Register] 注册工具启动失败: {executable}，{error}")
