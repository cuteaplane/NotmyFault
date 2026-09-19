import ctypes
import os
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


def test_audio_device_emits_only_on_change(monkeypatch):
    mod = load_plugin("triggers", "audio_device")
    scripted(
        monkeypatch,
        mod,
        "_query_default_devices",
        [
            {"render": "A", "capture": "C"},
            {"render": "A", "capture": "C"},
            {"render": "B", "capture": "C"},
        ],
    )
    trigger, events = make_trigger(
        mod, "AudioDeviceTrigger", {"device_type": "render"}
    )
    trigger.poll()
    trigger.poll()
    assert events == []
    trigger.poll()
    assert len(events) == 1
    assert events[0]["device_type"] == "render"
    assert events[0]["device_id"] == "B"
    assert events[0]["previous_device_id"] == "A"


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
    monkeypatch.setattr(mod, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ValueError, match="仅支持 Windows"):
        resume_trigger.validate()


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


def test_cron_schedule_interval_fires_every_n_minutes(monkeypatch):
    mod = load_plugin("triggers", "cron_schedule")
    start = datetime(2026, 1, 1, 0, 0)
    clock = FakeClock(start)
    monkeypatch.setattr(mod, "datetime", clock)
    trigger, events = make_trigger(
        mod, "CronScheduleTrigger", {"mode": "interval", "interval_minutes": 30}
    )
    trigger.poll()
    assert events == []
    clock.moment = start + timedelta(minutes=29)
    trigger.poll()
    assert events == []
    clock.moment = start + timedelta(minutes=31)
    trigger.poll()
    assert len(events) == 1
    clock.moment = start + timedelta(minutes=62)
    trigger.poll()
    assert len(events) == 2


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


def test_wifi_network_emits_on_target_connect(monkeypatch):
    mod = load_plugin("triggers", "wifi_network")
    scripted(monkeypatch, mod, "_current_ssid", ["", "Home"])
    trigger, events = make_trigger(
        mod, "WifiNetworkTrigger", {"direction": "connected", "ssid": "Home"}
    )
    trigger.poll()
    trigger.poll()
    assert events == [
        {"ssid": "Home", "previous_ssid": "", "connected": True, "target": "Home"}
    ]


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
