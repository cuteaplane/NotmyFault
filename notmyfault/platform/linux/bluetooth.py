from __future__ import annotations

import os
import time

from notmyfault.platform._run import default_runner
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError, BackendMissingError


class BluetoothBackend:
    def __init__(self, runner=None):
        self._runner = runner or default_runner

    def _run(self, *args, deadline):
        executable = self._runner.which("bluetoothctl")
        if not executable:
            raise BackendMissingError("未安装 bluetoothctl")
        timeout = min(8, deadline - time.monotonic())
        if timeout <= 0:
            raise BackendFailedError("等待蓝牙状态超过总时限（30s）")
        try:
            return self._runner.run(
                [executable, *args], timeout=timeout, env={**os.environ, "LC_ALL": "C"},
            )
        except OSError as error:
            raise BackendFailedError(f"无法执行 bluetoothctl：{error}") from error

    def _state(self, deadline):
        result = self._run("show", deadline=deadline)
        detail = (result.stderr or result.stdout or "").strip()
        if "No default controller available" in detail:
            return "unavailable"
        if result.returncode:
            raise BackendFailedError(detail or "bluetoothctl show 执行失败")
        for line in result.stdout.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() == "powered":
                if value.strip().lower() == "yes":
                    return "on"
                if value.strip().lower() == "no":
                    return "off"
        raise BackendFailedError("bluetoothctl 没有返回默认蓝牙控制器的电源状态")

    def state(self) -> str:
        return self._state(time.monotonic() + 30)

    def set(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise BackendInvalidError("蓝牙目标状态必须是布尔值")
        deadline = time.monotonic() + 30
        before = self._state(deadline)
        if before == "unavailable":
            raise BackendMissingError("未发现蓝牙无线电")
        target = "on" if enabled else "off"
        if before == target:
            return {"state": before, "changed": False, "method": "bluetoothctl"}
        result = self._run("power", target, deadline=deadline)
        if result.returncode:
            raise BackendFailedError((result.stderr or result.stdout or f"bluetoothctl power {target} 执行失败").strip())
        actual = before
        for _attempt in range(10):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendFailedError("等待蓝牙状态超过总时限（30s）")
            time.sleep(min(0.2, remaining))
            actual = self._state(deadline)
            if actual == target:
                return {"state": actual, "changed": True, "method": "bluetoothctl"}
        raise BackendFailedError(f"请求蓝牙切换到 {target}，回读状态仍为 {actual}")
