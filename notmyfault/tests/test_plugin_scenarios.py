import ctypes
import importlib.util
import json
import os
import struct
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from notmyfault.core.workflow import ActionCancellation, ActionCancelled, invoke_action

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


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "gb18030"])
def test_append_text_writes_timestamped_lines(tmp_path, encoding):
    mod = load_plugin("actions", "append_text")
    target = tmp_path / "log.txt"
    mod.run_with_context(
        {},
        {"file_path": str(target), "text": "一行\n[已带标记]", "add_timestamp": True, "encoding": encoding},
        {},
    )
    lines = target.read_text(encoding=encoding).splitlines()
    assert lines[0].startswith("[") and lines[0].endswith("] 一行")
    assert lines[1] == "[已带标记]"


def test_file_operation_compresses_a_single_file(tmp_path):
    mod = load_plugin("actions", "file_operation")
    source = tmp_path / "note.txt"
    archive = tmp_path / "note.zip"
    source.write_text("内容", encoding="utf-8")

    mod.run(
        {},
        {
            "operation": "compress",
            "source": str(source),
            "destination": str(archive),
        },
    )

    with zipfile.ZipFile(archive) as zipped:
        assert zipped.namelist() == ["note.txt"]
        assert zipped.read("note.txt").decode("utf-8") == "内容"


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用文件关联")
@pytest.mark.parametrize("directory", [r"C:\Work", "work"])
def test_launch_program_association_preserves_arguments_and_directory(monkeypatch, directory):
    mod = load_plugin("actions", "launch_program")
    arguments = r'--profile "work folder" --output "C:\Reports\new.txt"'
    popen = MagicMock(side_effect=OSError(193, "不是有效的可执行程序"))
    startfile = MagicMock()
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    monkeypatch.setattr(mod.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(mod.os, "startfile", startfile)

    mod.run({}, {"path": "task.py", "args": arguments, "working_directory": directory})

    startfile.assert_called_once_with(
        os.path.abspath(os.path.join(directory, "task.py")),
        arguments=arguments,
        cwd=directory,
    )
    assert popen.call_args.kwargs["cwd"] == directory


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 广播显示器电源命令")
@pytest.mark.parametrize("operation, power", [("off", 2), ("on", -1)])
def test_display_power_allows_receiver_to_acquire_native_lock(monkeypatch, operation, power):
    mod = load_plugin("actions", "display_control")
    queued = threading.Event()
    received = threading.Event()
    messages = []

    def receive():
        if queued.wait(1):
            with mod.native_lock():
                received.set()

    def send(*message, synchronous=False):
        messages.append(message)
        queued.set()
        if synchronous and not received.wait(0.2):
            raise RuntimeError("接收线程无法处理广播")
        return 1

    user32 = SimpleNamespace(
        SendNotifyMessageW=MagicMock(side_effect=send),
        SendMessageW=MagicMock(side_effect=lambda *args: send(*args, synchronous=True)),
    )
    monkeypatch.setattr(mod, "ctypes", SimpleNamespace(
        windll=SimpleNamespace(user32=user32),
        c_ssize_t=ctypes.c_ssize_t,
    ))
    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    try:
        assert mod.run({}, {"action": operation}) == {"action": operation}
        assert received.wait(1)
        assert messages == [(mod.HWND_BROADCAST, mod.WM_SYSCOMMAND, mod.SC_MONITORPOWER, power)]
    finally:
        queued.set()
        receiver.join(timeout=1)


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
        c_int=ctypes.c_int,
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
    user32.SendMessageTimeoutW.return_value = 1
    fake_ctypes = SimpleNamespace(
        windll=SimpleNamespace(user32=user32),
        c_ssize_t=ctypes.c_ssize_t,
        c_size_t=ctypes.c_size_t,
        POINTER=lambda x: x,
        byref=lambda x: x,
    )
    monkeypatch.setattr(mod, "ctypes", fake_ctypes)
    return user32


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 发送 APPCOMMAND")
@pytest.mark.parametrize("accepted,targets", [([1], [123]), ([0, 1], [123, 456]), ([0, 0], [123, 456])])
def test_media_control_dispatches_appcommand(monkeypatch, accepted, targets):
    mod = load_plugin("actions", "media_control")
    user32 = _patch_media_user32(monkeypatch, mod)
    user32.SendMessageTimeoutW.side_effect = accepted
    if any(accepted):
        assert mod.run({}, {"command": "next"}) == {"command": "next"}
    else:
        with pytest.raises(RuntimeError, match="没有窗口接受媒体命令"):
            mod.run({}, {"command": "next"})
    calls = user32.SendMessageTimeoutW.call_args_list
    assert [call.args[0] for call in calls] == targets
    assert all(call.args[1] == 0x0319 and call.args[3] == (11 << 16) for call in calls)
    assert all(ctypes.sizeof(call.args[-1]) == ctypes.sizeof(ctypes.c_void_p) for call in calls)
    if len(targets) == 1:
        user32.FindWindowW.assert_not_called()


def test_media_control_rejects_unknown_command():
    mod = load_plugin("actions", "media_control")
    with pytest.raises(ValueError, match="未知媒体命令"):
        mod.run({}, {"command": "warp"})


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用 GDI 截图")
def test_screenshot_deselects_active_window_bitmap_before_reading(
    tmp_path, monkeypatch
):
    mod = load_plugin("actions", "screenshot")
    user32 = MagicMock()
    gdi32 = MagicMock()
    calls = []

    user32.GetForegroundWindow.return_value = 101
    user32.GetDC.return_value = 201

    def get_window_rect(hwnd, rect_pointer):
        assert hwnd == 101
        rect = rect_pointer._obj
        rect.left = 10
        rect.top = 20
        rect.right = 330
        rect.bottom = 200
        return 1

    def print_window(*args):
        calls.append("print")
        return 1

    user32.GetWindowRect.side_effect = get_window_rect
    user32.PrintWindow.side_effect = print_window
    gdi32.CreateCompatibleDC.return_value = 202
    gdi32.CreateCompatibleBitmap.return_value = 303

    def select_object(*args):
        calls.append("select" if calls == [] else "restore")
        return 404 if len(calls) == 1 else 303

    def get_dibits(*args):
        assert struct.unpack_from("<IiiHH", ctypes.string_at(args[5], 40)) == (40, 320, 180, 1, 32)
        calls.append("read")
        return 180

    gdi32.SelectObject.side_effect = select_object
    gdi32.GetDIBits.side_effect = get_dibits
    monkeypatch.setattr(mod, "user32", user32)
    monkeypatch.setattr(mod, "gdi32", gdi32)
    target = tmp_path / "active.bmp"

    meta = json.loads((PKG_ROOT / "actions/screenshot/action.json").read_text(encoding="utf-8"))
    assert invoke_action(
        mod.run, mod, meta,
        {"mode": "active_window", "output_path": str(target), "format": "bmp"},
        {},
    ) == {"file": str(target)}
    gdi32.CreateCompatibleBitmap.assert_called_once_with(201, 320, 180)
    user32.PrintWindow.assert_called_once_with(101, 202, 0)
    assert calls == ["select", "print", "restore", "read"]
    assert target.read_bytes()[:2] == b"BM"


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
@pytest.mark.parametrize("sent", [0, 1, 3])
def test_send_keys_partial_input_releases_pressed_keys_without_repeating(monkeypatch, sent):
    mod = load_plugin("actions", "send_keys")
    batches = []

    def send_input(count, inputs, size):
        batches.append([(inputs[index].ki.wVk, inputs[index].ki.dwFlags) for index in range(count)])
        return sent if len(batches) == 1 else count

    user32 = SimpleNamespace(SendInput=send_input)
    monkeypatch.setattr(mod, "ctypes", SimpleNamespace(
        windll=SimpleNamespace(user32=user32),
        POINTER=ctypes.POINTER,
        sizeof=ctypes.sizeof,
        c_int=ctypes.c_int,
    ))

    with pytest.raises(RuntimeError, match="SendInput"):
        mod.run({}, {"mode": "hotkey", "keys": "ctrl+a"})

    assert batches[0] == [(0x11, 0), (0x41, 0), (0x41, mod.KEYEVENTF_KEYUP), (0x11, mod.KEYEVENTF_KEYUP)]
    assert batches[1:] == ([] if sent == 0 else [[(0x11, mod.KEYEVENTF_KEYUP)]])


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

    monkeypatch.setattr(mod, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(mod, "platform_services", FakeInputBackend)
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
    meta = json.loads((PKG_ROOT / "actions/window_pin/action.json").read_text(encoding="utf-8"))
    assert invoke_action(mod.run, mod, meta, {"action": "pin", "target": "title", "title": "记事本"}, {}) == {
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


# ---------------------------------------------------------------- power_state


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
