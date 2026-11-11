import codecs
import ctypes
import getopt
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
@pytest.mark.parametrize("encoding", [
    "utf-8", "gb18030", "utf-16-le",
    pytest.param("oem", marks=pytest.mark.skipif(os.name != "nt", reason="OEM 编码仅 Windows 提供")),
])
def test_powershell_bounds_output_and_kills_cancelled_process(monkeypatch, cancel, encoding):
    module = load_plugin("actions", "run_powershell")
    monkeypatch.setattr(module.sys, "platform", "win32")
    event = threading.Event()
    output = ("完成 café\n" + "x" * (module._MAX_OUTPUT + 8192)).encode(encoding, errors="replace")
    class Process:
        returncode = None if cancel else 0
        stdout = io.BytesIO(output)
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
    command = " \nWrite-Output value` "
    def launch(*args, **kwargs):
        assert args[0][-1] == command
        if cancel:
            event.set()
        return process
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(module, "_read_available", lambda stream: stream.read1(4096))
    params = {"command": command}
    if encoding != "oem":
        params["encoding"] = encoding
    if cancel:
        with pytest.raises(ActionCancelled):
            module.run_with_context({}, params, {"runtime": {"cancellation": ActionCancellation(event)}})
        assert process.killed
    else:
        result = module.run_with_context({}, params, {})
        assert result["stdout"] == output[:module._MAX_OUTPUT].decode(encoding, errors="replace") + "...（已截断）"
        assert result["stderr"] == ""


@pytest.mark.parametrize("encoding", [
    "utf-8", "gb18030", "utf-16-le", "not-a-codec", "base64_codec", "rot_13",
    pytest.param("oem", marks=pytest.mark.skipif(os.name != "nt", reason="OEM 编码仅 Windows 提供")),
])
def test_powershell_reports_exit_code_and_stderr(monkeypatch, encoding):
    module = load_plugin("actions", "run_powershell")
    monkeypatch.setattr(module.sys, "platform", "win32")
    if encoding in ("not-a-codec", "base64_codec", "rot_13"):
        monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("编码无效时不应启动命令"))
        with pytest.raises(ValueError, match="编码"):
            module.run_with_context({}, {"command": "exit 3", "encoding": encoding}, {})
        return
    error_bytes = "失败：café\n".encode(encoding, errors="replace")

    class Process:
        returncode = 3
        stdout = io.BytesIO(b"")
        stderr = io.BytesIO(error_bytes)

        def poll(self):
            return self.returncode

        def kill(self):
            raise AssertionError("命令已退出，不该再杀进程")

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(module, "_read_available", lambda stream: stream.read1(4096))

    params = {"command": "exit 3"}
    if encoding != "oem":
        params["encoding"] = encoding
    with pytest.raises(RuntimeError) as caught:
        module.run_with_context({}, params, {})
    assert "code=3" in str(caught.value)
    assert error_bytes.decode(encoding, errors="replace").strip() in str(caught.value)


# --------------------------------------------------------------- append_text


