import ctypes
import os
import ntpath
import posixpath
import shutil
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from notmyfault.tests.plugin_support import load_plugin


class FakeClock:
    def __init__(self, moment):
        self.moment = moment

    def now(self):
        return self.moment

def make_trigger(module, cls_name, config):
    events = []
    trigger = getattr(module, cls_name)(
        {"id": "scenario"}, config, events.append, threading.Event()
    )
    trigger.validate()
    return trigger, events

def scripted(monkeypatch, module, name, results):
    values = iter(results)
    monkeypatch.setattr(module, name, lambda: next(values))

def set_battery(monkeypatch, mod, percent, plugged):
    monkeypatch.setattr(
        mod.psutil,
        "sensors_battery",
        lambda: SimpleNamespace(percent=percent, power_plugged=plugged),
    )


@pytest.mark.parametrize("device_type,fail_at", [("render", 0), ("capture", 0), ("any", 0), ("any", 1), ("any", 2)])
def test_audio_device_emits_only_on_change(monkeypatch, device_type, fail_at):
    mod = load_plugin("triggers", "audio_device")
    devices = {"render": "A", "capture": "C"}
    monkeypatch.setattr(mod, "_query_default_devices", lambda: dict(devices))
    events = []
    attempts = 0

    def emit(payload):
        nonlocal attempts
        attempts += 1
        if attempts == fail_at:
            raise RuntimeError("事件提交失败")
        events.append(payload)

    trigger = mod.AudioDeviceTrigger(
        {"id": "audio_device"}, {"device_type": device_type}, emit, threading.Event(),
    )
    trigger.validate()
    trigger.poll()
    trigger.poll()
    assert events == []
    devices.update(render="B", capture="D")
    if fail_at:
        with pytest.raises(RuntimeError):
            trigger.poll()
    trigger.poll()
    trigger.poll()
    selected = [flow for flow in ("render", "capture") if device_type in (flow, "any")]
    assert [event["device_type"] for event in events] == selected
    assert [event["device_id"] for event in events] == [devices[flow] for flow in selected]
    assert [event["previous_device_id"] for event in events] == [{"render": "A", "capture": "C"}[flow] for flow in selected]


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("target,fail_at", [("ANY", 0), ("ANY", 1), ("ANY", 2), ("first", 0), ("first", 1), ("second", 0), ("second", 1)])
def test_usb_reports_new_devices_once_and_detects_reinsertion(monkeypatch, platform, target, fail_at):
    mod = load_plugin("triggers", "usb_insert")
    monkeypatch.setattr(mod, "os", SimpleNamespace(name=platform, path=ntpath if platform == "nt" else posixpath))
    old, first, second = ("D:", "E:", "F:") if platform == "nt" else ("/media/old", "/media/first ", "/media/second\u3000")
    expected_drive = {"ANY": "ANY", "first": first, "second": second}[target]
    sample = {"drives": {old}}

    def read_drives():
        if isinstance(sample["drives"], Exception):
            raise sample["drives"]
        return set(sample["drives"])

    monkeypatch.setattr(mod.UsbInsertTrigger, "_get_removable_drives", staticmethod(read_drives))
    events = []
    attempts = 0

    def emit(payload):
        nonlocal attempts
        attempts += 1
        if attempts == fail_at:
            raise RuntimeError("事件提交失败")
        events.append(payload)

    trigger = mod.UsbInsertTrigger(
        {"id": "usb_insert"}, {"drive_letter": expected_drive}, emit, threading.Event(),
    )
    trigger.validate()
    trigger.setup()
    trigger.poll()
    assert events == []
    sample["drives"] = {first, second}
    if fail_at:
        with pytest.raises(RuntimeError):
            trigger.poll()
    else:
        trigger.poll()
    accepted = len(events)
    sample["drives"] = PermissionError("设备状态读取失败")
    with pytest.raises(PermissionError):
        trigger.poll()
    assert len(events) == accepted
    sample["drives"] = {old, first, second}
    trigger.poll()
    trigger.poll()
    selected = {old, first, second} if target == "ANY" else {expected_drive}
    assert len({event["actual_drive"] for event in events}) == len(events)
    assert {event["actual_drive"] for event in events} == selected
    assert len(events) == len(selected)
    assert all(event["drive_letter"] == expected_drive for event in events)
    sample["drives"] = set()
    trigger.poll()
    sample["drives"] = {first, second}
    trigger.poll()
    trigger.poll()
    assert len(events) == len(selected) + (2 if target == "ANY" else 1)


