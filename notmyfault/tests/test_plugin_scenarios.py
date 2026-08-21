"""内置插件场景：8 个动作与 6 个触发器的行为、校验和元数据"""

import ctypes
import importlib.util
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from notmyfault.core.workflow import ActionCancellation, ActionCancelled
from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
from notmyfault.security.plugin_schema import current_platform_name, validate_plugin_meta
from notmyfault.security.security import SecurityMode

PKG_ROOT = Path(__file__).resolve().parents[1]

NEW_ACTIONS = [
    "append_text",
    "clipboard_clear",
    "create_shortcut",
    "media_control",
    "open_url",
    "power_plan",
    "send_keys",
    "uia_control",
    "uia_focus_window",
    "uia_read_text",
    "uia_wait",
    "window_pin",
]
WINDOWS_ONLY_ACTIONS = {
    "power_plan",
    "uia_control",
    "uia_focus_window",
    "uia_read_text",
    "uia_wait",
}
NEW_TRIGGERS = [
    "audio_device",
    "battery_level",
    "cron_schedule",
    "session_lock",
    "system_startup",
    "wifi_network",
]


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


def test_append_text_validates_input(tmp_path):
    mod = load_plugin("actions", "append_text")
    target = str(tmp_path / "log.txt")
    with pytest.raises(ValueError, match="未指定日志文件路径"):
        mod.run_with_context({}, {"text": "x"}, {})
    with pytest.raises(ValueError, match="没有可写入的内容"):
        mod.run_with_context({}, {"file_path": target, "text": "  "}, {})
    with pytest.raises(ValueError, match="不支持的编码"):
        mod.run_with_context(
            {}, {"file_path": target, "text": "x", "encoding": "utf-16"}, {}
        )


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


def test_clipboard_clear_linux_path(monkeypatch):
    import sys

    mod = load_plugin("actions", "clipboard_clear")
    written = []
    monkeypatch.setattr(mod.os, "name", "posix")
    monkeypatch.setitem(
        sys.modules,
        "notmyfault.platform.linux_support",
        SimpleNamespace(set_clipboard_text=written.append),
    )
    result = mod.run({}, {})
    assert result == {"cleared": True}
    assert written == [""]


# ------------------------------------------------------------ create_shortcut


def test_create_shortcut_requires_name_and_target():
    mod = load_plugin("actions", "create_shortcut")
    with pytest.raises(ValueError, match="未指定快捷方式名称"):
        mod.run({}, {"target_path": "C:\\x.exe"})
    with pytest.raises(ValueError, match="名称不合法"):
        mod.run({}, {"name": "../evil", "target_path": "C:\\x.exe"})
    with pytest.raises(ValueError, match="未指定快捷方式目标路径"):
        mod.run({}, {"name": "快捷"})


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


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用 SendInput ABI")
def test_send_keys_type_text_builds_unicode_inputs(monkeypatch):
    mod = load_plugin("actions", "send_keys")
    sent = []
    monkeypatch.setattr(mod, "_send", sent.extend)
    mod.run({}, {"mode": "type_text", "text": "a中"})
    assert len(sent) == 4
    assert [inp.ki.wScan for inp in sent] == [ord("a"), ord("a"), 0x4E2D, 0x4E2D]
    assert all(inp.ki.wVk == 0 for inp in sent)
    assert sent[0].ki.dwFlags == mod.KEYEVENTF_UNICODE
    assert sent[1].ki.dwFlags == mod.KEYEVENTF_UNICODE | mod.KEYEVENTF_KEYUP


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 需要 UTF-16 代理对")
def test_send_keys_type_text_splits_surrogate_pairs(monkeypatch):
    mod = load_plugin("actions", "send_keys")
    sent = []
    monkeypatch.setattr(mod, "_send", sent.extend)
    mod.run({}, {"mode": "type_text", "text": "😀"})
    # 😀 = U+1F600，拆成高代理 0xD83D 和低代理 0xDE00，各发一次按下和抬起
    assert [inp.ki.wScan for inp in sent] == [0xD83D, 0xD83D, 0xDE00, 0xDE00]
    assert all(inp.ki.wVk == 0 for inp in sent)


# Windows 上 run() 走 SendInput，会把组合键真的按进系统
@pytest.mark.skipif(os.name != "posix", reason="仅 Linux 调用 xdotool/ydotool")
def test_send_keys_linux_dispatches_input_tool(monkeypatch):
    mod = load_plugin("actions", "send_keys")
    commands = []
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/xdotool")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda cmd, **kwargs: commands.append(cmd) or SimpleNamespace(returncode=0),
    )
    assert mod.run({}, {"mode": "hotkey", "keys": "ctrl+win+enter"}) == {
        "mode": "hotkey"
    }
    assert commands[-1] == [
        "/usr/bin/xdotool",
        "key",
        "--clearmodifiers",
        "ctrl+super+Return",
    ]
    assert mod.run({}, {"mode": "type_text", "text": "a中😀"}) == {
        "mode": "type_text"
    }
    assert commands[-1] == [
        "/usr/bin/xdotool",
        "type",
        "--clearmodifiers",
        "--",
        "a中😀",
    ]