@pytest.mark.parametrize("initial_state", ["missing", "empty", "content"])
@pytest.mark.parametrize("encoding,byte_encoding,bom", [
    ("utf-8", "utf-8", b""),
    ("utf-8-sig", "utf-8", codecs.BOM_UTF8),
    ("gb18030", "gb18030", b""),
    ("utf-16", "utf-16-le", codecs.BOM_UTF16_LE),
    ("utf-16", "utf-16-be", codecs.BOM_UTF16_BE),
    ("utf_16", "utf-16-be", codecs.BOM_UTF16_BE),
    ("utf-32", "utf-32-le", codecs.BOM_UTF32_LE),
    ("utf-32", "utf-32-be", codecs.BOM_UTF32_BE),
    ("UTF32", "utf-32-be", codecs.BOM_UTF32_BE),
])
def test_append_text_appends_without_overwriting(tmp_path, monkeypatch, initial_state, encoding, byte_encoding, bom):
    monkeypatch.chdir(tmp_path)
    mod = load_plugin("actions", "append_text")
    reader = load_plugin("actions", "read_text")
    target = tmp_path / " log.txt\u3000"
    original = "旧内容\n" if initial_state == "content" else ""
    original_bytes = bom + original.encode(byte_encoding) if original else b""
    if initial_state != "missing":
        target.write_bytes(original_bytes)
    params = {"file_path": target.name, "text": "新内容", "add_timestamp": False, "encoding": encoding}
    meta = json.loads((PKG_ROOT / "actions/append_text/action.json").read_text(encoding="utf-8"))
    result = invoke_action(mod.run_with_context, mod, meta, params, {})
    expected_bytes = original_bytes + "新内容\n".encode(byte_encoding if original else encoding)
    assert target.read_bytes() == expected_bytes
    assert result == {"file": str(target), "lines": 1, "written": "新内容"}
    loaded = reader.run_with_context({}, {"file_path": str(target), "encoding": encoding}, {})
    assert loaded["text"] == original + "新内容\n"
    invoke_action(mod.run_with_context, mod, meta, {**params, "text": "末尾"}, {})
    assert target.read_bytes().startswith(expected_bytes)
    assert reader.run_with_context({}, {"file_path": str(target), "encoding": encoding}, {})["text"] == original + "新内容\n末尾\n"
    if initial_state == "content" and codecs.lookup(encoding).name in ("utf-16", "utf-32"):
        unmarked = "旧内容\n".encode(byte_encoding)
        target.write_bytes(unmarked)
        with pytest.raises(ValueError, match="BOM"):
            invoke_action(mod.run_with_context, mod, meta, params, {})
        assert target.read_bytes() == unmarked
        invoke_action(mod.run_with_context, mod, meta, {**params, "encoding": byte_encoding}, {})
        assert target.read_bytes() == unmarked + "新内容\n".encode(byte_encoding)
        assert reader.run_with_context({}, {"file_path": str(target), "encoding": byte_encoding}, {})["text"] == "旧内容\n新内容\n"


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


@pytest.mark.parametrize("directory", [False, True])
def test_file_operation_compresses_sources_without_overwriting_them(tmp_path, directory):
    mod = load_plugin("actions", "file_operation")
    source = tmp_path / "source" if directory else tmp_path / "note.txt"
    source_file = source / "nested" / "note.txt" if directory else source
    source_file.parent.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / "note.zip"
    source_file.write_text("内容", encoding="utf-8")

    mod.run_with_context(
        {},
        {
            "operation": "compress",
            "source": str(source),
            "destination": str(archive),
        },
    {})

    with zipfile.ZipFile(archive) as zipped:
        member = "nested/note.txt" if directory else "note.txt"
        assert zipped.read(member).decode("utf-8") == "内容"
    alias = tmp_path / "source-alias.zip"
    os.link(source_file, alias)
    with pytest.raises(ValueError):
        mod.run_with_context({}, {"operation": "compress", "source": str(source), "destination": str(alias)}, {})
    assert source_file.read_text(encoding="utf-8") == alias.read_text(encoding="utf-8") == "内容"


@pytest.mark.parametrize("entry_name", ["a.txt", "a?.txt", "folder?/a.txt", "folder./a.txt"])
def test_archive_unzip_skips_existing_files_and_reports_them(tmp_path, entry_name):
    mod = load_plugin("actions", "archive")
    source = tmp_path / "a.txt"
    source.write_text("新内容", encoding="utf-8")
    archive = tmp_path / "pack.zip"
    assert mod.run_with_context({}, {"mode": "zip", "source": str(source), "destination": str(archive)}, {})["ok"]
    if entry_name != "a.txt":
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(entry_name, source.read_bytes())

    target = tmp_path / "out"
    target.mkdir()
    output_name = entry_name.replace("?", "_").replace("./", "/") if os.name == "nt" else entry_name
    output = target / output_name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("原有内容", encoding="utf-8")

    skipped = mod.run_with_context(
        {}, {"mode": "unzip", "source": str(archive), "destination": str(target)}
    , {})
    assert skipped["ok"] is True
    assert skipped["file_count"] == 0
    assert skipped["skipped"] == 1
    assert output.read_text(encoding="utf-8") == "原有内容"

    overwritten = mod.run_with_context(
        {},
        {
            "mode": "unzip",
            "source": str(archive),
            "destination": str(target),
            "overwrite": True,
        },
    {})
    assert overwritten["file_count"] == 1
    assert overwritten["skipped"] == 0
    assert output.read_text(encoding="utf-8") == "新内容"


def test_archive_unzip_rejects_archives_over_entry_limit(tmp_path, monkeypatch):
    mod = load_plugin("actions", "archive")
    monkeypatch.setattr(mod, "_MAX_ENTRIES", 2)
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for index in range(3):
            zipped.writestr(f"{index}.txt", "x")

    result = mod.run_with_context(
        {}, {"mode": "unzip", "source": str(archive), "destination": str(tmp_path / "out")}
    , {})
    assert result["ok"] is False
    assert "条目过多" in result["error"]
    assert not (tmp_path / "out" / "0.txt").exists()


