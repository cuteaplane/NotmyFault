from __future__ import annotations

import asyncio
import threading
from contextlib import suppress

from notmyfault.platform._run import CommandRunner, default_runner, failed_detail
from notmyfault.platform.errors import (
    BackendFailedError,
    BackendInvalidError,
    BackendMissingError,
)
from notmyfault.platform.linux.session import SessionBackend


_LINUX_POWER_PROFILES = {
    "balanced": "balanced",
    "high_performance": "performance",
    "power_saver": "power-saver",
}


class PowerBackend:

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def set_profile(self, plan: str, custom_guid: str = "") -> str:
        if plan == "custom":
            raise BackendInvalidError(
                "Linux 不支持自定义 GUID 电源计划，请改用 balanced/high_performance/power_saver"
            )
        profile = _LINUX_POWER_PROFILES.get(plan)
        if profile is None:
            raise BackendInvalidError(
                f"未知电源计划: {plan!r}"
                "（可选: balanced/high_performance/power_saver）"
            )
        path = self._runner.which("powerprofilesctl")
        if not path:
            raise BackendMissingError("依赖缺失：电源计划需要 powerprofilesctl")
        result = self._runner.run([path, "set", profile], timeout=10)
        if result.returncode != 0:
            raise BackendFailedError(failed_detail(result))
        return profile

    def battery(self) -> dict:
        try:
            import psutil

            battery = psutil.sensors_battery()
        except ImportError as error:
            raise BackendMissingError("依赖缺失：电池查询需要 psutil") from error
        except OSError as error:
            raise BackendFailedError(f"读取电池状态失败: {error}") from error
        if battery is None:
            return {"on_battery": False, "percent": None}
        return {"on_battery": not battery.power_plugged, "percent": round(battery.percent)}

    def resume_monitor(self):
        return SleepResumeBackend()

    def system(self, action: str, force: bool = False) -> None:
        commands = {
            "shutdown": "poweroff", "restart": "reboot",
            "sleep": "suspend", "hibernate": "hibernate",
        }
        if action not in (*commands, "logoff"):
            raise BackendInvalidError(f"不支持的系统电源操作: {action!r}")
        if not isinstance(force, bool):
            raise BackendInvalidError("force 必须为布尔值")
        executable = self._runner.which("loginctl" if action == "logoff" else "systemctl")
        if not executable:
            raise BackendMissingError("依赖缺失：系统电源操作需要 loginctl 或 systemctl")
        if action == "logoff":
            session = SessionBackend(self._runner).session_id()
            if not session:
                raise BackendFailedError("当前系统无法获取会话 ID，不能注销")
            args = [executable, "terminate-session", session]
        else:
            args = [executable, commands[action]]
            if force:
                args.append("--ignore-inhibitors")
        result = self._runner.run(args, timeout=15)
        if result.returncode != 0:
            raise BackendFailedError(failed_detail(result))


class SleepResumeBackend:
    """监听 logind PrepareForSleep(False)，在睡眠恢复时置位"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resumed = False
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            import dbus_next  # noqa: F401
        except ImportError as error:
            raise BackendMissingError("依赖缺失：睡眠恢复监听需要 dbus-next") from error
        self._stop.clear()
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run, name="nmf-sleep-resume", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            self.stop()
            raise BackendFailedError("连接 logind 睡眠恢复信号超时")
        if self._start_error is not None:
            error = self._start_error
            self.stop()
            raise BackendFailedError(f"无法监听睡眠恢复: {error}") from error

    def consume_resume(self) -> bool:
        with self._lock:
            value = self._resumed
            self._resumed = False
            return value

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None
        self._loop = None

    def _mark_resumed(self) -> None:
        with self._lock:
            self._resumed = True

    def _run(self) -> None:
        try:
            asyncio.run(self._watch())
        except Exception as error:
            self._start_error = error
            self._ready.set()

    async def _watch(self) -> None:
        from dbus_next import BusType, Message, MessageType
        from dbus_next.aio import MessageBus

        self._loop = asyncio.get_running_loop()
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            match_rule = (
                "type='signal',"
                "sender='org.freedesktop.login1',"
                "interface='org.freedesktop.login1.Manager',"
                "member='PrepareForSleep',"
                "path='/org/freedesktop/login1'"
            )

            def on_message(message: Message) -> bool:
                if (
                    message.message_type == MessageType.SIGNAL
                    and message.interface == "org.freedesktop.login1.Manager"
                    and message.member == "PrepareForSleep"
                    and message.body
                    and message.body[0] is False
                ):
                    self._mark_resumed()
                    return True
                return False

            bus.add_message_handler(on_message)
            match_reply = await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[match_rule],
                )
            )
            if match_reply.message_type == MessageType.ERROR:
                raise BackendFailedError("无法订阅 logind PrepareForSleep 信号")
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.2)
        finally:
            with suppress(Exception):
                bus.disconnect()