def test_battery_level_fires_once_with_hysteresis(monkeypatch):
    mod = load_plugin("triggers", "battery_level")
    set_battery(monkeypatch, mod, 19, False)
    trigger, events = make_trigger(
        mod, "BatteryLevelTrigger", {"threshold": 20, "direction": "below"}
    )
    trigger.poll()
    assert len(events) == 1
    assert events[0]["battery_percent"] == 19
    trigger.poll()
    assert len(events) == 1
    # 回到阈值加迟滞距离以上才重新允许触发
    set_battery(monkeypatch, mod, 26, False)
    trigger.poll()
    assert len(events) == 1
    set_battery(monkeypatch, mod, 19, False)
    trigger.poll()
    assert len(events) == 2


def test_battery_level_respects_charge_state_filter(monkeypatch):
    mod = load_plugin("triggers", "battery_level")
    set_battery(monkeypatch, mod, 10, False)
    trigger, events = make_trigger(
        mod, "BatteryLevelTrigger", {"threshold": 20, "charge_state": "charging"}
    )
    trigger.poll()
    assert events == []
    set_battery(monkeypatch, mod, 10, True)
    trigger.poll()
    assert len(events) == 1
    assert events[0]["state"] == "charging"


@pytest.mark.parametrize(
    "config",
    [
        {"direction": "sideways"},
        {"charge_state": "full"},
        {"threshold": "abc"},
        {"threshold": -1},
        {"threshold": 101},
    ],
)
def test_battery_level_validate_rejects_bad_config(config):
    mod = load_plugin("triggers", "battery_level")
    trigger = mod.BatteryLevelTrigger(
        {"id": "battery_level"}, config, lambda e: None, threading.Event()
    )
    with pytest.raises(ValueError):
        trigger.validate()