def test_archive_unzip_rejects_archives_over_size_limit(tmp_path, monkeypatch):
    mod = load_plugin("actions", "archive")
    monkeypatch.setattr(mod, "_MAX_TOTAL_BYTES", 4)
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("big.txt", "0123456789")

    result = mod.run_with_context(
        {}, {"mode": "unzip", "source": str(archive), "destination": str(tmp_path / "out")}
    , {})
    assert result["ok"] is False
    assert "大小超过上限" in result["error"]


def test_archive_unzip_honors_cancellation(tmp_path):
    mod = load_plugin("actions", "archive")
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("a.txt", "a")
        zipped.writestr("b.txt", "b")

    class Cancellation:
        def raise_if_cancelled(self):
            raise ActionCancelled("已取消")

    with pytest.raises(ActionCancelled, match="已取消"):
        mod.run_with_context(
            {},
            {
                "mode": "unzip",
                "source": str(archive),
                "destination": str(tmp_path / "out"),
            },
            {"runtime": {"cancellation": Cancellation()}},
        )
    assert not (tmp_path / "out" / "a.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用文件关联")
@pytest.mark.parametrize("directory", [r"C:\Work", "work", " work", " work "])
@pytest.mark.parametrize("path", ["task.py", " task.py"])
def test_launch_program_association_preserves_arguments_and_directory(monkeypatch, directory, path):
    mod = load_plugin("actions", "launch_program")
    arguments = r'--profile "work folder" --output "C:\Reports\new.txt"'
    popen = MagicMock(side_effect=OSError(193, "不是有效的可执行程序"))
    startfile = MagicMock()
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    monkeypatch.setattr(mod.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(mod.os, "startfile", startfile)

    mod.run_with_context({}, {"path": path, "args": arguments, "working_directory": directory}, {})

    startfile.assert_called_once_with(
        os.path.abspath(os.path.join(directory, path)),
        arguments=arguments,
        cwd=directory,
    )
    assert popen.call_args.args[0][0] == path
    assert popen.call_args.kwargs["cwd"] == directory


@pytest.mark.parametrize("operation, power", [("off", 2), ("on", -1)])
def test_display_power_allows_receiver_to_acquire_native_lock(monkeypatch, operation, power):
    from notmyfault.platform.windows import display as mod
    queued = threading.Event()
    received = threading.Event()
    messages = []

    def receive():
        if queued.wait(1):
            with mod.NATIVE_LOCK:
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
    monkeypatch.setattr(mod._win32, "user32", lambda: user32)
    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    try:
        assert mod.DisplayBackend().set_power(operation) == {"methods": ["SendNotifyMessageW"], "warnings": []}
        assert received.wait(1)
        assert messages == [(0xFFFF, 0x0112, 0xF170, power)]
    finally:
        queued.set()
        receiver.join(timeout=1)


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 使用 WMI 亮度脚本")
def test_display_wmi_preserves_chinese_error(monkeypatch):
    from notmyfault.platform.windows import display as mod
    import subprocess
    run = subprocess.run
    monkeypatch.setattr("locale.getencoding", lambda: "utf-8")

    def without_monitors(command, **kwargs):
        command = list(command)
        command[-1] = "function Get-CimInstance { @() };" + command[-1]
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", without_monitors)
    with pytest.raises(RuntimeError) as caught:
        mod.DisplayBackend()._set_wmi_brightness(50)
    assert "未发现支持 WMI 亮度控制的显示器" in str(caught.value)
    assert "\ufffd" not in str(caught.value)


# ------------------------------------------------------------- clipboard_clear




# ------------------------------------------------------------ create_shortcut




def test_create_shortcut_rejects_unrepresentable_working_directory():
    from notmyfault.plugin_api import PlatformServiceError
    mod = load_plugin("actions", "create_shortcut")
    with pytest.raises(PlatformServiceError) as error:
        mod.run_with_context({}, {"working_directory": "C:\\"}, {})
    assert error.value.kind == "invalid"


# -------------------------------------------------------------- media_control


def _patch_media_user32(monkeypatch, mod):
    user32 = MagicMock()
    user32.GetForegroundWindow.return_value = 123
    user32.FindWindowW.return_value = 456
    user32.SendMessageTimeoutW.return_value = 1
    monkeypatch.setattr(mod._win32, "user32", lambda: user32)
    return user32


@pytest.mark.parametrize("accepted,targets", [([1], [123]), ([0, 1], [123, 456]), ([0, 0], [123, 456])])
def test_media_control_dispatches_appcommand(monkeypatch, accepted, targets):
    from notmyfault.platform.windows import media as mod
    user32 = _patch_media_user32(monkeypatch, mod)
    user32.SendMessageTimeoutW.side_effect = accepted
    if any(accepted):
        mod.MediaBackend().send("next")
    else:
        with pytest.raises(RuntimeError, match="没有窗口接受媒体命令"):
            mod.MediaBackend().send("next")
    calls = user32.SendMessageTimeoutW.call_args_list
    assert [call.args[0] for call in calls] == targets
    assert all(call.args[1] == 0x0319 and call.args[3] == (11 << 16) for call in calls)
    assert all(ctypes.sizeof(call.args[-1]._obj) == ctypes.sizeof(ctypes.c_void_p) for call in calls)
    if len(targets) == 1:
        user32.FindWindowW.assert_not_called()


def test_media_control_rejects_unknown_command():
    mod = load_plugin("actions", "media_control")
    with pytest.raises(ValueError, match="未知媒体命令"):
        mod.run_with_context({}, {"command": "warp"}, {})


@pytest.mark.parametrize("mode", ["active_window", "fullscreen"])
def test_screenshot_deselects_active_window_bitmap_before_reading(
    tmp_path, monkeypatch, mode
):
    monkeypatch.chdir(tmp_path)
    from notmyfault.platform.windows import screen as mod
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
    monkeypatch.setattr(mod._win32, "user32", lambda: user32)
    monkeypatch.setattr(mod._win32, "gdi32", lambda: gdi32)
    target = tmp_path / " active.bmp\u3000"

    assert mod.ScreenBackend().capture(target.name, mode, "bmp") == target.name
    gdi32.CreateCompatibleBitmap.assert_called_once_with(201, 320, 180)
    if mode == "active_window":
        user32.PrintWindow.assert_called_once_with(101, 202, 0)
    else:
        gdi32.BitBlt.assert_called_once_with(202, 0, 0, 320, 180, 201, -320, -180, 0x00CC0020)
    assert calls == ["select", "print", "restore", "read"]
    assert target.read_bytes()[:2] == b"BM"


# ------------------------------------------------------------------ open_url


@pytest.mark.parametrize("new_window", [False, True, "false", "true", 0, 1, None, [], {}])
def test_open_url_prepends_https_and_opens_all(monkeypatch, new_window):
    mod = load_plugin("actions", "open_url")
    meta = json.loads((PKG_ROOT / "actions" / "open_url" / "action.json").read_text(encoding="utf-8"))
    opened = []
    monkeypatch.setattr(
        mod.webbrowser, "open", lambda url, new=0: opened.append((url, new)) or True
    )
    params = {"urls": "example.com\nhttps://foo.bar", "new_window": new_window}
    if not isinstance(new_window, bool):
        with pytest.raises(ValueError, match="布尔"):
            invoke_action(mod.run_with_context, mod, meta, params, {})
        assert opened == []
        return
    result = invoke_action(mod.run_with_context, mod, meta, params, {})
    assert result == {"opened": 2}
    assert opened == [("https://example.com", int(new_window)), ("https://foo.bar", int(new_window))]
    opened.clear()
    with pytest.raises(ValueError, match="只支持"):
        mod.run_with_context({}, {"urls": "https://example.com\nfile:///bad"}, {})
    assert opened == []


def test_open_url_requires_input():
    mod = load_plugin("actions", "open_url")
    with pytest.raises(ValueError, match="没有可打开的链接"):
        mod.run_with_context({}, {"urls": "  \n"}, {})
    with pytest.raises(ValueError, match="只支持 http/https"):
        mod.run_with_context({}, {"urls": "file://C:/x"}, {})


# ---------------------------------------------------------------- power_plan




# ------------------------------------------------------------------- run_shell


@pytest.mark.parametrize("cancel", [False, True])
def test_run_shell_bounds_output_and_kills_cancelled_process(monkeypatch, cancel):
    module = load_plugin("actions", "run_shell")
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module, "_shell_executable", lambda: "/bin/bash")
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
    command = " \nprintf '%s' value\\\t"
    def launch(*args, **kwargs):
        assert args[0][-1] == command
        if cancel:
            event.set()
        return process
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(module, "_read_available", lambda stream: stream.read1(4096))
    if cancel:
        with pytest.raises(ActionCancelled):
            module.run_with_context(
                {},
                {"command": command},
                {"runtime": {"cancellation": ActionCancellation(event)}},
            )
        assert process.killed
    else:
        result = module.run_with_context({}, {"command": command}, {})
        assert result["stdout"] == "x" * module._MAX_OUTPUT + "...（已截断）"
        assert result["stderr"] == ""


