from __future__ import annotations

import json
from pathlib import Path

from notmyfault.platform._run import default_runner, powershell_path
from notmyfault.platform.errors import (
    BackendFailedError, BackendInvalidError, BackendMissingError, BackendPermissionDeniedError,
)
from notmyfault.platform.windows import _win32 as w

_ERRORS = {
    "no_radio": "未发现蓝牙无线电",
    "access_denied": "Windows 拒绝蓝牙控制权限",
    "state_change_denied": "Windows 拒绝更改蓝牙状态",
    "state_not_applied": "Windows 接受了请求，但蓝牙状态没有改变",
    "disabled_radio": "蓝牙无线电已被硬件开关或系统策略禁用",
    "winrt_unavailable": "当前 Windows 无法使用蓝牙无线电接口",
    "operation_timeout": "等待 Windows 更改蓝牙状态超时",
}


class BluetoothBackend:
    def __init__(self, runner=None):
        self._runner = runner or default_runner

    def _execute(self, action):
        helper = Path(__file__).with_name("radio.ps1")
        if not helper.is_file():
            raise BackendMissingError("蓝牙后端缺少 radio.ps1")
        try:
            result = self._runner.run(
                [powershell_path(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                 "Bypass", "-File", str(helper), "-Action", action],
                timeout=30, encoding="utf-8", creationflags=w.CREATE_NO_WINDOW,
            )
        except OSError as error:
            raise BackendFailedError(f"无法执行蓝牙辅助程序：{error}") from error
        payload = None
        for line in reversed(result.stdout.splitlines()):
            try:
                value = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                payload = value
                break
        if payload is None:
            raise BackendFailedError("蓝牙辅助程序没有返回有效结果")
        code = payload.get("code")
        if action == "query" and code == "no_radio":
            return {"state": "unavailable"}
        if result.returncode or payload.get("ok") is not True:
            error = BackendFailedError
            if code == "no_radio":
                error = BackendMissingError
            elif code in ("access_denied", "state_change_denied", "disabled_radio"):
                error = BackendPermissionDeniedError
            message = _ERRORS.get(code) or str(payload.get("message") or "蓝牙辅助程序执行失败")
            detail = str(payload.get("detail", "")).strip()
            raise error(f"{message}：{detail}" if detail and detail != message else message)
        state = payload.get("state")
        if state not in ("on", "off"):
            raise BackendFailedError(f"蓝牙无线电状态无法表示为开关：{state!r}")
        return payload

    def state(self) -> str:
        return self._execute("query")["state"]

    def set(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise BackendInvalidError("蓝牙目标状态必须是布尔值")
        target = "on" if enabled else "off"
        payload = self._execute(target)
        if payload["state"] != target:
            raise BackendFailedError(f"请求蓝牙切换到 {target}，回读状态仍为 {payload['state']}")
        if not isinstance(payload.get("changed"), bool):
            raise BackendFailedError("蓝牙辅助程序没有返回有效的状态变化标志")
        result = {"state": target, "changed": payload["changed"], "method": "winrt"}
        if "radios" in payload:
            result["radios"] = payload["radios"]
        return result