def test_send_keys_rejects_bad_input():
    mod = load_plugin("actions", "send_keys")
    with pytest.raises(ValueError, match="未知模式"):
        mod.run({}, {"mode": "warp"})
    with pytest.raises(ValueError, match="没有要输入的文本"):
        mod.run({}, {"mode": "type_text", "text": ""})
    with pytest.raises(ValueError, match="只允许修饰键在前"):
        mod.run({}, {"mode": "hotkey", "keys": "a+ctrl"})


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


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用 user32 ABI")
def test_window_pin_unpin_uses_notopmost(monkeypatch):
    mod = load_plugin("actions", "window_pin")
    user32 = _patch_pin_user32(monkeypatch, mod, exstyle=mod.WS_EX_TOPMOST)
    result = mod.run({}, {"action": "toggle"})
    assert result["state"] == "unpinned"
    assert user32.SetWindowPos.call_args.args[1] == mod.HWND_NOTOPMOST


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用 user32 ABI")
def test_window_pin_title_target_resolves_window(monkeypatch):
    mod = load_plugin("actions", "window_pin")
    _patch_pin_user32(monkeypatch, mod)
    monkeypatch.setattr(mod, "_find_windows_by_title", lambda kw: [42])
    result = mod.run({}, {"action": "pin", "target": "title", "title": "记事本"})
    assert result == {"state": "pinned", "hwnd": 42}


# Windows 上 run() 走 user32，会真的置顶前台窗口
@pytest.mark.skipif(os.name != "posix", reason="仅 Linux 调用 wmctrl")
def test_window_pin_linux_dispatches_wmctrl(monkeypatch):
    mod = load_plugin("actions", "window_pin")
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout="0x2a host 0 记事本\n", stderr="")

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/wmctrl")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.run({}, {"action": "pin", "target": "title", "title": "记事本"}) == {
        "state": "pinned",
        "window_id": "0x2a",
    }
    assert commands == [
        ["/usr/bin/wmctrl", "-l"],
        ["/usr/bin/wmctrl", "-i", "-r", "0x2a", "-b", "add,above"],
    ]


def test_window_pin_rejects_bad_input():
    mod = load_plugin("actions", "window_pin")
    with pytest.raises(ValueError, match="未知操作"):
        mod.run({}, {"action": "warp"})
    with pytest.raises(ValueError, match="必须填写窗口标题"):
        mod.run({}, {"action": "pin", "target": "title", "title": ""})


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


def test_audio_device_any_tracks_both_flows(monkeypatch):
    mod = load_plugin("triggers", "audio_device")
    scripted(
        monkeypatch,
        mod,
        "_query_default_devices",
        [
            {"render": "A", "capture": "C"},
            {"render": "B", "capture": "D"},
        ],
    )
    trigger, events = make_trigger(mod, "AudioDeviceTrigger", {"device_type": "any"})
    trigger.poll()
    trigger.poll()
    assert [e["device_type"] for e in events] == ["render", "capture"]


def test_audio_device_query_failure_is_silent(monkeypatch):
    mod = load_plugin("triggers", "audio_device")
    scripted(monkeypatch, mod, "_query_default_devices", [{}, {}])
    trigger, events = make_trigger(mod, "AudioDeviceTrigger", {})
    trigger.poll()
    trigger.poll()
    assert events == []


def test_audio_device_validate_rejects_bad_config():
    mod = load_plugin("triggers", "audio_device")
    trigger = mod.AudioDeviceTrigger(
        {"id": "audio_device"}, {"device_type": "speaker"}, lambda e: None, threading.Event()
    )
    with pytest.raises(ValueError, match="无效的设备类型"):
        trigger.validate()


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


def test_battery_level_ignores_missing_battery(monkeypatch):
    mod = load_plugin("triggers", "battery_level")
    monkeypatch.setattr(mod.psutil, "sensors_battery", lambda: None)
    trigger, events = make_trigger(mod, "BatteryLevelTrigger", {})
    trigger.poll()
    assert events == []


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


@pytest.mark.parametrize(
    "config",
    [
        {"mode": "hourly"},
        {"mode": "daily", "time": "25:00"},
        {"mode": "daily", "time": "abc"},
        {"mode": "weekly", "time": "08:00", "days": "9"},
        {"mode": "weekly", "time": "08:00", "days": ""},
        {"mode": "interval", "interval_minutes": 0},
        {"mode": "interval", "interval_minutes": "x"},
    ],
)
def test_cron_schedule_validate_rejects_bad_config(config):
    mod = load_plugin("triggers", "cron_schedule")
    trigger = mod.CronScheduleTrigger(
        {"id": "cron_schedule"}, config, lambda e: None, threading.Event()
    )
    with pytest.raises(ValueError):
        trigger.validate()


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


