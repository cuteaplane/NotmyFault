import ctypes
import importlib.util
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from notmyfault.core.workflow import ActionCancellation, ActionCancelled

PKG_ROOT = Path(__file__).resolve().parents[1]

def load_plugin(ptype, name):
    filename = "action.py" if ptype == "actions" else "trigger.py"
    path = PKG_ROOT / ptype / name / filename
    spec = importlib.util.spec_from_file_location(f"scenario_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


# --------------------------------------------------------------- append_text


def test_append_text_appends_without_overwriting(tmp_path):
    mod = load_plugin("actions", "append_text")
    target = tmp_path / "log.txt"
    target.write_text("旧内容\n", encoding="utf-8")
    mod.run_with_context(
        {}, {"file_path": str(target), "text": "新内容", "add_timestamp": False}, {}
    )
    assert target.read_text(encoding="utf-8") == "旧内容\n新内容\n"


def test_append_text_writes_timestamped_lines(tmp_path):
    mod = load_plugin("actions", "append_text")
    target = tmp_path / "log.txt"
    mod.run_with_context(
        {},
        {"file_path": str(target), "text": "一行\n[已带标记]", "add_timestamp": True},
        {},
    )
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("[") and lines[0].endswith("] 一行")
    assert lines[1] == "[已带标记]"


# ------------------------------------------------------------- clipboard_clear


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 走剪贴板 API")
def test_clipboard_clear_windows_flow(monkeypatch):
    mod = load_plugin("actions", "clipboard_clear")
    user32 = MagicMock()
    user32.OpenClipboard.return_value = True
    user32.EmptyClipboard.return_value = True
    fake_ctypes = SimpleNamespace(
        windll=SimpleNamespace(user32=user32),
        c_void_p=ctypes.c_void_p,
        c_bool=ctypes.c_bool,
    )
    monkeypatch.setattr(mod, "ctypes", fake_ctypes)
    result = mod.run({}, {})
    assert result == {"cleared": True}
    user32.OpenClipboard.assert_called_once_with(None)
    user32.EmptyClipboard.assert_called_once()
    user32.CloseClipboard.assert_called_once()



# ------------------------------------------------------------ create_shortcut



@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 创建 lnk")
def test_create_shortcut_passes_values_via_env(monkeypatch):
    mod = load_plugin("actions", "create_shortcut")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="C:\\桌面\\t.lnk\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    result = mod.run(
        {},
        {
            "name": "快捷",
            "target_path": "C:\\x.exe",
            "arguments": "--fast",
            "working_directory": "C:\\",
            "icon_path": "C:\\i.ico",
            "location": "start_menu",
        },
    )
    assert result == {"shortcut_path": "C:\\桌面\\t.lnk"}
    env = captured["env"]
    assert env["NMF_LNK_NAME"] == "快捷"
    assert env["NMF_LNK_TARGET"] == "C:\\x.exe"
    assert env["NMF_LNK_ARGS"] == "--fast"
    assert env["NMF_LNK_WORKDIR"] == "C:\\"
    assert env["NMF_LNK_ICON"] == "C:\\i.ico"
    assert env["NMF_LNK_LOCATION"] == "start_menu"


# -------------------------------------------------------------- media_control


def _patch_media_user32(monkeypatch, mod):
    user32 = MagicMock()
    user32.GetForegroundWindow.return_value = 123
    user32.FindWindowW.return_value = 456
    fake_ctypes = SimpleNamespace(
        windll=SimpleNamespace(user32=user32),
        c_ssize_t=ctypes.c_ssize_t,
        c_ulong=ctypes.c_ulong,
        POINTER=lambda x: x,
        byref=lambda x: x,
    )
    monkeypatch.setattr(mod, "ctypes", fake_ctypes)
    return user32


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 发送 APPCOMMAND")
def test_media_control_dispatches_appcommand(monkeypatch):
    mod = load_plugin("actions", "media_control")
    user32 = _patch_media_user32(monkeypatch, mod)
    result = mod.run({}, {"command": "next"})
    assert result == {"command": "next"}
    calls = user32.SendMessageTimeoutW.call_args_list
    # 前台窗口和系统托盘各发一次，lparam 高 16 位是命令码
    assert len(calls) == 2
    assert calls[0].args[0] == 123
    assert calls[1].args[0] == 456
    for call in calls:
        assert call.args[1] == 0x0319
        assert call.args[3] == (11 << 16)


def test_media_control_rejects_unknown_command():
    mod = load_plugin("actions", "media_control")
    with pytest.raises(ValueError, match="未知媒体命令"):
        mod.run({}, {"command": "warp"})


# ------------------------------------------------------------------ open_url


