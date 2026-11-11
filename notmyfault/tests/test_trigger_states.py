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

from notmyfault.tests.plugin_support import load_plugin, hosted_sampler


class FakeClock:
    def __init__(self, moment):
        self.moment = moment

    def time(self):
        return self.moment.timestamp()

    def monotonic(self):
        return 0.0

def make_trigger(module, cls_name, config, *, clock=None):
    events = []
    if cls_name in ("SessionLockTrigger", "WifiNetworkTrigger", "NetworkStatusTrigger", "ProcessStateTrigger", "IdleDetectTrigger", "BatteryLevelTrigger", "SystemResourceTrigger", "WindowTitleTrigger", "PowerStateTrigger"):
        from notmyfault.core.clock import FakeClock as HostClock
        trigger = hosted_sampler(module, cls_name, config, events, clock=HostClock())
    else:
        trigger = getattr(module, cls_name)({"id": "scenario"}, config, events.append, threading.Event())
    if clock is not None:
        trigger.clock = clock
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


@pytest.mark.parametrize("device_type,fail_at", [("render", 0), ("capture", 0), ("any", 0)])
def test_audio_device_emits_only_on_change(monkeypatch, device_type, fail_at):
    mod = load_plugin("triggers", "audio_device")
    devices = {"render": "A", "capture": "C"}
    monkeypatch.setattr(mod.platform.audio, "default_devices", lambda: dict(devices))
    events = []
    attempts = 0

    def emit(payload):
        nonlocal attempts
        attempts += 1
        if attempts == fail_at:
            raise RuntimeError("事件提交失败")
        events.append(payload)

    trigger = hosted_sampler(mod, "AudioDeviceTrigger", {"device_type": device_type}, events)
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


@pytest.mark.parametrize("percent", [88, None])
def test_power_state_resume_emits_once_and_stops_monitor(monkeypatch, percent):
    mod = load_plugin("triggers", "power_state")
    monitor = MagicMock()
    monitor.consume_resume.side_effect = [True, False]
    monkeypatch.setattr(mod.platform.power, "battery", lambda: {"on_battery": False, "percent": percent})
    monkeypatch.setattr(mod.platform.power, "resume_monitor", lambda: monitor)
    events = []
    trigger = hosted_sampler(mod, "PowerStateTrigger", {"state": "resume"}, events)
    trigger.validate()
    trigger.setup()
    trigger.poll()
    trigger.poll()
    assert events == [{"state": "resume", "battery_percent": percent}]
    trigger.teardown()
    monitor.stop.assert_called_once()


def test_power_state_low_battery_excludes_unknown_and_rearms_after_absence(monkeypatch):
    mod = load_plugin("triggers", "power_state")
    values = iter([
        {"on_battery": False, "percent": None},
        {"on_battery": True, "percent": None},
        {"on_battery": True, "percent": 19},
        {"on_battery": True, "percent": 22},
        {"on_battery": False, "percent": None},
        {"on_battery": True, "percent": 18},
    ])
    monkeypatch.setattr(mod.platform.power, "battery", lambda: next(values))
    trigger, events = make_trigger(mod, "PowerStateTrigger", {"state": "low_battery"})
    trigger.setup()
    for _ in range(6):
        trigger.poll()
    trigger.teardown()
    assert events == [
        {"state": "low_battery", "battery_percent": 19},
        {"state": "low_battery", "battery_percent": 18},
    ]




@pytest.mark.parametrize("mode,next_date", [("daily", 2), ("weekly", 8)])
def test_cron_schedule_fires_once_per_scheduled_date(tmp_path, mode, next_date):
    from notmyfault.core.state_store import StateStore, TriggerState
    from notmyfault.plugin_api import TriggerContext, use_trigger_context

    mod = load_plugin("triggers", "cron_schedule")
    clock = FakeClock(datetime(2026, 1, 1, 7, 59))
    config = {"mode": mode, "time": "08:00", "days": "4"}
    events = []
    path = tmp_path / "runtime.sqlite3"
    store = StateStore(path)

    def create_trigger():
        context = TriggerContext(
            {"id": "cron_schedule"}, config, events.append, threading.Event(),
            TriggerState(lambda: store, "cron_schedule"), "boot", clock.time(), clock,
        )
        with use_trigger_context(context):
            trigger = mod.CronScheduleTrigger.from_context(context)
        trigger.validate()
        return trigger

    try:
        trigger = create_trigger()
        trigger.poll()
        assert events == []
        clock.moment = datetime(2026, 1, 1, 8, 0, 5)
        trigger.poll()
        trigger.poll()
        assert len(events) == 1

        assert store.close()
        store = StateStore(path)
        clock.moment = datetime(2026, 1, 1, 8, 0, 30)
        restarted = create_trigger()
        restarted.poll()
        assert len(events) == 1

        clock.moment = datetime(2026, 1, next_date, 9, 0)
        restarted.poll()
        restarted.poll()
        assert len(events) == 2
        assert all(event["mode"] == mode for event in events)
    finally:
        assert store.close()