def test_session_lock_query_failure_is_silent(monkeypatch):
    mod = load_plugin("triggers", "session_lock")
    scripted(monkeypatch, mod, "_is_locked", [None, None])
    trigger, events = make_trigger(mod, "SessionLockTrigger", {})
    trigger.poll()
    trigger.poll()
    assert events == []


def test_session_lock_validate_rejects_bad_state():
    mod = load_plugin("triggers", "session_lock")
    trigger = mod.SessionLockTrigger(
        {"id": "session_lock"}, {"state": "sleeping"}, lambda e: None, threading.Event()
    )
    with pytest.raises(ValueError, match="无效的目标状态"):
        trigger.validate()


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


def test_system_startup_zero_delay_fires_immediately(monkeypatch):
    mod = load_plugin("triggers", "system_startup")
    clock = FakeClock(datetime(2026, 1, 1, 0, 0, 0))
    monkeypatch.setattr(mod, "datetime", clock)
    trigger, events = make_trigger(mod, "SystemStartupTrigger", {"delay_seconds": 0})
    trigger.poll()
    assert len(events) == 1


def test_system_startup_validate_rejects_bad_delay():
    mod = load_plugin("triggers", "system_startup")
    for config in ({"delay_seconds": "abc"}, {"delay_seconds": 301}):
        trigger = mod.SystemStartupTrigger(
            {"id": "system_startup"}, config, lambda e: None, threading.Event()
        )
        with pytest.raises(ValueError):
            trigger.validate()


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


def test_wifi_network_any_fires_on_every_change(monkeypatch):
    mod = load_plugin("triggers", "wifi_network")
    scripted(monkeypatch, mod, "_current_ssid", ["A", "B", ""])
    trigger, events = make_trigger(mod, "WifiNetworkTrigger", {"direction": "any"})
    trigger.poll()
    trigger.poll()
    trigger.poll()
    assert len(events) == 2


def test_wifi_network_validate_rejects_bad_config():
    mod = load_plugin("triggers", "wifi_network")
    bad = mod.WifiNetworkTrigger(
        {"id": "wifi_network"}, {"direction": "sideways"}, lambda e: None, threading.Event()
    )
    with pytest.raises(ValueError, match="无效的触发方向"):
        bad.validate()
    missing = mod.WifiNetworkTrigger(
        {"id": "wifi_network"}, {"direction": "connected"}, lambda e: None, threading.Event()
    )
    with pytest.raises(ValueError, match="必须填写目标 SSID"):
        missing.validate()


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


# ------------------------------------------------------------ 元数据与加载


@pytest.mark.parametrize("name", NEW_ACTIONS)
def test_new_action_meta_valid(name):
    meta = json.loads(
        (PKG_ROOT / "actions" / name / "action.json").read_text(encoding="utf-8")
    )
    ok, errors = validate_plugin_meta(meta, "action")
    assert ok is True, errors


@pytest.mark.parametrize("name", NEW_TRIGGERS)
def test_new_trigger_meta_valid(name):
    meta = json.loads(
        (PKG_ROOT / "triggers" / name / "trigger.json").read_text(encoding="utf-8")
    )
    ok, errors = validate_plugin_meta(meta, "trigger")
    assert ok is True, errors


class SudoStub:
    def authorize_plugin(self, plugin_id, token, module=None):
        pass

    def deauthorize_plugin(self, plugin_id, token):
        pass


def test_new_plugins_loaded_by_engine():
    loader = PluginLoader(
        registry=PluginRegistry(),
        config={},
        diagnostics=SimpleNamespace(record_plugin_error=lambda *a, **k: None),
        security_mode=SecurityMode.PERMISSIVE,
        sudo=SudoStub(),
        engine_token="token",
        integrity_errors=[],
    )
    base = str(PKG_ROOT)
    actions_meta: dict = {}
    loader.load(
        base_dir=base,
        plugins_dir="actions",
        json_filename="action.json",
        py_filename="action.py",
        module_prefix="notmyfault.action_",
        meta_store=actions_meta,
        func_store={},
        store_name="Action",
        origin="builtin",
    )
    triggers_meta: dict = {}
    loader.load(
        base_dir=base,
        plugins_dir="triggers",
        json_filename="trigger.json",
        py_filename="trigger.py",
        module_prefix="notmyfault.trigger_",
        meta_store=triggers_meta,
        func_store={},
        store_name="Trigger",
        origin="builtin",
    )
    expected_actions = [
        name
        for name in NEW_ACTIONS
        if current_platform_name() == "windows" or name not in WINDOWS_ONLY_ACTIONS
    ]
    for name in expected_actions:
        assert name in actions_meta, f"动作 {name} 未被引擎加载"
    for name in NEW_TRIGGERS:
        assert name in triggers_meta, f"触发器 {name} 未被引擎加载"