def test_open_url_prepends_https_and_opens_all(monkeypatch):
    mod = load_plugin("actions", "open_url")
    opened = []
    monkeypatch.setattr(
        mod.webbrowser, "open", lambda url, new=0: opened.append(url) or True
    )
    result = mod.run({}, {"urls": "example.com\nhttps://foo.bar"})
    assert result == {"opened": 2}
    assert opened == ["https://example.com", "https://foo.bar"]


def test_open_url_requires_input():
    mod = load_plugin("actions", "open_url")
    with pytest.raises(ValueError, match="没有可打开的链接"):
        mod.run({}, {"urls": "  \n"})
    with pytest.raises(ValueError, match="只支持 http/https"):
        mod.run({}, {"urls": "file://C:/x"})


# ---------------------------------------------------------------- power_plan


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 支持 powercfg")
def test_power_plan_runs_powercfg(monkeypatch):
    mod = load_plugin("actions", "power_plan")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    result = mod.run({}, {"plan": "high_performance"})
    assert result["plan"] == "high_performance"
    assert captured["cmd"][:2] == ["powercfg", "/setactive"]
    assert captured["cmd"][2] == result["guid"]


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 支持 powercfg")
def test_power_plan_custom_guid_required():
    mod = load_plugin("actions", "power_plan")
    with pytest.raises(ValueError, match="必须提供 GUID"):
        mod.run({}, {"plan": "custom"})


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 支持 powercfg")
def test_power_plan_failure_raises(monkeypatch):
    mod = load_plugin("actions", "power_plan")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(RuntimeError, match="切换电源计划失败"):
        mod.run({}, {"plan": "balanced"})


# ------------------------------------------------------------------ send_keys


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用 SendInput ABI")
def test_send_keys_hotkey_order_and_modifiers(monkeypatch):
    mod = load_plugin("actions", "send_keys")
    sent = []
    monkeypatch.setattr(mod, "_send", sent.extend)
    mod.run({}, {"mode": "hotkey", "keys": "ctrl+shift+a"})
    sequence = [(inp.ki.wVk, inp.ki.dwFlags) for inp in sent]
    assert sequence == [
        (0x11, 0),
        (0x10, 0),
        (0x41, 0),
        (0x41, mod.KEYEVENTF_KEYUP),
        (0x10, mod.KEYEVENTF_KEYUP),
        (0x11, mod.KEYEVENTF_KEYUP),
    ]


# Windows 上 run() 走 SendInput，会把组合键真的按进系统
@pytest.mark.skipif(os.name != "posix", reason="仅 Linux 走 InputBackend")
def test_send_keys_linux_dispatches_input_tool(monkeypatch):
    mod = load_plugin("actions", "send_keys")
    calls = []

    class FakeInputBackend:
        def __init__(self):
            pass

        def type_text(self, text):
            calls.append(("type", text))

        def send_hotkey(self, parts):
            calls.append(("hotkey", list(parts)))

    monkeypatch.setattr(
        "notmyfault.platform.backends.InputBackend", FakeInputBackend
    )
    assert mod.run({}, {"mode": "hotkey", "keys": "ctrl+win+enter"}) == {
        "mode": "hotkey"
    }
    # 动作只负责拆分和校验，按键名翻译在 InputBackend 里
    assert calls[-1] == ("hotkey", ["ctrl", "win", "enter"])
    assert mod.run({}, {"mode": "type_text", "text": "a中😀"}) == {
        "mode": "type_text"
    }
    assert calls[-1] == ("type", "a中😀")



# ---------------------------------------------------------------- window_pin


def _patch_pin_user32(monkeypatch, mod, exstyle=0):
    user32 = MagicMock()
    user32.GetForegroundWindow.return_value = 777
    user32.GetWindowLongW.return_value = exstyle
    user32.SetWindowPos.return_value = 1
    monkeypatch.setattr(mod, "user32", user32)
    return user32


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用 user32 ABI")
def test_window_pin_toggle_pins_active_window(monkeypatch):
    mod = load_plugin("actions", "window_pin")
    user32 = _patch_pin_user32(monkeypatch, mod)
    result = mod.run({}, {"action": "toggle"})
    assert result == {"state": "pinned", "hwnd": 777}
    assert user32.SetWindowPos.call_args.args[1] == mod.HWND_TOPMOST


