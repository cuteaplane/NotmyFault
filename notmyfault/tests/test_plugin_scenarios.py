import ctypes
import io
import json
import os
import struct
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from notmyfault.core.workflow import ActionCancellation, ActionCancelled, invoke_action

from notmyfault.tests.plugin_support import load_plugin

PKG_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("cancel", [False, True])
def test_powershell_bounds_output_and_kills_cancelled_process(monkeypatch, cancel):
    module = load_plugin("actions", "run_powershell")
    event = threading.Event()
    class Process:
        returncode = None if cancel else 0
        stdout = io.BytesIO(b"x" * (module._MAX_OUTPUT + 8192))
        stderr = io.BytesIO(b"")
        killed = False
        def poll(self):
            return self.returncode
        def kill(self):
            self.killed = True
            self.returncode = -1
        def wait(self, timeout):
            return self.returncode
    process = Process()
    def launch(*args, **kwargs):
        if cancel:
            event.set()
        return process
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(module, "_read_available", lambda stream: stream.read1(4096))
    if cancel:
        with pytest.raises(ActionCancelled):
            module.run_with_context({}, {"command": "ignored"}, {"runtime": {"cancellation": ActionCancellation(event)}})
        assert process.killed
    else:
        result = module.run({}, {"command": "ignored"})
        assert result["stdout"] == "x" * module._MAX_OUTPUT + "...（已截断）"
        assert result["stderr"] == ""


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
        {"file_path": str(target), "text": "一行\n\n[普通文本]\n[2024-01-01 12:00:00] 已带时间", "add_timestamp": True, "encoding": encoding},
        {},
    )
    lines = target.read_text(encoding=encoding).splitlines()
    assert lines[0].startswith("[") and lines[0].endswith("] 一行")
    assert lines[1] == ""
    assert lines[2].endswith("] [普通文本]")
    assert lines[3] == "[2024-01-01 12:00:00] 已带时间"


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
@pytest.mark.parametrize("mode", ["active_window", "fullscreen"])
def test_screenshot_deselects_active_window_bitmap_before_reading(
    tmp_path, monkeypatch, mode
):
    mod = load_plugin("actions", "screenshot")
    user32 = MagicMock()
    gdi32 = MagicMock()
    calls = []

    user32.GetForegroundWindow.return_value = 101
    user32.GetDC.return_value = 201
    user32.GetSystemMetrics.side_effect = lambda metric: {76:-320, 77:-180, 78:320, 79:180}[metric]

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
    gdi32.BitBlt.side_effect = print_window
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
        {"mode": mode, "output_path": str(target), "format": "bmp"},
        {},
    ) == {"file": str(target)}
    gdi32.CreateCompatibleBitmap.assert_called_once_with(201, 320, 180)
    if mode == "active_window":
        user32.PrintWindow.assert_called_once_with(101, 202, 0)
    else:
        gdi32.BitBlt.assert_called_once_with(202, 0, 0, 320, 180, 201, -320, -180, 0x00CC0020)
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
    opened.clear()
    with pytest.raises(ValueError, match="只支持"):
        mod.run({}, {"urls": "https://example.com\nfile:///bad"})
    assert opened == []


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
    def set_window_pos(_hwnd, insert_after, *_args):
        user32.GetWindowLongW.return_value = mod.WS_EX_TOPMOST if insert_after == mod.HWND_TOPMOST else 0
        return 1
    user32.SetWindowPos.side_effect = set_window_pos
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


class WindowControlBackend:
    def __init__(self):
        self.windows = {
            22: {"hwnd": 22, "title": "文档 — App", "pid": 88, "process_name": "app.exe", "class_name": "AppWindow",
                 "x": 20, "y": 30, "width": 800, "height": 600, "visible": True, "minimized": False,
                 "maximized": False, "topmost": False, "opacity": 100},
            11: {"hwnd": 11, "title": "草稿 — App", "pid": 88, "process_name": "app.exe", "class_name": "AppWindow",
                 "x": 10, "y": 10, "width": 400, "height": 300, "visible": False, "minimized": False,
                 "maximized": False, "topmost": False, "opacity": 100},
        }
        self.changed = []

    def foreground(self):
        return 22

    def handles(self):
        return list(self.windows)

    def info(self, hwnd):
        return dict(self.windows[hwnd]) if hwnd in self.windows else None

    def show(self, hwnd, command):
        self.changed.append((hwnd, "show"))
        self.windows[hwnd].update(visible=command != 0, minimized=command == 6, maximized=command == 3)

    def pin(self, hwnd, enabled):
        self.changed.append((hwnd, "pin"))
        self.windows[hwnd]["topmost"] = enabled

    def position(self, hwnd, rect):
        self.changed.append((hwnd, "position"))
        self.windows[hwnd].update(rect)

    def monitor_for(self, hwnd):
        return 1

    def monitors(self):
        return [{"handle": 1, "work": {"x": 0, "y": 0, "width": 1920, "height": 1040}},
                {"handle": 2, "work": {"x": -1441, "y": -100, "width": 1441, "height": 1001}}]