def test_run_shell_prefers_fixed_shell_path(monkeypatch):
    module = load_plugin("actions", "run_shell")
    monkeypatch.setattr(module.os.path, "isfile", lambda path: path == "/usr/bin/bash")
    monkeypatch.setattr(module.os, "access", lambda path, mode: path == "/usr/bin/bash")
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/tmp/hijack/{name}")

    assert module._shell_executable() == "/usr/bin/bash"


def test_run_shell_falls_back_to_path_lookup(monkeypatch):
    module = load_plugin("actions", "run_shell")
    monkeypatch.setattr(module.os.path, "isfile", lambda path: False)
    monkeypatch.setattr(
        module.shutil, "which", lambda name: f"/opt/{name}" if name == "bash" else None
    )

    assert module._shell_executable() == "/opt/bash"


def test_run_shell_reports_exit_code_and_stderr(monkeypatch):
    module = load_plugin("actions", "run_shell")
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module, "_shell_executable", lambda: "/bin/bash")

    class Process:
        returncode = 3
        stdout = io.BytesIO(b"")
        stderr = io.BytesIO(b"boom\n")

        def poll(self):
            return self.returncode

        def kill(self):
            raise AssertionError("命令已退出，不该再杀进程")

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(module, "_read_available", lambda stream: stream.read1(4096))

    with pytest.raises(RuntimeError) as caught:
        module.run_with_context({}, {"command": "false"}, {})
    assert "code=3" in str(caught.value)
    assert "boom" in str(caught.value)