def test_window_pin_linux_dispatches_wmctrl(monkeypatch):
    mod = load_plugin("actions", "window_pin")
    calls = []

    class FakeWindowBackend:
        def __init__(self, runner):
            calls.append(runner)

        def set_pinned(self, action, target, title):
            calls.append((action, target, title))
            return {"state": "pinned", "window_id": "0x2a"}

    monkeypatch.setattr(mod, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(mod, "WindowBackend", FakeWindowBackend)
    assert mod.run({}, {"action": "pin", "target": "title", "title": "记事本"}) == {
        "state": "pinned",
        "window_id": "0x2a",
    }
    assert calls == [mod.default_runner, ("pin", "title", "记事本")]



# --------------------------------------------------------------- audio_device


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




# -------------------------------------------------------------- battery_level


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


# -------------------------------------------------------------- cron_schedule


def test_cron_schedule_daily_fires_once_per_day(monkeypatch):
    mod = load_plugin("triggers", "cron_schedule")
    clock = FakeClock(datetime(2026, 1, 1, 9, 0))
    monkeypatch.setattr(mod, "datetime", clock)
    trigger, events = make_trigger(
        mod, "CronScheduleTrigger", {"mode": "daily", "time": "08:00"}
    )
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




# -------------------------------------------------------------- session_lock


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




# ------------------------------------------------------------ system_startup


def test_system_startup_fires_once_after_delay(monkeypatch):
    mod = load_plugin("triggers", "system_startup")
    start = datetime(2026, 1, 1, 0, 0, 0)
    clock = FakeClock(start)
    monkeypatch.setattr(mod, "datetime", clock)
    trigger, events = make_trigger(mod, "SystemStartupTrigger", {"delay_seconds": 5})
    clock.moment = start + timedelta(seconds=4)
    trigger.poll()
    assert events == []
    clock.moment = start + timedelta(seconds=6)
    trigger.poll()
    trigger.poll()
    assert len(events) == 1
    assert events[0]["delay_seconds"] == 5.0




# -------------------------------------------------------------- wifi_network


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


def test_wifi_network_emits_on_leaving_target(monkeypatch):
    mod = load_plugin("triggers", "wifi_network")
    scripted(monkeypatch, mod, "_current_ssid", ["Home", ""])
    trigger, events = make_trigger(
        mod, "WifiNetworkTrigger", {"direction": "disconnected", "ssid": "Home"}
    )
    trigger.poll()
    trigger.poll()
    assert events == [
        {"ssid": "", "previous_ssid": "Home", "connected": False, "target": "Home"}
    ]




def test_shutdown_system_delay_can_be_cancelled_before_system_call():
    mod = load_plugin("actions", "shutdown_system")
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(ActionCancelled, match="动作已取消"):
        mod.run_with_context(
            {},
            {
                "action": "shutdown",
                "confirm": True,
                "delay_seconds": 3600,
            },
            {
                "runtime": {
                    "cancellation": ActionCancellation(cancel_event)
                }
            },
        )


def test_shutdown_system_rejects_non_boolean_force():
    mod = load_plugin("actions", "shutdown_system")

    with pytest.raises(ValueError, match="force 必须为布尔值"):
        mod.run_with_context(
            {},
            {"action": "shutdown", "confirm": True, "force": "false"},
            {"runtime": {"cancellation": ActionCancellation(threading.Event())}},
        )


def _sample_uia_selector():
    return {
        "version": 1,
        "window": {"process": "notepad.exe"},
        "target": {"name": "保存", "control_type": 50000},
    }


def test_uia_control_passes_selector_operation_and_cancellation(monkeypatch):
    mod = load_plugin("actions", "uia_control")
    calls = []
    cancellation = object()
    selector = _sample_uia_selector()
    monkeypatch.setattr(
        mod,
        "perform_selector",
        lambda target, operation, token, text: calls.append(
            (target, operation, token, text)
        ) or {"operation": operation},
    )

    result = mod.run_with_context(
        {},
        {"target": selector, "operation": "set_text", "text": "示例"},
        {"runtime": {"cancellation": cancellation}},
    )

    assert result == {"operation": "set_text"}
    assert calls == [(selector, "set_text", cancellation, "示例")]


def test_uia_wait_passes_selector_timeout_and_cancellation(monkeypatch):
    mod = load_plugin("actions", "uia_wait")
    calls = []
    cancellation = object()
    selector = _sample_uia_selector()
    monkeypatch.setattr(
        mod,
        "wait_for_selector",
        lambda target, timeout, token: calls.append(
            (target, timeout, token)
        ) or {"found": True},
    )

    result = mod.run_with_context(
        {},
        {"target": selector, "wait_seconds": 45},
        {"runtime": {"cancellation": cancellation}},
    )

    assert result == {"found": True}
    assert calls == [(selector, 45, cancellation)]


@pytest.mark.parametrize(
    ("plugin_id", "function_name", "expected"),
    [
        ("uia_focus_window", "focus_selector_window", {"focused": True}),
        ("uia_read_text", "read_selector_text", {"text": "示例"}),
    ],
)
def test_uia_window_and_text_actions_pass_cancellation(
    monkeypatch, plugin_id, function_name, expected
):
    mod = load_plugin("actions", plugin_id)
    calls = []
    cancellation = object()
    selector = _sample_uia_selector()
    monkeypatch.setattr(
        mod,
        function_name,
        lambda target, token: calls.append((target, token)) or expected,
    )

    result = mod.run_with_context(
        {},
        {"target": selector},
        {"runtime": {"cancellation": cancellation}},
    )

    assert result == expected
    assert calls == [(selector, cancellation)]