def window_control(monkeypatch):
    module = load_plugin("actions", "window_control")
    backend = WindowControlBackend()
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module, "_backend", lambda: backend)
    meta = json.loads((PKG_ROOT / "actions/window_control/action.json").read_text(encoding="utf-8"))
    return module, backend, meta


@pytest.mark.parametrize("selector,expected", [
    ({"target": "title", "title": "app"}, [22]),
    ({"target": "class_name", "class_name": "^appwindow$", "match_mode": "regex"}, [22]),
    ({"target": "process", "process_name": "APP", "include_hidden": True}, [22, 11]),
    ({"target": "pid", "pid": 88, "include_hidden": True}, [22, 11]),
    ({"target": "hwnd", "hwnd": 11}, [11]),
])
def test_window_control_selection_and_output_contract(monkeypatch, selector, expected):
    module, backend, meta = window_control(monkeypatch)
    result = invoke_action(module.run, module, meta, {"action": "list", **selector}, {})
    assert [item["hwnd"] for item in result["windows"]] == expected
    assert result["count"] == len(expected)
    assert backend.changed == []


def test_window_control_requires_explicit_multiple_match_policy(monkeypatch):
    module, backend, _ = window_control(monkeypatch)
    params = {"action": "pin", "target": "title", "title": "App", "include_hidden": True}
    with pytest.raises(RuntimeError, match="匹配到 2 个窗口"):
        module.run({}, params)
    assert backend.changed == []
    result = module.run({}, {**params, "match": "first"})
    assert result["hwnd"] == 22
    assert backend.changed == [(22, "pin")]
    backend.changed.clear()
    result = module.run({}, {**params, "match": "all"})
    assert result["count"] == 2 and result["hwnd"] == 0
    assert backend.changed == [(22, "pin"), (11, "pin")]


@pytest.mark.parametrize("options,expected", [
    ({"action": "snap", "layout": "right"}, {"x": -721, "y": -100, "width": 721, "height": 1001}),
    ({"action": "snap", "layout": "bottom_left"}, {"x": -1441, "y": 400, "width": 720, "height": 501}),
    ({"action": "move_to_monitor"}, {"x": -1121, "y": 100, "width": 800, "height": 600}),
    ({"action": "move_resize", "x": -1200, "y": -80, "width": 640, "height": 480},
     {"x": -1200, "y": -80, "width": 640, "height": 480}),
])
def test_window_control_restores_and_positions_in_monitor_work_area(monkeypatch, options, expected):
    module, backend, meta = window_control(monkeypatch)
    backend.windows[22]["minimized"] = True
    result = invoke_action(module.run, module, meta, {**options, "monitor": "next"}, {})
    window = result["windows"][0]
    assert {key: window[key] for key in expected} == expected
    assert not window["minimized"]
    assert backend.changed == [(22, "show"), (22, "position")]


def test_window_control_wait_can_be_cancelled(monkeypatch):
    module, backend, meta = window_control(monkeypatch)
    cancelled = threading.Event()
    def disappear():
        cancelled.set()
        return []
    monkeypatch.setattr(backend, "handles", disappear)
    with pytest.raises(ActionCancelled):
        invoke_action(module.run, module, meta, {"target": "all", "wait_seconds": 30},
                      {"runtime": {"cancellation": ActionCancellation(cancelled)}})
    assert backend.changed == []


def test_window_control_batch_uses_one_deadline(monkeypatch):
    module, backend, _ = window_control(monkeypatch)
    clock = [0]
    original = backend.pin
    def slow_pin(hwnd, enabled):
        original(hwnd, enabled)
        clock[0] += 2
    monkeypatch.setattr(backend, "pin", slow_pin)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(RuntimeError, match="已完成 1/2"):
        module.run({}, {"action": "pin", "target": "all", "match": "all", "include_hidden": True, "timeout_seconds": 1})
    assert backend.changed == [(22, "pin")]


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
