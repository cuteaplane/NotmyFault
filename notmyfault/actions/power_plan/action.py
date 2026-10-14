"""电源计划切换：Windows 用 powercfg GUID，Linux 用 powerprofilesctl"""

import subprocess
import sys
import uuid

from notmyfault.plugin_api import platform_backend_api, action_values

_platform_backend = platform_backend_api()
PowerProfileBackend = _platform_backend.PowerProfileBackend
BackendError = _platform_backend.BackendError
default_runner = _platform_backend.default_runner

# Windows 电源计划 GUID 在不同语言系统上保持一致
_PLANS = {
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "high_performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "power_saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
}


def _run_windows(plan: str, params: dict) -> dict:
    if plan == "custom":
        guid = str(params.get("custom_guid", "") or "").strip()
        if not guid:
            raise ValueError("自定义电源计划必须提供 GUID")
        try:
            guid = str(uuid.UUID(guid))
        except ValueError:
            raise ValueError("自定义电源计划必须提供有效的 GUID") from None
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


def _run_linux(plan: str, params: dict) -> dict:
    if plan == "custom":
        raise ValueError("Linux 不支持自定义 GUID 电源计划，请改用 balanced/high_performance/power_saver")
    if plan not in _PLANS:
        raise ValueError(
            f"未知电源计划: {plan!r}"
            "（可选: balanced/high_performance/power_saver）"
        )
    print(f"[Action:power_plan] 切换电源计划: {plan}")
    try:
        profile = PowerProfileBackend(default_runner).set_profile(plan)
    except BackendError as error:
        raise RuntimeError(str(error)) from error
    print(f"[Action:power_plan] 已切换到 {plan}（{profile}）")
    return {"plan": plan, "profile": profile}


def run_with_context(action_info, params, context):
    params = action_values(params)
    plan = params.get("plan", "balanced")
    if sys.platform == "win32":
        return _run_windows(plan, params)
    if sys.platform.startswith("linux"):
        return _run_linux(plan, params)
    raise RuntimeError("电源计划切换仅支持 Windows 和 Linux")