def test_power_state_creates_message_window_only_for_resume(monkeypatch):
    mod = load_plugin("triggers", "power_state")
    window = object()
    created = []
    monkeypatch.setattr(mod, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(mod, "_is_on_battery", lambda: (False, 100))
    monkeypatch.setattr(
        mod,
        "_create_power_event_window",
        lambda: created.append(True) or window,
    )

    ac_trigger = mod.PowerStateTrigger(
        {"id": "power_state"},
        {"state": "ac"},
        lambda payload: None,
        threading.Event(),
    )
    ac_trigger.setup()
    resume_trigger = mod.PowerStateTrigger(
        {"id": "power_state"},
        {"state": "resume"},
        lambda payload: None,
        threading.Event(),
    )
    resume_trigger.setup()

    assert ac_trigger.power_window is None
    assert resume_trigger.power_window is window
    assert created == [True]
    resume_trigger.validate()


def test_power_state_resume_uses_linux_monitor(monkeypatch):
    mod = load_plugin("triggers", "power_state")
    monitor = MagicMock()
    monitor.consume_resume.return_value = True
    monkeypatch.setattr(mod, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(mod, "_is_on_battery", lambda: (False, 88))
    monkeypatch.setattr(mod, "_create_linux_resume_monitor", lambda: monitor)
    events = []
    trigger = mod.PowerStateTrigger(
        {"id": "power_state"},
        {"state": "resume"},
        events.append,
        threading.Event(),
    )
    trigger.validate()
    trigger.setup()
    assert trigger.power_window is None
    assert trigger.resume_monitor is monitor
    trigger.poll()
    assert events == [{"state": "resume", "battery_percent": 88}]
    trigger.teardown()
    monitor.stop.assert_called_once()


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用 GetSystemPowerStatus")
@pytest.mark.parametrize("success,ac,flags,percent,expected", [
    (0, 0, 0, 0, None), (1, 255, 255, 255, None),
    (1, 1, 255, 255, None), (1, 1, 128, 255, (False, 100)),
    (1, 0, 1, 20, (True, 20)),
])
def test_power_read_preserves_unknown_state(monkeypatch, success, ac, flags, percent, expected):
    mod = load_plugin("triggers", "power_state")
    def query(status):
        status[0], status[1], status[2] = ac, flags, percent
        return success
    monkeypatch.setattr(mod.ctypes.windll.kernel32, "GetSystemPowerStatus", query)
    if expected is None:
        with pytest.raises(RuntimeError):
            mod._is_on_battery()
    else:
        assert mod._is_on_battery() == expected


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 创建电源消息窗口")
def test_power_state_keeps_native_callback_until_window_is_destroyed(monkeypatch):
    mod = load_plugin("triggers", "power_state")
    user32 = MagicMock()
    kernel32 = MagicMock()
    user32.RegisterClassW.return_value = 1
    user32.CreateWindowExW.return_value = 99
    user32.DestroyWindow.return_value = 1
    user32.UnregisterClassW.return_value = 1
    kernel32.GetModuleHandleW.return_value = 88
    kernel32.GetCurrentThreadId.return_value = 123
    monkeypatch.setattr(mod.ctypes.windll, "user32", user32)
    monkeypatch.setattr(mod.ctypes.windll, "kernel32", kernel32)

    window = mod._create_power_event_window()

    def peek_message(message, *_args):
        ctypes.POINTER(mod.wintypes.MSG).from_param(message)
        return 0

    user32.PeekMessageW.side_effect = peek_message
    window["wnd_proc"](99, mod.WM_POWERBROADCAST, mod.PBT_APMRESUMEAUTOMATIC, 0)
    assert mod._pump_power_messages(window) is True

    def destroy_window(hwnd):
        assert window["wnd_proc"] in mod._WND_PROC_HOLD
        assert window["wnd_proc"](hwnd, mod.WM_POWERBROADCAST, mod.PBT_APMRESUMEAUTOMATIC, 0) == 0
        return 1

    user32.DestroyWindow.side_effect = destroy_window
    mod._destroy_power_event_window(window)
    assert window["state"]["resume"] is True
    user32.DestroyWindow.assert_called_once_with(99)
    user32.UnregisterClassW.assert_called_once_with(
        window["class_name"], 88
    )
    assert window["wnd_proc"] not in mod._WND_PROC_HOLD


def test_cron_schedule_daily_fires_once_per_day(monkeypatch):
    mod = load_plugin("triggers", "cron_schedule")
    clock = FakeClock(datetime(2026, 1, 1, 7, 59))
    monkeypatch.setattr(mod, "datetime", clock)
    trigger, events = make_trigger(
        mod, "CronScheduleTrigger", {"mode": "daily", "time": "08:00"}
    )
    trigger.poll()
    assert events == []
    clock.moment = datetime(2026, 1, 1, 9, 0)
    trigger.poll()
    trigger.poll()
    assert len(events) == 1
    clock.moment = datetime(2026, 1, 2, 8, 0)
    trigger.poll()
    assert len(events) == 2
    assert all(e["mode"] == "daily" for e in events)


@pytest.mark.parametrize("interval_minutes", [1, 30, 1440])
@pytest.mark.parametrize("wall_shift", [timedelta(), timedelta(hours=1), timedelta(hours=-1), timedelta(days=1), timedelta(days=-1)])
def test_cron_schedule_interval_fires_every_n_minutes(monkeypatch, interval_minutes, wall_shift):
    mod = load_plugin("triggers", "cron_schedule")
    start = datetime(2026, 1, 1, 0, 0)
    clock = FakeClock(start)
    monkeypatch.setattr(mod, "datetime", clock)
    elapsed = {"seconds": 0.0}
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: 1000.0 + elapsed["seconds"]))
    trigger, events = make_trigger(
        mod, "CronScheduleTrigger", {"mode": "interval", "interval_minutes": interval_minutes}
    )
    trigger.poll()
    assert events == []
    elapsed["seconds"] = interval_minutes * 60 - 1
    clock.moment = start + timedelta(seconds=elapsed["seconds"]) + wall_shift
    trigger.poll()
    assert events == []
    elapsed["seconds"] += 1
    clock.moment = start + timedelta(seconds=elapsed["seconds"]) + wall_shift
    trigger.poll()
    assert len(events) == 1
    assert events[0] == {"mode": "interval", "triggered_at": clock.moment.strftime("%Y-%m-%d %H:%M:%S")}
    trigger.poll()
    assert len(events) == 1
    elapsed["seconds"] += interval_minutes * 60 - 1
    clock.moment = start + timedelta(seconds=elapsed["seconds"]) - wall_shift
    trigger.poll()
    assert len(events) == 1
    elapsed["seconds"] += 1
    clock.moment = start + timedelta(seconds=elapsed["seconds"]) - wall_shift
    trigger.poll()
    assert len(events) == 2
    elapsed["seconds"] += interval_minutes * 60 * 3
    clock.moment = start + timedelta(seconds=elapsed["seconds"])
    trigger.poll()
    trigger.poll()
    assert len(events) == 3


def test_cron_schedule_weekly_respects_weekdays(monkeypatch):
    mod = load_plugin("triggers", "cron_schedule")
    # 2026-01-06 是周二，2026-01-12 是周一；days="1" 只允许周一
    clock = FakeClock(datetime(2026, 1, 6, 9, 0))
    monkeypatch.setattr(mod, "datetime", clock)
    trigger, events = make_trigger(
        mod, "CronScheduleTrigger", {"mode": "weekly", "time": "00:00", "days": "1"}
    )
    trigger.poll()
    assert events == []
    clock.moment = datetime(2026, 1, 12, 9, 0)
    trigger.poll()
    assert len(events) == 1
    assert events[0]["mode"] == "weekly"


