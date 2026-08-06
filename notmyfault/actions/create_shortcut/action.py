"""创建快捷方式动作：用 PowerShell 的 WScript.Shell 在桌面或开始菜单创建 .lnk 文件
参数通过环境变量传给子进程，路径拼接在 PowerShell 内完成
"""

import os
import subprocess
import sys

# 脚本只从环境变量读值；路径拼接也全部在 PowerShell 里做
_POWERSHELL_SCRIPT = r"""
$name = $env:NMF_LNK_NAME
$target = $env:NMF_LNK_TARGET
if (-not $name -or -not $target) { throw "缺少快捷方式名称或目标路径" }
if ($env:NMF_LNK_LOCATION -eq 'start_menu') {
    $base = [Environment]::GetFolderPath('StartMenu')
} else {
    $base = [Environment]::GetFolderPath('Desktop')
}
$path = Join-Path $base ($name + '.lnk')
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($path)
$sc.TargetPath = $target
if ($env:NMF_LNK_ARGS) { $sc.Arguments = $env:NMF_LNK_ARGS }
if ($env:NMF_LNK_WORKDIR) { $sc.WorkingDirectory = $env:NMF_LNK_WORKDIR }
if ($env:NMF_LNK_ICON) { $sc.IconLocation = $env:NMF_LNK_ICON }
$sc.Save()
Write-Output $path
"""


def run(action_info, params):
    if sys.platform != "win32":
        raise RuntimeError("创建快捷方式仅支持 Windows")

    name = str(params.get("name", "") or "").strip()
    target = str(params.get("target_path", "") or "").strip()
    if not name:
        raise ValueError("未指定快捷方式名称")
    # name 会拼进 Join-Path，带分隔符就能写到桌面和开始菜单之外
    if any(ch in name for ch in ("\\", "/", ":")) or ".." in name:
        raise ValueError(f"快捷方式名称不合法: {name!r}")
    if not target:
        raise ValueError("未指定快捷方式目标路径")
    location = str(params.get("location", "desktop") or "desktop")
    if location not in ("desktop", "start_menu"):
        raise ValueError(f"无效的创建位置: {location!r}（可选: desktop/start_menu）")

    env = os.environ.copy()
    # NMF_ 前缀变量仅供本任务使用，系统环境变量保持原值
    env["NMF_LNK_LOCATION"] = location
    env["NMF_LNK_NAME"] = name
    env["NMF_LNK_TARGET"] = target
    env["NMF_LNK_ARGS"] = str(params.get("arguments", "") or "")
    env["NMF_LNK_WORKDIR"] = str(params.get("working_directory", "") or "")
    env["NMF_LNK_ICON"] = str(params.get("icon_path", "") or "")

    print(f"[Action:create_shortcut] 创建快捷方式: {name}.lnk -> {target}")
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _POWERSHELL_SCRIPT,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"执行 PowerShell 失败: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-300:]
        raise RuntimeError(
            f"创建快捷方式失败 (code={result.returncode}): {detail}"
        )

    shortcut_path = (result.stdout or "").strip()
    print(f"[Action:create_shortcut] 已创建: {shortcut_path}")
    return {"shortcut_path": shortcut_path}
