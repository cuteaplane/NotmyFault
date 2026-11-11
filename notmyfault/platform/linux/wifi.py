from __future__ import annotations

import os
import re
import tempfile
import time
import uuid

from notmyfault.platform._run import default_runner
from notmyfault.platform.errors import (
    BackendError, BackendFailedError, BackendInvalidError, BackendMissingError,
)


class WifiBackend:
    def __init__(self, runner=None):
        self._runner = runner or default_runner

    def _run(self, *args, timeout=30):
        executable = self._runner.which("nmcli")
        if not executable:
            raise BackendMissingError("未安装 nmcli")
        try:
            result = self._runner.run(
                [executable, *args], timeout=timeout,
                env={**os.environ, "LC_ALL": "C"},
            )
        except OSError as error:
            raise BackendFailedError(f"无法执行 nmcli：{error}") from error
        if result.returncode:
            raise BackendFailedError(
                (result.stderr or result.stdout or "nmcli 执行失败").strip()
            )
        return result.stdout

    def current_ssid(self, interface=None) -> str | None:
        args = ["-t", "-f", "ACTIVE,SSID", "dev", "wifi"]
        if interface is not None:
            args.extend(["ifname", interface])
        output = self._run(*args, timeout=10)
        for line in output.splitlines():
            if line.startswith("yes:"):
                return re.sub(r"\\([\\:])", r"\1", line[4:]) or None
        return None

    def _device(self, interface):
        if interface is not None:
            return interface
        output = self._run("-t", "-f", "DEVICE,TYPE,STATE", "device", "status", timeout=10)
        for line in output.splitlines():
            parts = re.split(r"(?<!\\):", line)
            if len(parts) >= 2 and parts[1] == "wifi":
                return re.sub(r"\\([\\:])", r"\1", parts[0])
        raise BackendMissingError("未发现 WiFi 网卡")

    def _saved_uuid(self, ssid):
        output = self._run("-t", "-f", "UUID,TYPE", "connection", "show", timeout=10)
        for line in output.splitlines():
            connection_uuid, _, conn_type = line.rpartition(":")
            if not connection_uuid or conn_type not in ("802-11-wireless", "wifi"):
                continue
            profile = self._run(
                "-g", "802-11-wireless.ssid", "-e", "no", "connection", "show",
                "uuid", connection_uuid, timeout=10,
            )
            if profile.rstrip("\r\n") == ssid:
                return connection_uuid
        return None

    def _wait(self, expected, interface):
        deadline = time.monotonic() + 20
        actual = None
        while time.monotonic() < deadline:
            actual = self.current_ssid(interface)
            if actual == expected:
                return
            time.sleep(0.4)
        raise BackendFailedError(f"请求 WiFi 状态 {expected!r}，回读为 {actual!r}")

    def connect(self, ssid, password=None, interface=None) -> dict:
        if not isinstance(ssid, str) or not ssid:
            raise BackendInvalidError("连接 WiFi 时必须填写目标 SSID")
        if password is not None and not isinstance(password, str):
            raise BackendInvalidError("WiFi 密码必须是字符串")
        if password and ("\n" in password or "\r" in password):
            raise BackendInvalidError("WiFi 密码不能包含换行")
        try:
            return self._connect(ssid, password, interface)
        except BackendError as error:
            if password:
                raise type(error)("WiFi 密码连接操作失败，详细输出已隐藏") from None
            raise
        except (OSError, UnicodeError) as error:
            if password:
                raise BackendFailedError("WiFi 密码连接操作失败，详细输出已隐藏") from None
            raise BackendFailedError(f"WiFi 连接失败：{error}") from error

    def _connect(self, ssid, password, interface):
        device = self._device(interface)
        if self.current_ssid(device) == ssid:
            return {"ssid": ssid, "connected": True, "method": "nmcli"}
        connection_uuid = self._saved_uuid(ssid)
        if connection_uuid is None and password:
            connection_uuid = str(uuid.uuid4())
            self._run(
                "connection", "add", "type", "wifi", "con-name", f"NotmyFault-{connection_uuid}",
                "ifname", device, "ssid", ssid, "connection.uuid", connection_uuid,
                "wifi-sec.key-mgmt", "wpa-psk",
            )
        if connection_uuid is not None:
            args = ["connection", "up", "uuid", connection_uuid, "ifname", device]
            if password:
                descriptor, path = tempfile.mkstemp(prefix="notmyfault-wifi-", suffix=".passwd")
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(f"wifi.psk:{password}\n")
                    self._run(*args, "passwd-file", path)
                finally:
                    os.unlink(path)
            else:
                self._run(*args)
        else:
            self._run("device", "wifi", "connect", ssid, "ifname", device)
        self._wait(ssid, device)
        return {"ssid": ssid, "connected": True, "method": "nmcli"}

    def disconnect(self, interface=None) -> dict:
        device = self._device(interface)
        if self.current_ssid(device) is not None:
            self._run("device", "disconnect", device)
            self._wait(None, device)
        return {"ssid": "", "connected": False, "method": "nmcli"}
