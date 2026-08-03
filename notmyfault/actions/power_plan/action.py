"""电源计划切换动作：通过 powercfg 使用计划 GUID 切换 Windows 电源计划
powercfg 在子进程中运行，并设置超时
"""

import subprocess
import sys

# Windows 电源计划 GUID 在不同语言系统上保持一致
_PLANS = {
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "high_performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "power_saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
}


def run(action_info, params):
    if sys.platform != "win32":
        raise RuntimeError("电源计划切换仅支持 Windows")

    plan = params.get("plan", "balanced")
    if plan == "custom":
        guid = str(params.get("custom_guid", "") or "").strip()
        if not guid:
            raise ValueError("自定义电源计划必须提供 GUID")
    elif plan in _PLANS:
        guid = _PLANS[plan]
    else:
        raise ValueError(
            f"未知电源计划: {plan!r}"
            "（可选: balanced/high_performance/power_saver/custom）"
        )

    print(f"[Action:power_plan] 切换电源计划: {plan} ({guid})")
    try:
        result = subprocess.run(
            ["powercfg", "/setactive", guid],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"执行 powercfg 失败: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-200:]
        raise RuntimeError(
            f"切换电源计划失败 (code={result.returncode}): {detail}"
        )

    print(f"[Action:power_plan] 已切换到 {plan}")
    return {"plan": plan, "guid": guid}