def test_session_lock_emits_after_debounce(monkeypatch):
    mod = load_plugin("triggers", "session_lock")
    scripted(monkeypatch, mod, "_is_locked", [False, True, True])
    trigger, events = make_trigger(mod, "SessionLockTrigger", {})
    trigger.poll()
    trigger.poll()
    assert events == []
    trigger.poll()
    assert events == [{"state": "locked", "previous_state": "unlocked"}]


def test_session_lock_debounce_reverts_before_confirmation(monkeypatch):
    mod = load_plugin("triggers", "session_lock")
    scripted(monkeypatch, mod, "_is_locked", [False, True, False])
    trigger, events = make_trigger(mod, "SessionLockTrigger", {})
    trigger.poll()
    trigger.poll()
    trigger.poll()
    assert events == []


def test_session_lock_filters_by_target_state(monkeypatch):
    mod = load_plugin("triggers", "session_lock")
    scripted(monkeypatch, mod, "_is_locked", [True, False, False, True, True])
    trigger, events = make_trigger(mod, "SessionLockTrigger", {"state": "locked"})
    trigger.poll()
    trigger.poll()
    trigger.poll()
    assert events == []
    trigger.poll()
    trigger.poll()
    assert events == [{"state": "locked", "previous_state": "unlocked"}]


def test_system_startup_fires_once_after_delay(monkeypatch):
    mod = load_plugin("triggers", "system_startup")
    start = datetime(2026, 1, 1, 0, 0, 0)
    clock = FakeClock(start)
    monkeypatch.setattr(mod, "datetime", clock)
    ticks = [0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: ticks[0])
    trigger, events = make_trigger(mod, "SystemStartupTrigger", {"delay_seconds": 5})
    clock.moment = start + timedelta(seconds=4)
    ticks[0] = 4
    trigger.poll()
    assert events == []
    clock.moment = start + timedelta(seconds=6)
    ticks[0] = 6
    trigger.poll()
    trigger.poll()
    assert len(events) == 1
    assert events[0]["delay_seconds"] == 5.0


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("ssid", ["Home", '"家中:WiFi"', r"Lab\WiFi", "Cafe:WiFi", 'Home"', " Home ", " ", "\u3000Home\u3000"])
@pytest.mark.parametrize("locale_env", [
    {"LANG": "C", "LC_ALL": "C"}, {"LANG": "zh_CN.UTF-8"},
    {"LANG": "C", "LC_ALL": "zh_CN.UTF-8"},
])
def test_wifi_network_emits_on_target_connect(monkeypatch, platform, ssid, locale_env):
    from notmyfault.platform import windows_wifi

    mod = load_plugin("triggers", "wifi_network")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    for key, value in locale_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setattr(mod, "os", SimpleNamespace(name=platform, environ=os.environ))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nmcli")
    states = iter(["", ssid])
    monkeypatch.setattr(windows_wifi, "current_ssid", lambda: next(states))

    def query(*args, **kwargs):
        assert platform == "posix"
        current = next(states)
        encoded = current.replace("\\", "\\\\").replace(":", "\\:")
        environment = kwargs.get("env", os.environ)
        english = (environment.get("LC_ALL") or environment.get("LANG")) == "C"
        active = "yes" if english else "是"
        inactive = "no" if english else "否"
        output = f"{active}:{encoded}\n" if current else f"{inactive}:Other\n"
        assert environment["DBUS_SESSION_BUS_ADDRESS"] == os.environ["DBUS_SESSION_BUS_ADDRESS"]
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(mod.subprocess, "run", query)
    trigger, events = make_trigger(
        mod, "WifiNetworkTrigger", {"direction": "connected", "ssid": ssid}
    )
    trigger.poll()
    trigger.poll()
    assert events == [
        {"ssid": ssid, "previous_ssid": "", "connected": True, "target": ssid}
    ]
    assert dict(os.environ).get("LC_ALL") == locale_env.get("LC_ALL")


@pytest.mark.parametrize("next_ssid", ["", "Office"])
def test_wifi_network_emits_on_leaving_target(monkeypatch, next_ssid):
    mod = load_plugin("triggers", "wifi_network")
    scripted(monkeypatch, mod, "_current_ssid", ["Home", next_ssid])
    trigger, events = make_trigger(
        mod, "WifiNetworkTrigger", {"direction": "disconnected", "ssid": "Home"}
    )
    trigger.poll()
    trigger.poll()
    assert events == [
        {"ssid": next_ssid, "previous_ssid": "Home", "connected": bool(next_ssid), "target": "Home"}
    ]