@pytest.mark.parametrize("interval_minutes", [1, 30, 1440])
@pytest.mark.parametrize("wall_shift", [timedelta(), timedelta(hours=1), timedelta(hours=-1), timedelta(days=1), timedelta(days=-1)])
def test_cron_schedule_interval_fires_every_n_minutes(monkeypatch, interval_minutes, wall_shift):
    mod = load_plugin("triggers", "cron_schedule")
    start = datetime(2026, 1, 1, 0, 0)
    clock = FakeClock(start)
    elapsed = {"seconds": 0.0}
    clock.monotonic = lambda: 1000.0 + elapsed["seconds"]
    trigger, events = make_trigger(
        mod, "CronScheduleTrigger", {"mode": "interval", "interval_minutes": interval_minutes}, clock=clock
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
    trigger, events = make_trigger(
        mod, "CronScheduleTrigger", {"mode": "weekly", "time": "00:00", "days": "1"}, clock=clock
    )
    trigger.poll()
    assert events == []
    clock.moment = datetime(2026, 1, 12, 9, 0)
    trigger.poll()
    assert len(events) == 1
    assert events[0]["mode"] == "weekly"


def test_session_lock_emits_after_debounce(monkeypatch):
    mod = load_plugin("triggers", "session_lock")
    scripted(monkeypatch, mod.platform.session, "is_locked", [False, True, True])
    trigger, events = make_trigger(mod, "SessionLockTrigger", {})
    trigger.poll()
    trigger.poll()
    assert events == []
    trigger.host_clock.advance(5)
    trigger.poll()
    assert events == [{"state": "locked", "previous_state": "unlocked"}]


def test_session_lock_debounce_reverts_before_confirmation(monkeypatch):
    mod = load_plugin("triggers", "session_lock")
    scripted(monkeypatch, mod.platform.session, "is_locked", [False, True, False])
    trigger, events = make_trigger(mod, "SessionLockTrigger", {})
    trigger.poll()
    trigger.poll()
    trigger.poll()
    assert events == []


def test_session_lock_filters_by_target_state(monkeypatch):
    mod = load_plugin("triggers", "session_lock")
    scripted(monkeypatch, mod.platform.session, "is_locked", [True, False, False, True, True])
    trigger, events = make_trigger(mod, "SessionLockTrigger", {"state": "locked"})
    trigger.poll()
    trigger.poll()
    trigger.host_clock.advance(5)
    trigger.poll()
    assert events == []
    trigger.poll()
    trigger.host_clock.advance(5)
    trigger.poll()
    assert events == [{"state": "locked", "previous_state": "unlocked"}]


def test_system_startup_fires_once_after_delay(monkeypatch):
    mod = load_plugin("triggers", "system_startup")
    start = datetime(2026, 1, 1, 0, 0, 0)
    clock = FakeClock(start)
    ticks = [0]
    clock.monotonic = lambda: ticks[0]
    trigger, events = make_trigger(mod, "SystemStartupTrigger", {"delay_seconds": 5}, clock=clock)
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


@pytest.mark.parametrize("ssid", ["Home", '"家中:WiFi"', r"Lab\WiFi", "Cafe:WiFi", 'Home"', " Home ", " ", "\u3000Home\u3000"])
def test_wifi_network_emits_on_target_connect(monkeypatch, ssid):
    mod = load_plugin("triggers", "wifi_network")
    scripted(monkeypatch, mod.platform.wifi, "current_ssid", [None, ssid])
    trigger, events = make_trigger(
        mod, "WifiNetworkTrigger", {"direction": "connected", "ssid": ssid}
    )
    trigger.poll()
    trigger.poll()
    assert events == [
        {"ssid": ssid, "previous_ssid": "", "connected": True, "target": ssid}
    ]


@pytest.mark.parametrize("next_ssid", [None, "Office"])
def test_wifi_network_emits_on_leaving_target(monkeypatch, next_ssid):
    mod = load_plugin("triggers", "wifi_network")
    scripted(monkeypatch, mod.platform.wifi, "current_ssid", ["Home", next_ssid])
    trigger, events = make_trigger(
        mod, "WifiNetworkTrigger", {"direction": "disconnected", "ssid": "Home"}
    )
    trigger.poll()
    trigger.poll()
    assert events == [
        {"ssid": next_ssid or "", "previous_ssid": "Home", "connected": bool(next_ssid), "target": "Home"}
    ]