# ------------------------------------------------------------------ send_keys




@pytest.mark.parametrize("sent", [0, 1, 3])
def test_send_keys_partial_input_releases_pressed_keys_without_repeating(monkeypatch, sent):
    from notmyfault.platform.windows import keyboard as mod
    batches = []

    def send_input(count, inputs, size):
        batches.append([(inputs[index].ki.wVk, inputs[index].ki.dwFlags) for index in range(count)])
        return sent if len(batches) == 1 else count

    user32 = SimpleNamespace(SendInput=send_input)
    monkeypatch.setattr(mod._win32, "user32", lambda: user32)

    with pytest.raises(RuntimeError, match="SendInput"):
        mod.KeyboardBackend().send_hotkey(["ctrl", "a"])

    assert batches[0] == [(0x11, 0), (0x41, 0), (0x41, 2), (0x11, 2)]
    assert batches[1:] == ([] if sent == 0 else [[(0x11, 2)]])




# ---------------------------------------------------------------- window_pin




class WindowControlBackend:
    def __init__(self):
        self.windows = {
            "22": {"window_id": "22", "title": "文档 — App", "pid": 88, "process_name": "app.exe", "class_name": "AppWindow",
                 "x": 20, "y": 30, "width": 800, "height": 600, "visible": True, "minimized": False,
                 "maximized": False, "topmost": False, "opacity": 100},
            "11": {"window_id": "11", "title": "草稿 — App", "pid": 88, "process_name": "app.exe", "class_name": "AppWindow",
                 "x": 10, "y": 10, "width": 400, "height": 300, "visible": False, "minimized": False,
                 "maximized": False, "topmost": False, "opacity": 100},
        }
        self.changed = []

    def foreground(self):
        return "22"

    def list(self):
        return [dict(window) for window in self.windows.values()]

    def info(self, hwnd):
        return dict(self.windows[hwnd]) if hwnd in self.windows else None

    def show(self, hwnd, state):
        self.changed.append((hwnd, "show"))
        self.windows[hwnd].update(visible=state != "hide", minimized=state == "minimize", maximized=state == "maximize")

    def set_pinned(self, action, window_id, title=""):
        self.changed.append((window_id, "pin"))
        self.windows[window_id]["topmost"] = action == "pin"
        return {"state": "pinned" if action == "pin" else "unpinned", "window_id": window_id}

    def move(self, hwnd, rect):
        self.changed.append((hwnd, "position"))
        self.windows[hwnd].update(rect)

    def monitors(self):
        return [{"handle": 1, "bounds": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                 "work": {"x": 0, "y": 0, "width": 1920, "height": 1040}},
                {"handle": 2, "bounds": {"x": -1441, "y": -100, "width": 1441, "height": 1001},
                 "work": {"x": -1441, "y": -100, "width": 1441, "height": 1001}}]


