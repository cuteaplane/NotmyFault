"""创建快捷方式动作
Windows 用 PowerShell WScript.Shell 创建 .lnk；Linux 创建 .desktop 文件
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
        return _run_linux(action_info, params)
    return _run_windows(action_info, params)


def _validate_common(params) -> tuple[str, str, str]:
    name = str(params.get("name", "") or "").strip()
    target = str(params.get("target_path", "") or "").strip()
    if not name:
        raise ValueError("未指定快捷方式名称")
    if any(ch in name for ch in ("\\", "/", ":")) or ".." in name:
        raise ValueError(f"快捷方式名称不合法: {name!r}")
    if not target:
        raise ValueError("未指定快捷方式目标路径")
    location = str(params.get("location", "desktop") or "desktop")
    return name, target, location


def _run_linux(action_info, params):
    from pathlib import Path

    name, target, location = _validate_common(params)
    if location not in ("desktop", "applications"):
        raise ValueError(f"无效的创建位置: {location!r}（可选: desktop/applications）")

    desktop_entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Exec={target}\n"
        "Terminal=false\n"
    )
    arguments = str(params.get("arguments", "") or "").strip()
    if arguments:
        desktop_entry = desktop_entry.replace(
            f"Exec={target}", f"Exec={target} {arguments}"
        )

    if location == "desktop":
        base_dir = Path.home() / "Desktop"
        if not base_dir.is_dir():
            base_dir = Path.home() / "桌面"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        base_dir = Path(xdg_data) if xdg_data else Path.home() / ".local" / "share"
        base_dir = base_dir / "applications"

    base_dir.mkdir(parents=True, exist_ok=True)
    safe_name = name.replace("/", "_").replace(" ", "_")
    desktop_path = base_dir / f"{safe_name}.desktop"
    desktop_path.write_text(desktop_entry, encoding="utf-8")
    desktop_path.chmod(0o755)
    print(f"[Action:create_shortcut] 已创建: {desktop_path}")
    return {"shortcut_path": str(desktop_path)}


def _run_windows(action_info, params):
    name, target, location = _validate_common(params)
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