def window_control(monkeypatch):
    module = load_plugin("actions", "window_control")
    backend = WindowControlBackend()
    monkeypatch.setattr(module, "platform", SimpleNamespace(window=backend))
    meta = json.loads((PKG_ROOT / "actions/window_control/action.json").read_text(encoding="utf-8"))
    return module, backend, meta


@pytest.mark.parametrize("selector,expected", [
    ({"target": "title", "title": "app"}, ["22"]),
    ({"target": "class_name", "class_name": "^appwindow$", "match_mode": "regex"}, ["22"]),
    ({"target": "process", "process_name": "APP", "include_hidden": True}, ["22", "11"]),
    ({"target": "pid", "pid": 88, "include_hidden": True}, ["22", "11"]),
    ({"target": "window_id", "window_id": "11"}, ["11"]),
])
def test_window_control_selection_and_output_contract(monkeypatch, selector, expected):
    module, backend, meta = window_control(monkeypatch)
    result = invoke_action(module.run_with_context, module, meta, {"action": "list", **selector}, {})
    assert [item["window_id"] for item in result["windows"]] == expected
    assert result["count"] == len(expected)
    assert backend.changed == []


def test_window_control_requires_explicit_multiple_match_policy(monkeypatch):
    module, backend, _ = window_control(monkeypatch)
    params = {"action": "pin", "target": "title", "title": "App", "include_hidden": True}
    with pytest.raises(RuntimeError, match="匹配到 2 个窗口"):
        module.run_with_context({}, params, {})
    assert backend.changed == []
    result = module.run_with_context({}, {**params, "match": "first"}, {})
    assert result["window_id"] == "22"
    assert backend.changed == [("22", "pin")]
    backend.changed.clear()
    result = module.run_with_context({}, {**params, "match": "all"}, {})
    assert result["count"] == 2 and result["window_id"] == ""
    assert backend.changed == [("22", "pin"), ("11", "pin")]


@pytest.mark.parametrize("options,expected", [
    ({"action": "snap", "layout": "right"}, {"x": -721, "y": -100, "width": 721, "height": 1001}),
    ({"action": "snap", "layout": "bottom_left"}, {"x": -1441, "y": 400, "width": 720, "height": 501}),
    ({"action": "move_to_monitor"}, {"x": -1121, "y": 100, "width": 800, "height": 600}),
    ({"action": "move_resize", "x": -1200, "y": -80, "width": 640, "height": 480},
     {"x": -1200, "y": -80, "width": 640, "height": 480}),
])
def test_window_control_restores_and_positions_in_monitor_work_area(monkeypatch, options, expected):
    module, backend, meta = window_control(monkeypatch)
    backend.windows["22"]["minimized"] = True
    result = invoke_action(module.run_with_context, module, meta, {**options, "monitor": "next"}, {})
    window = result["windows"][0]
    assert {key: window[key] for key in expected} == expected
    assert not window["minimized"]
    assert backend.changed == [("22", "show"), ("22", "position")]


def test_window_control_wait_can_be_cancelled(monkeypatch):
    module, backend, meta = window_control(monkeypatch)
    cancelled = threading.Event()
    def disappear():
        cancelled.set()
        return []
    monkeypatch.setattr(backend, "list", disappear)
    with pytest.raises(ActionCancelled):
        invoke_action(module.run_with_context, module, meta, {"target": "all", "wait_seconds": 30},
                      {"runtime": {"cancellation": ActionCancellation(cancelled)}})
    assert backend.changed == []


def test_window_control_batch_uses_one_deadline(monkeypatch):
    module, backend, _ = window_control(monkeypatch)
    clock = [0]
    original = backend.set_pinned
    def slow_pin(action, window_id, title=""):
        original(action, window_id, title)
        clock[0] += 2
    monkeypatch.setattr(backend, "set_pinned", slow_pin)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(RuntimeError, match="已完成 1/2"):
        module.run_with_context({}, {"action": "pin", "target": "all", "match": "all", "include_hidden": True, "timeout_seconds": 1}, {})
    assert backend.changed == [("22", "pin")]


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
