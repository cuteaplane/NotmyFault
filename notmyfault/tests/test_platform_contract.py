import ctypes
import importlib
import io
import json
import types
import os
import stat
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from notmyfault.platform.errors import BackendError, BackendFailedError, BackendInvalidError, PlatformServiceError
from notmyfault.platform.services import operations, platform


def completed(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


class DesktopState:
    def __init__(self):
        self.text = "文本😀"
        self.volume = 0.37
        self.muted = False
        self.brightness = 50
        self.defaults = {"render": "41", "capture": "55"}
        self.devices = [
            {"id": "41", "name": "Speakers", "flow": "render"},
            {"id": "42", "name": "Headphones", "flow": "render"},
            {"id": "55", "name": "Microphone", "flow": "capture"},
        ]
        self.window = {"x": 10, "y": 20, "width": 800, "height": 600}
        self.pinned = False
        self.visible = True
        self.minimized = False
        self.maximized = False
        self.closed = False
        self.inputs = []
        self.profile = "balanced"
        self.reject = False
        self.ignore_brightness = False


class DesktopRunner:
    def __init__(self, state):
        self.state = state

    def which(self, *names):
        return next((f"/fake/{name}" for name in names if name in {
            "wl-paste", "wl-copy", "xdotool", "wpctl", "wmctrl", "xprop",
            "xrandr", "brightnessctl", "powerprofilesctl", "powercfg",
            "gnome-screenshot", "tesseract", "playerctl", "xset",
        }), None)

    def read_output(self, args, max_chars, timeout):
        result = self.run(args, timeout=timeout)
        result.stdout = result.stdout[:max_chars]
        return result

    def run(self, args, **kwargs):
        state = self.state
        tool, *parts = args
        tool = Path(tool).name
        if state.reject:
            return completed(returncode=1, stderr="device rejected request")
        if tool == "wl-copy":
            state.text = None if "--clear" in parts else kwargs.get("input", "")
        elif tool == "wl-paste":
            if "--list-types" in parts:
                return completed("text/plain;charset=utf-8" if state.text is not None else "image/png")
            return completed(state.text or "")
        elif tool == "wpctl":
            if parts[0] == "status":
                output = "Audio\n ├─ Sinks:\n"
                for flow, header in (("render", ""), ("capture", " ├─ Sources:\n")):
                    output += header
                    for device in state.devices:
                        if device["flow"] == flow:
                            mark = "*" if state.defaults[flow] == device["id"] else " "
                            output += f" │ {mark} {device['id']}. {device['name']} [vol: 0.37]\n"
                return completed(output + " └─ Streams:\n")
            if parts[0] == "get-volume":
                return completed(f"Volume: {state.volume:.2f}" + (" [MUTED]" if state.muted else ""))
            if parts[0] == "set-volume":
                state.volume = float(parts[-1].rstrip("%")) / 100
            elif parts[0] == "set-mute":
                state.muted = parts[-1] == "1"
            elif parts[0] == "set-default":
                selected = next(device for device in state.devices if device["id"] == parts[-1])
                state.defaults[selected["flow"]] = selected["id"]
        elif tool == "xdotool":
            state.inputs.append(parts)
            if parts[0] == "windowminimize":
                state.minimized = True
            elif parts[0] in {"windowmap", "windowunmap"}:
                state.visible = parts[0] == "windowmap"
        elif tool == "wmctrl":
            if parts == ["-lGpx"]:
                if state.closed or not state.visible:
                    return completed()
                rect = state.window
                return completed(f"0x2 0 0 {rect['x']} {rect['y']} {rect['width']} {rect['height']} app.App host Document\n")
            if "-b" in parts:
                change = parts[-1]
                if "above" in change:
                    state.pinned = change.startswith("add")
                if "hidden" in change:
                    state.minimized = change.startswith("add")
                if "maximized" in change:
                    state.maximized = change.startswith("add")
            if "-e" in parts:
                values = list(map(int, parts[-1].split(",")))[1:]
                state.window.update(zip(("x", "y", "width", "height"), values))
            if "-c" in parts:
                state.closed = True
        elif tool == "xprop":
            if "-root" in parts:
                return completed("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2")
            if "_NET_WM_STATE" in parts:
                return completed(",".join(name for name, enabled in [
                    ("_NET_WM_STATE_ABOVE", state.pinned),
                    ("_NET_WM_STATE_HIDDEN", state.minimized),
                    ("_NET_WM_STATE_MAXIMIZED_VERT", state.maximized),
                    ("_NET_WM_STATE_MAXIMIZED_HORZ", state.maximized),
                ] if enabled))
            return completed(returncode=int(state.closed))
        elif tool == "xrandr":
            return completed("Monitors: 1\n 0: +*HDMI-1 1920/527x1080/296+0+0 HDMI-1\n")
        elif tool == "brightnessctl":
            if parts[0] == "set" and not state.ignore_brightness:
                state.brightness = int(parts[-1].rstrip("%"))
            elif parts[0] == "get":
                return completed(str(state.brightness))
            elif parts[0] == "max":
                return completed("100")
        elif tool == "powerprofilesctl":
            state.profile = parts[-1]
        elif tool == "powercfg":
            state.profile = parts[-1]
        elif tool == "powershell.exe":
            if not state.ignore_brightness:
                state.brightness = int(re.search(r"Brightness=\[byte\](\d+)", parts[-1])[1])
            return completed(str(state.brightness))
        elif tool == "gnome-screenshot":
            from PIL import Image
            Image.new("RGB", (2, 2), "red").save(parts[-1])
        elif tool == "tesseract":
            return completed("文字\n")
        return completed()


def install_native(monkeypatch, state):
    from notmyfault.platform.windows import _win32

    user = MagicMock()
    kernel = MagicMock()
    gdi = MagicMock()
    ddc = MagicMock()
    ddc.GetNumberOfPhysicalMonitorsFromHMONITOR.return_value = 0
    kernel.CreateFileW.side_effect = lambda path, *_: 20 if Path(path).is_file() else ctypes.c_void_p(-1).value
    buffers = {}
    clipboard = [None]

    def allocate(_flags, size):
        buffer = ctypes.create_string_buffer(size)
        handle = ctypes.addressof(buffer)
        buffers[handle] = buffer
        return handle

    def clipboard_data(_format):
        raw = (state.text + "\0").encode("utf-16-le")
        handle = allocate(0, len(raw))
        ctypes.memmove(handle, raw, len(raw))
        return handle

    def set_clipboard(_format, handle):
        state.text = bytes(buffers[handle]).decode("utf-16-le").split("\0", 1)[0]
        clipboard[0] = handle
        return handle

    kernel.GlobalAlloc.side_effect = allocate
    kernel.GlobalLock.side_effect = lambda handle: handle
    kernel.GlobalSize.side_effect = lambda handle: len(buffers[handle])
    user.OpenClipboard.side_effect = lambda _: not state.reject
    user.IsClipboardFormatAvailable.side_effect = lambda _: state.text is not None
    user.GetClipboardData.side_effect = clipboard_data
    user.EmptyClipboard.side_effect = lambda: setattr(state, "text", None) or True
    user.SetClipboardData.side_effect = set_clipboard
    user.SendInput.side_effect = lambda count, inputs, size: state.inputs.extend(
        (inputs[index].ki.wVk, inputs[index].ki.wScan, inputs[index].ki.dwFlags)
        for index in range(count)
    ) or count
    user.GetForegroundWindow.return_value = 2
    user.IsWindow.side_effect = lambda handle: handle == 2 and not state.closed
    user.GetAncestor.side_effect = lambda handle, _: handle
    user.EnumWindows.side_effect = lambda callback, value: callback(2, value) if not state.closed else 1
    user.GetWindowTextLengthW.return_value = len("Document")
    user.GetWindowTextW.side_effect = lambda handle, buffer, size: setattr(buffer, "value", "Document") or 8
    user.GetClassNameW.side_effect = lambda handle, buffer, size: setattr(buffer, "value", "App") or 3
    user.GetWindowThreadProcessId.side_effect = lambda handle, pointer: setattr(pointer._obj, "value", 0) or 1

    def window_rect(_handle, pointer):
        rect = pointer._obj
        rect.left, rect.top = state.window["x"], state.window["y"]
        rect.right, rect.bottom = rect.left + state.window["width"], rect.top + state.window["height"]
        return 1

    user.GetWindowRect.side_effect = window_rect
    user.IsWindowVisible.side_effect = lambda _: state.visible
    user.IsIconic.side_effect = lambda _: state.minimized
    user.IsZoomed.side_effect = lambda _: state.maximized
    user.GetWindowLongW.side_effect = lambda *_: 8 if state.pinned else 0

    def position(_handle, after, x, y, width, height, flags):
        if after in (-1, -2):
            state.pinned = after == -1
        else:
            state.window.update(x=x, y=y, width=width, height=height)
        return 1

    user.SetWindowPos.side_effect = position
    def show_window(_handle, command):
        state.visible = command != 0
        state.minimized = command == 6
        state.maximized = command == 3
        return 1

    user.ShowWindowAsync.side_effect = show_window
    user.PostMessageW.side_effect = lambda *_: setattr(state, "closed", True) or 1
    user.EnumDisplayMonitors.side_effect = lambda a, b, callback, value: callback(1, None, None, value)

    def monitor_info(_handle, pointer):
        info = pointer._obj
        info.flags, info.device = 1, "DISPLAY1"
        info.bounds.right, info.bounds.bottom = 1920, 1080
        info.work.right, info.work.bottom = 1920, 1040
        return 1

    user.GetMonitorInfoW.side_effect = monitor_info
    user.GetSystemMetrics.side_effect = lambda metric: {76: 0, 77: 0, 78: 2, 79: 2}[metric]
    user.GetDC.return_value = 10
    gdi.CreateCompatibleDC.return_value = 11
    gdi.CreateCompatibleBitmap.return_value = 12
    gdi.SelectObject.return_value = 13
    gdi.GetDIBits.return_value = 2
    monkeypatch.setattr(_win32, "user32", lambda: user)
    monkeypatch.setattr(_win32, "kernel32", lambda: kernel)
    monkeypatch.setattr(_win32, "gdi32", lambda: gdi)
    monkeypatch.setattr(_win32, "dxva2", lambda: ddc)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", lambda value: None, raising=False)
    monkeypatch.setattr(ctypes, "FormatError", lambda code: "request failed", raising=False)
    return user


def install_audio_com(monkeypatch, state):
    class COMError(Exception):
        hresult = 0x80070490

    class Endpoint:
        def SetMasterVolumeLevelScalar(self, value, _):
            if not state.reject:
                state.volume = value

        def GetMasterVolumeLevelScalar(self):
            return state.volume

        def SetMute(self, value, _):
            if not state.reject:
                state.muted = value

        def GetMute(self):
            return state.muted

    def device_id(value):
        return SimpleNamespace(GetId=lambda: value)

    def endpoints(flow, _):
        devices = [item for item in state.devices if item["flow"] == ("render" if flow == 0 else "capture")]
        return SimpleNamespace(GetCount=lambda: len(devices), Item=lambda index: device_id(devices[index]["id"]))

    def default(flow, role):
        return device_id(state.defaults["render" if flow == 0 else "capture"])

    def set_default(value, role):
        selected = next(item for item in state.devices if item["id"] == value)
        if not state.reject:
            state.defaults[selected["flow"]] = value
        return 0

    com = ModuleType("comtypes")
    com.CoInitialize = lambda: None
    com.CoUninitialize = lambda: None
    com.COMError = COMError
    com.IUnknown = object
    com.GUID = lambda value: value
    com.COMMETHOD = lambda *args: args
    com.HRESULT = ctypes.c_long
    com.CLSCTX_ALL = 23
    com.CoCreateInstance = lambda *args: SimpleNamespace(SetDefaultEndpoint=set_default)
    pycaw = ModuleType("pycaw.pycaw")
    pycaw.AudioUtilities = SimpleNamespace(
        GetSpeakers=lambda: SimpleNamespace(EndpointVolume=Endpoint()),
        GetDeviceEnumerator=lambda: SimpleNamespace(EnumAudioEndpoints=endpoints, GetDefaultAudioEndpoint=default),
        GetAllDevices=lambda: [SimpleNamespace(id=item["id"], FriendlyName=item["name"]) for item in state.devices],
    )
    monkeypatch.setitem(sys.modules, "comtypes", com)
    monkeypatch.setitem(sys.modules, "pycaw", ModuleType("pycaw"))
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", pycaw)


@pytest.fixture(params=["linux", "windows"])
def desktop(request, monkeypatch):
    name = request.param
    state = DesktopState()
    runner = DesktopRunner(state)
    monkeypatch.setattr("notmyfault.platform.current", lambda: name)
    monkeypatch.setattr("notmyfault.platform.services.default_runner", runner)
    monkeypatch.setattr("notmyfault.platform.capabilities.probe_capability", lambda _: {
        "available": True, "backend": "contract-device", "reason": None, "degraded": False,
    })
    monkeypatch.setattr("notmyfault.platform.linux.desktop.session_type", lambda: "x11")
    monkeypatch.setattr("notmyfault.platform.linux.desktop.desktop_environment", lambda: "unknown")
    native = install_native(monkeypatch, state)
    install_audio_com(monkeypatch, state)
    return SimpleNamespace(name=name, state=state, runner=runner, native=native)


def test_both_registries_implement_every_facade_operation():
    for name in ("linux", "windows"):
        backends = importlib.import_module(f"notmyfault.platform.{name}").BACKENDS
        missing = [(domain, method) for domain, method, _ in operations()
                   if not callable(getattr(backends.get(domain), method, None))]
        assert missing == []
        probes = importlib.import_module(f"notmyfault.platform.{name}.probes").PROBES
        from notmyfault.platform.capabilities import CAPABILITY_IDS

        assert set(probes) == CAPABILITY_IDS


def test_clipboard_text_empty_and_non_text_are_distinct(desktop, monkeypatch):
    if desktop.name == "linux":
        monkeypatch.setattr("notmyfault.platform.linux.desktop.session_type", lambda: "wayland")
    for text in ("a中😀z", ""):
        platform.clipboard.write(text)
        assert platform.clipboard.read() == text
        assert platform.clipboard.read(max_chars=2) == text[:2]
    desktop.state.text = None
    assert platform.clipboard.read() is None
    platform.clipboard.write("clear me")
    platform.clipboard.clear()
    assert platform.clipboard.read() is None
    desktop.state.reject = True
    with pytest.raises(PlatformServiceError) as caught:
        platform.clipboard.read()
    assert caught.value.kind == "failed"


@pytest.mark.parametrize("keys", [["ctrl", "unknown"], ["a", "b"], ["a", "ctrl"], ["ctrl"], []])
def test_keyboard_rejects_invalid_chords(desktop, keys):
    with pytest.raises(PlatformServiceError) as caught:
        platform.keyboard.send_hotkey(keys)
    assert caught.value.kind == "invalid"
    assert desktop.state.inputs == []


def test_keyboard_aliases_send_one_chord(desktop):
    platform.keyboard.send_hotkey(["win", "a"])
    first = list(desktop.state.inputs)
    desktop.state.inputs.clear()
    platform.keyboard.send_hotkey(["super", "a"])
    assert desktop.state.inputs == first
    if desktop.name == "windows":
        assert len(first) == 4
        assert [flags for _, _, flags in first] == [0, 0, 2, 2]
    else:
        assert first == [["key", "--clearmodifiers", "super+a"]]


def test_audio_mute_preserves_volume_and_devices_resolve_names(desktop):
    platform.audio.set_muted(True)
    assert desktop.state.muted is True
    assert desktop.state.volume == 0.37
    platform.audio.set_volume(65)
    assert desktop.state.volume == 0.65
    platform.audio.set_muted(False)
    assert desktop.state.muted is False
    assert platform.audio.devices() == desktop.state.devices
    result = platform.audio.set_default("render", "headphones")
    assert result["device_id"] == "42"
    assert result["device_name"] == "Headphones"
    assert result["previous_device_id"] == "41"
    assert result["changed"] is True
    assert platform.audio.default_devices() == {"render": "42", "capture": "55"}
    assert platform.audio.set_default("render", "42")["changed"] is False


@pytest.mark.parametrize("domain,method,args", [
    ("audio", "set_volume", (-1,)), ("audio", "set_volume", (101,)),
    ("audio", "set_volume", (True,)), ("audio", "set_volume", (2.5,)),
    ("audio", "set_muted", (1,)), ("audio", "set_default", ("wrong", "41")),
    ("audio", "set_default", ("render", "missing")),
    ("display", "set_brightness", (-1,)), ("display", "set_brightness", (101,)),
    ("display", "set_brightness", (True,)), ("display", "set_power", ("wrong",)),
    ("screen", "capture", ("unused.png", "wrong", "png")),
    ("screen", "capture", ("unused.png", "fullscreen", "gif")),
    ("power", "set_profile", ("wrong",)),
    ("window", "show", ("2", 999)),
    ("window", "show", ("2", "unknown")),
    ("window", "show", ("2", 3)),
    ("window", "show", ("2", None)),
    ("window", "show", ("2", [])),
    ("media", "send", ("wrong",)),
])
def test_invalid_arguments_have_one_public_kind(desktop, domain, method, args):
    with pytest.raises(PlatformServiceError) as caught:
        getattr(getattr(platform, domain), method)(*args)
    assert caught.value.kind == "invalid"


def test_audio_rejects_ambiguous_names_and_unapplied_changes(desktop):
    desktop.state.devices.append({"id": "43", "name": "Headphones USB", "flow": "render"})
    with pytest.raises(PlatformServiceError) as caught:
        platform.audio.set_default("render", "headphones")
    assert caught.value.kind == "invalid"
    assert desktop.state.defaults["render"] == "41"
    desktop.state.reject = True
    with pytest.raises(PlatformServiceError) as caught:
        platform.audio.set_volume(70)
    assert caught.value.kind == "failed"


def test_window_ids_geometry_and_pin_state(desktop):
    windows = platform.window.list()
    assert len(windows) == 1
    window = windows[0]
    window_id = window["window_id"]
    assert isinstance(window_id, str)
    assert "hwnd" not in window
    assert platform.window.foreground() == window_id
    assert platform.window.titles() == {window_id: "Document"}
    assert platform.window.info(window_id)["width"] == 800
    assert platform.window.monitors()[0]["work"]["width"] == 1920
    rectangle = {"x": -20, "y": 30, "width": 640, "height": 480}
    platform.window.move(window_id, rectangle)
    actual = platform.window.info(window_id)
    assert {key: actual[key] for key in rectangle} == rectangle
    assert platform.window.set_pinned("pin", "active") == {"state": "pinned", "window_id": window_id}
    assert platform.window.set_pinned("toggle", "active") == {"state": "unpinned", "window_id": window_id}
    for state, expected in [
        ("hide", (False, False, False)),
        ("show", (True, False, False)),
        ("minimize", (True, True, False)),
        ("restore", (True, False, False)),
        ("maximize", (True, False, True)),
        ("restore", (True, False, False)),
    ]:
        platform.window.show(window_id, state)
        actual = platform.window.info(window_id)
        assert tuple(actual[key] for key in ("visible", "minimized", "maximized")) == expected
    platform.window.close(window_id)
    assert platform.window.info(window_id) is None


@pytest.mark.parametrize("method,args", [
    ("show", ("2", "show")), ("activate", ("2",)),
    ("move", ("2", {"x": 0, "y": 0, "width": 10, "height": 10})),
    ("set_opacity", ("2", 50)), ("close", ("2",)),
])
def test_window_control_unavailable_keeps_enumeration_and_pin(desktop, monkeypatch, method, args):
    def probe(capability):
        return {"available": capability != "window.control", "backend": "fake",
                "reason": "控制接口不可用" if capability == "window.control" else None,
                "degraded": False}

    monkeypatch.setattr("notmyfault.platform.capabilities.probe_capability", probe)
    assert platform.window.list()
    assert platform.window.set_pinned("pin", "active")["state"] == "pinned"
    with pytest.raises(PlatformServiceError) as caught:
        getattr(platform.window, method)(*args)
    assert caught.value.kind == "unavailable"
    assert caught.value.capability == "window.control"


@pytest.mark.parametrize("session,tools,available", [
    ("wayland", {"wmctrl", "xdotool"}, False),
    ("x11", set(), False),
    ("x11", {"xdotool"}, False),
    ("x11", {"wmctrl"}, False),
    ("x11", {"wmctrl", "xdotool"}, True),
])
def test_linux_window_control_probe_requires_its_tools(monkeypatch, session, tools, available):
    from notmyfault.platform.linux import probes

    monkeypatch.setattr(probes.desktop, "session_type", lambda: session)
    monkeypatch.setattr(probes, "command_path",
                        lambda *names: next((f"/fake/{name}" for name in names if name in tools), None))
    result = probes.PROBES["window.control"]()
    assert result["available"] is available
    if not available:
        assert result["reason"]
    else:
        assert result["degraded"] is True
        assert "透明度" in result["reason"]


@pytest.mark.parametrize("missing", [None, "ShowWindowAsync", "SetWindowPos", "SetLayeredWindowAttributes"])
def test_windows_window_control_probe_checks_user32(monkeypatch, missing):
    from notmyfault.platform.windows import probes

    class User32:
        def __getattr__(self, name):
            if name == missing:
                raise AttributeError(name)
            return lambda *args: 1

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: User32(), raising=False)
    result = probes.PROBES["window.control"]()
    assert result["available"] is (missing is None)
    if missing:
        assert missing in result["reason"]


def test_capture_produces_requested_image_and_ocr_text(desktop, tmp_path):
    from PIL import Image

    destination = str(tmp_path / "图.png")
    assert platform.screen.capture(destination, "fullscreen", "png") == destination
    with Image.open(destination) as image:
        assert image.format == "PNG"
        assert image.size == (2, 2)
    assert platform.screen.ocr(destination, "chi_sim") == "文字"


def test_power_profile_is_opaque_string_and_rejects_incomplete_custom(desktop):
    result = platform.power.set_profile("balanced")
    assert isinstance(result, str)
    assert result == ("balanced" if desktop.name == "linux" else "381b4222-f694-41f0-9685-ff5bb260df2e")
    with pytest.raises(PlatformServiceError) as caught:
        platform.power.set_profile("custom")
    assert caught.value.kind == "invalid"


def test_display_reports_methods_and_rejects_unapplied_brightness(desktop):
    result = platform.display.set_brightness(60)
    assert result["brightness"] == 60
    assert desktop.state.brightness == 60
    assert isinstance(result["methods"], list) and all(isinstance(item, str) for item in result["methods"])
    assert result["methods"]
    assert isinstance(result["warnings"], list) and all(isinstance(item, str) for item in result["warnings"])
    power = platform.display.set_power("on")
    assert isinstance(power["methods"], list) and power["methods"]
    assert isinstance(power["warnings"], list)
    desktop.state.ignore_brightness = True
    with pytest.raises(PlatformServiceError) as caught:
        platform.display.set_brightness(20)
    assert caught.value.kind == "failed"


def test_media_mute_preserves_volume(desktop):
    platform.media.send("mute")
    assert desktop.state.muted is True
    assert desktop.state.volume == 0.37


def test_document_file_check_distinguishes_readable_and_missing_files(desktop, tmp_path):
    document = tmp_path / "文档.txt"
    document.write_text("content", encoding="utf-8")
    assert platform.desktop.file_in_use(str(document)) is False
    assert platform.desktop.file_in_use(str(tmp_path / "missing.txt")) is True



class _SpeechDesktopRunner:
    def __init__(self):
        self.calls = []

    def which(self, *names):
        return names[0]

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        output = ""
        if "--list-synthesis-voices" in args:
            output = "NAME                 LANGUAGE  VARIANT\nEnglish Voice        en-US     none\n中文声音             zh-CN     none\n"
        elif "list-keys" in args:
            output = "picture-uri\npicture-uri-dark\n"
        elif args[1:2] == ["-c"]:
            capture = io.StringIO()
            stdin = SimpleNamespace(buffer=io.BytesIO(kwargs["input"].encode("utf-8")))
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(sys, "stdin", stdin)
                patch.setattr(sys, "stdout", capture)
                try:
                    exec(args[2], {})
                except SystemExit as error:
                    return SimpleNamespace(returncode=error.code, stdout=capture.getvalue(), stderr="")
        else:
            output = "/isolated/快捷.lnk\n"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


@pytest.fixture(params=["linux", "windows"])
def speech_desktop_platform(request, monkeypatch):
    platform_name = request.param
    runner = _SpeechDesktopRunner()
    speaker = MagicMock()
    token = MagicMock()
    token.GetAttribute.side_effect = lambda name: {"Name": "English Voice", "Language": "409;804"}[name]
    speaker.GetVoices.return_value = [token]
    pythoncom = ModuleType("pythoncom")
    pythoncom.CoInitialize = MagicMock()
    pythoncom.CoUninitialize = MagicMock()
    client = ModuleType("win32com.client")
    client.Dispatch = MagicMock(return_value=speaker)
    package = ModuleType("win32com")
    package.client = client
    for name, module in (("pythoncom", pythoncom), ("win32com", package), ("win32com.client", client)):
        monkeypatch.setitem(sys.modules, name, module)
    return platform_name, runner, speaker


@pytest.mark.parametrize("rate,volume", [(-10, 0), (0, 50), (10, 100)])
@pytest.mark.parametrize("voice", ["", "English", "en-US", "zh-CN"])
def test_speech_desktop_speech_controls(speech_desktop_platform, rate, volume, voice):
    name, runner, speaker = speech_desktop_platform
    module = importlib.import_module(f"notmyfault.platform.{name}.speech")
    assert module.SpeechBackend(runner).speak("中文 --help", rate, volume, voice) is None
    args, kwargs = runner.calls[-1]
    if name == "linux":
        assert args[args.index("--rate") + 1] == str(rate * 10)
        assert args[args.index("--volume") + 1] == str(volume * 2 - 100)
        assert args[-2:] == ["--", "中文 --help"]
        assert "--wait" in args
    else:
        assert kwargs["timeout"] == 60
        assert kwargs["encoding"] == "utf-8"
        assert isinstance(kwargs["input"], str)
        speaker.Speak.assert_called_once_with("中文 --help")
        assert speaker.Rate == rate
        assert speaker.Volume == volume


@pytest.mark.parametrize("rate,volume,voice", [(-11, 100, ""), (11, 100, ""), (0, -1, ""), (0, 101, ""), (True, 100, ""), (0, 1.5, ""), ("1", 100, ""), (0, 100, "missing-voice")])
def test_speech_desktop_speech_invalid(speech_desktop_platform, rate, volume, voice):
    name, runner, speaker = speech_desktop_platform
    backend = importlib.import_module(f"notmyfault.platform.{name}.speech").SpeechBackend(runner)
    with pytest.raises(BackendInvalidError):
        backend.speak("text", rate, volume, voice)
    speaker.Speak.assert_not_called()


@pytest.mark.parametrize("style,gnome,windows", [("fill", "zoom", "10"), ("fit", "scaled", "6"), ("stretch", "stretched", "2"), ("center", "centered", "0"), ("tile", "wallpaper", "0"), ("span", "spanned", "22")])
def test_speech_desktop_wallpaper(speech_desktop_platform, monkeypatch, tmp_path, style, gnome, windows):
    name, runner, _ = speech_desktop_platform
    module = importlib.import_module(f"notmyfault.platform.{name}.desktop")
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    events = []
    if name == "linux":
        monkeypatch.setattr(module, "desktop_environment", lambda: "gnome")
    else:
        registry = MagicMock()
        registry.SetValueEx.side_effect = lambda *args: events.append((args[1], args[-1]))
        monkeypatch.setitem(sys.modules, "winreg", registry)
        user32 = MagicMock()
        user32.SystemParametersInfoW.side_effect = lambda *args: events.append(("SPI", args)) or 1
        monkeypatch.setattr(module._win32, "user32", lambda: user32)
    assert module.DesktopBackend(runner).set_wallpaper(str(image), style) is None
    if name == "linux":
        assert runner.calls[-1][0][-2:] == ["picture-options", gnome]
    else:
        assert events[0][0] == "SPI"
        assert events[1:] == [("WallpaperStyle", windows), ("TileWallpaper", "1" if style == "tile" else "0")]


@pytest.mark.parametrize("style", ["fit", "stretch", "center", "tile", "span"])
def test_speech_desktop_plasma_unsupported(monkeypatch, tmp_path, style):
    from notmyfault.platform.linux import desktop
    monkeypatch.setattr(desktop, "desktop_environment", lambda: "kde")
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    runner = _SpeechDesktopRunner()
    with pytest.raises(BackendInvalidError):
        desktop.DesktopBackend(runner).set_wallpaper(str(image), style)
    assert runner.calls == []


@pytest.mark.parametrize("location", ["desktop", "start_menu", "startup", "path"])
def test_speech_desktop_shortcut(speech_desktop_platform, monkeypatch, tmp_path, location):
    name, runner, _ = speech_desktop_platform
    from pathlib import Path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "Desktop").mkdir()
    backend = importlib.import_module(f"notmyfault.platform.{name}.desktop").DesktopBackend(runner)
    if location == "path":
        with pytest.raises(BackendInvalidError, match="目录参数"):
            backend.create_shortcut("/target", location, "快捷")
        assert runner.calls == []
        return
    result = backend.create_shortcut('/tmp/a "b" %file', location, "快捷 name", '--message "hello world"', "/tmp/icon path")
    assert set(result) == {"shortcut_path"}
    assert isinstance(result["shortcut_path"], str)
    if name == "linux":
        path = Path(result["shortcut_path"])
        expected = {"desktop": tmp_path / "Desktop", "start_menu": tmp_path / "data/applications", "startup": tmp_path / "config/autostart"}[location]
        assert path.parent == expected
        content = path.read_text(encoding="utf-8")
        assert '"--message" "hello world"' in content
        assert "%%file" in content
        assert "Icon=/tmp/icon\\spath" in content
        assert "Path=" not in content
        if sys.platform != "win32":
            assert path.stat().st_mode & 0o777 == 0o755
    else:
        args, kwargs = runner.calls[-1]
        assert kwargs["env"]["NMF_LNK_TARGET"] == '/tmp/a "b" %file'
        assert kwargs["env"]["NMF_LNK_ARGS"] == '--message "hello world"'
        assert kwargs["env"]["NMF_LNK_LOCATION"] == location
        assert kwargs["env"]["NMF_LNK_WORKDIR"] == ""
        assert result == {"shortcut_path": "/isolated/快捷.lnk"}



@pytest.fixture(params=["linux", "windows"])
def session_power_backend(request, monkeypatch):
    platform_name = request.param
    session_module = importlib.import_module(f"notmyfault.platform.{platform_name}.session")
    power_module = importlib.import_module(f"notmyfault.platform.{platform_name}.power")
    state = SimpleNamespace(
        locked=True, failed=False, malformed=False, session="42", idle=0.032,
        battery=None, ac=1, battery_flag=128, percent=255, commands=[],
        exit_flags=[], suspend_flags=[], close_handles=[], freed=[],
        messages=[], monitor=None, destroyed=False, unregistered=False, query_failed=False,
    )
    monkeypatch.setenv("XDG_SESSION_ID", "42")
    monkeypatch.setenv("USER", "alice")
    runner = Mock()
    runner.which.side_effect = lambda name: f"/bin/{name}"

    def run(args, **kwargs):
        state.commands.append(args)
        if state.failed:
            return SimpleNamespace(returncode=1, stdout="", stderr="denied")
        output = ""
        if args[1:2] == ["show-user"]:
            output = f"Display={state.session or ''}\n"
        elif args[1:2] == ["list-sessions"]:
            output = ""
        elif "LockedHint" in args:
            output = "broken" if state.malformed else f"LockedHint={'yes' if state.locked else 'no'}\n"
        elif "GetIdletime" in " ".join(args):
            output = "(uint64 32,)"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    runner.run.side_effect = run
    if platform_name == "linux":
        monkeypatch.setattr(session_module.desktop, "desktop_environment", lambda: "gnome")
        import psutil

        monkeypatch.setattr(psutil, "sensors_battery", lambda: state.battery)
    else:
        api = session_module._win32
        info = api.WTSINFOEXW()
        state.info = info

        def process_session(pid, output):
            output._obj.value = int(state.session)
            return not state.failed

        def query(server, session_id, info_class, buffer, length):
            assert info_class == 25
            info.Level = 2 if state.malformed else 1
            info.Data.WTSInfoExLevel1.SessionFlags = 0 if state.locked else 1
            buffer._obj.value = ctypes.addressof(info)
            length._obj.value = ctypes.sizeof(info)
            return not state.query_failed

        def last_input(output):
            output._obj.dwTime = 0xFFFFFFF0
            return not state.failed

        def battery(output):
            output._obj.ACLineStatus = state.ac
            output._obj.BatteryFlag = state.battery_flag
            output._obj.BatteryLifePercent = state.percent
            return not state.failed

        def register(window_class):
            return 1

        def dispatch(message):
            state.monitor._callback(1000, api.WM_POWERBROADCAST, state.messages.pop(0), 0)
            return 0

        def destroy(hwnd):
            assert state.monitor._callback is not None
            state.monitor._callback(hwnd, 0, 0, 0)
            state.destroyed = True
            return 1

        def unregister(*args):
            state.unregistered = True
            return 1

        user = SimpleNamespace(
            LockWorkStation=lambda: not state.failed,
            GetLastInputInfo=last_input,
            ExitWindowsEx=lambda flags, reason: state.exit_flags.append(flags) or 1,
            RegisterClassW=register, CreateWindowExW=lambda *args: 1000,
            PeekMessageW=lambda *args: bool(state.messages),
            TranslateMessage=lambda *args: 1, DispatchMessageW=dispatch,
            DefWindowProcW=lambda *args: 0,
            DestroyWindow=destroy, UnregisterClassW=unregister,
        )
        kernel = SimpleNamespace(
            ProcessIdToSessionId=process_session, GetTickCount=lambda: 16,
            GetSystemPowerStatus=battery, GetCurrentProcess=lambda: 10,
            SetLastError=lambda value: None, GetLastError=lambda: 0,
            CloseHandle=lambda token: state.close_handles.append(token.value) or 1,
            GetModuleHandleW=lambda *args: 10, GetCurrentThreadId=lambda: 12,
        )

        def open_token(process, flags, token):
            token._obj.value = 17
            return 1

        monkeypatch.setattr(api, "user32", lambda: user)
        monkeypatch.setattr(api, "kernel32", lambda: kernel)
        monkeypatch.setattr(api, "wtsapi32", lambda: SimpleNamespace(
            WTSQuerySessionInformationW=query,
            WTSFreeMemory=lambda pointer: state.freed.append(pointer.value),
        ))
        monkeypatch.setattr(api, "advapi32", lambda: SimpleNamespace(
            OpenProcessToken=open_token, LookupPrivilegeValueW=lambda *args: 1,
            AdjustTokenPrivileges=lambda *args: 1,
        ))
        monkeypatch.setattr(api, "powrprof", lambda: SimpleNamespace(
            SetSuspendState=lambda *flags: state.suspend_flags.append(flags) or 1,
        ))
    return platform_name, session_module.SessionBackend(runner), power_module.PowerBackend(runner), state


def test_session_power_lock_and_idle_contract(session_power_backend):
    name, session, power, state = session_power_backend
    assert session.session_id() == "42"
    session.lock()
    assert session.is_locked() is True
    state.locked = False
    assert session.is_locked() is False
    assert session.idle_seconds() == pytest.approx(0.032)
    state.failed = True
    with pytest.raises(BackendFailedError):
        session.is_locked()
    with pytest.raises(BackendFailedError):
        session.lock()
    state.failed = False
    if name == "windows":
        state.query_failed = True
        with pytest.raises(BackendFailedError):
            session.is_locked()
        state.query_failed = False
    state.malformed = True
    with pytest.raises(BackendFailedError):
        session.is_locked()
    if name == "windows":
        assert len(state.freed) == 4


def test_session_lookup_and_query_errors(session_power_backend, monkeypatch):
    name, session, power, state = session_power_backend
    monkeypatch.delenv("XDG_SESSION_ID")
    assert session.session_id() == "42"
    state.failed = True
    with pytest.raises(BackendFailedError):
        session.session_id()
    if name == "linux":
        state.failed = False
        state.session = None
        assert session.session_id() is None
        with pytest.raises(BackendFailedError):
            session.is_locked()


def test_power_battery_absent_and_percent_contract(session_power_backend):
    name, session, power, state = session_power_backend
    assert power.battery() == {"on_battery": False, "percent": None}
    state.battery = SimpleNamespace(power_plugged=False, percent=37.4)
    state.ac, state.battery_flag, state.percent = 0, 0, 37
    assert power.battery() == {"on_battery": True, "percent": 37}
    if name == "windows":
        state.percent = 255
        assert power.battery() == {"on_battery": True, "percent": None}
        state.ac = 255
        assert power.battery() == {"on_battery": False, "percent": None}
        state.failed = True
        with pytest.raises(BackendFailedError):
            power.battery()


@pytest.mark.parametrize("action", ["shutdown", "restart", "logoff", "sleep", "hibernate"])
def test_power_force_command_contract(session_power_backend, action):
    name, session, power, state = session_power_backend
    power.system(action, force=True)
    if name == "linux":
        expected = {
            "shutdown": ["/bin/systemctl", "poweroff", "--ignore-inhibitors"],
            "restart": ["/bin/systemctl", "reboot", "--ignore-inhibitors"],
            "sleep": ["/bin/systemctl", "suspend", "--ignore-inhibitors"],
            "hibernate": ["/bin/systemctl", "hibernate", "--ignore-inhibitors"],
            "logoff": ["/bin/loginctl", "terminate-session", "42"],
        }
        assert state.commands[-1] == expected[action]
    elif action in ("sleep", "hibernate"):
        assert state.suspend_flags == [(action == "hibernate", False, False)]
        assert state.close_handles == [17]
    else:
        assert state.exit_flags == [{"shutdown": 13, "restart": 6, "logoff": 4}[action]]
        assert state.close_handles == ([] if action == "logoff" else [17])
    with pytest.raises(BackendInvalidError):
        power.system("unknown")


def test_power_resume_consumption_and_close_contract(session_power_backend, monkeypatch):
    name, session, power, state = session_power_backend
    disconnected = []
    if name == "linux":
        dbus = types.ModuleType("dbus_next")
        dbus.BusType = SimpleNamespace(SYSTEM=1)
        dbus.MessageType = SimpleNamespace(SIGNAL=1, ERROR=2)
        dbus.Message = lambda **kwargs: SimpleNamespace(**kwargs)

        class Bus:
            async def connect(self):
                return self

            def add_message_handler(self, handler):
                self.handler = handler

            async def call(self, message):
                self.handler(SimpleNamespace(
                    message_type=1, interface="org.freedesktop.login1.Manager",
                    member="PrepareForSleep", body=[False],
                ))
                return SimpleNamespace(message_type=0)

            def disconnect(self):
                disconnected.append(True)

        aio = types.ModuleType("dbus_next.aio")
        aio.MessageBus = lambda **kwargs: Bus()
        monkeypatch.setitem(sys.modules, "dbus_next", dbus)
        monkeypatch.setitem(sys.modules, "dbus_next.aio", aio)
    monitor = power.resume_monitor()
    state.monitor = monitor
    monitor.start()
    if name == "windows":
        from notmyfault.platform.windows import _win32

        state.messages.append(_win32.PBT_APMRESUMEAUTOMATIC)
    try:
        assert monitor.consume_resume() is True
        assert monitor.consume_resume() is False
    finally:
        monitor.stop()
    if name == "linux":
        assert disconnected == [True]
        assert monitor._thread is None
        assert monitor._loop is None
    else:
        assert state.destroyed is True
        assert state.unregistered is True
        assert monitor._callback is None
        assert monitor._hwnd is None


def test_windows_wts_buffer_validation(monkeypatch):
    from notmyfault.platform.windows import session as module

    api = module._win32
    info = api.WTSINFOEXW()
    info.Level = 1
    info.Data.WTSInfoExLevel1.SessionFlags = -1
    freed = []
    length = ctypes.sizeof(info)

    def query(server, session_id, info_class, buffer, returned):
        buffer._obj.value = ctypes.addressof(info)
        returned._obj.value = length
        return 1

    monkeypatch.setattr(module.SessionBackend, "session_id", lambda self: "42")
    monkeypatch.setattr(api, "wtsapi32", lambda: SimpleNamespace(
        WTSQuerySessionInformationW=query,
        WTSFreeMemory=lambda pointer: freed.append(pointer.value),
    ))
    backend = module.SessionBackend(Mock())
    with pytest.raises(BackendFailedError):
        backend.is_locked()
    length = 4
    with pytest.raises(BackendFailedError):
        backend.is_locked()
    assert freed == [ctypes.addressof(info)] * 2



@pytest.fixture(params=["linux", "windows"])
def hotkey_backend(request, monkeypatch):
    module = importlib.import_module(f"notmyfault.platform.{request.param}.hotkey")
    state = SimpleNamespace(displays=[], registrations=[], unregistered=[], messages=[], down=set(), fail=False)

    class Lock:
        depth = 0

        def __enter__(self):
            self.depth += 1

        def __exit__(self, *args):
            self.depth -= 1

    lock = Lock()
    monkeypatch.setattr(module, "NATIVE_LOCK", lock)
    ticks = iter(index * 0.01 for index in range(10000))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda seconds: None))

    def checked():
        assert lock.depth > 0
        if state.fail:
            raise OSError("native failure")

    if request.param == "windows":
        monkeypatch.setattr(module, "_LISTENERS", {})
        monkeypatch.setattr(module, "_next_id", 1)

        def register(hwnd, key_id, modifiers, vk):
            checked()
            state.registrations.append((key_id, modifiers, vk))
            return True

        def unregister(hwnd, key_id):
            checked()
            state.unregistered.append(key_id)
            return True

        def peek(pointer, hwnd, minimum, maximum, remove):
            checked()
            for index, (message, key_id) in enumerate(state.messages):
                if minimum <= message <= maximum:
                    state.messages.pop(index)
                    pointer._obj.message = message
                    pointer._obj.wParam = key_id
                    return True
            return False

        def key_state(vk):
            checked()
            return 0x8000 if vk in state.down else 0

        dll = SimpleNamespace(RegisterHotKey=register, UnregisterHotKey=unregister,
                              PeekMessageW=peek, GetAsyncKeyState=key_state)
        monkeypatch.setattr(module._win32, "user32", lambda: dll)

        def press(listener, count):
            state.messages.extend([(module._win32.WM_HOTKEY, listener._id)] * count)

        def capture_input(mode):
            state.down = {0x11, 0x5B, 0x41} if mode == "known" else ({0x1B} if mode == "escape" else {0x11, 0xFF})
    else:
        x = SimpleNamespace(KeyPress=2, KeyRelease=3, LockMask=2, Mod2Mask=16,
                            GrabModeAsync=1, CurrentTime=0, GrabSuccess=0)
        names = {"Escape": 0xFF1B}

        def string_to_keysym(name):
            checked()
            if len(name) == 1:
                return ord(name)
            return names.setdefault(name, len(names) + 0x1000)

        def keysym_to_string(keysym):
            checked()
            return chr(keysym) if keysym < 128 else "unknown_native_key"

        class Root:
            def __init__(self):
                self.ungrabs = []
                self.keyboard_ungrabs = 0

            def grab_key(self, keycode, mask, *args, **kwargs):
                checked()
                state.registrations.append((keycode, mask))

            def ungrab_key(self, keycode, mask):
                assert lock.depth > 0
                self.ungrabs.append((keycode, mask))

            def grab_keyboard(self, *args):
                checked()
                return x.GrabSuccess

            def ungrab_keyboard(self, *args):
                assert lock.depth > 0
                self.keyboard_ungrabs += 1

        class Display:
            def __init__(self):
                checked()
                self.root = Root()
                self.closed = 0
                self.events = list(state.capture_events) if hasattr(state, "capture_events") else []
                state.displays.append(self)

            def screen(self):
                checked()
                return SimpleNamespace(root=self.root)

            def sync(self):
                assert lock.depth > 0

            def keysym_to_keycode(self, keysym):
                checked()
                return keysym

            def keycode_to_keysym(self, keycode, index):
                checked()
                return keycode

            def pending_events(self):
                checked()
                return len(self.events)

            def next_event(self):
                checked()
                return self.events.pop(0)

            def ungrab_keyboard(self, timestamp):
                self.root.ungrab_keyboard(timestamp)

            def close(self):
                assert lock.depth > 0
                self.closed += 1

        xlib = ModuleType("Xlib")
        xlib.X = x
        xlib.XK = SimpleNamespace(string_to_keysym=string_to_keysym, keysym_to_string=keysym_to_string)
        display_module = ModuleType("Xlib.display")
        display_module.Display = Display
        monkeypatch.setitem(sys.modules, "Xlib", xlib)
        monkeypatch.setitem(sys.modules, "Xlib.display", display_module)

        def press(listener, count):
            listener._display.events.extend(
                SimpleNamespace(type=x.KeyPress, detail=listener._keycode, time=index + 1)
                for index in range(count)
            )

        def capture_input(mode):
            keycode = ord("a") if mode == "known" else (0xFF1B if mode == "escape" else 0xFE00)
            state.capture_events = [SimpleNamespace(type=x.KeyPress, detail=keycode, state=4 | 64)]

    return SimpleNamespace(name=request.param, module=module, backend=module.HotkeyBackend(),
                           state=state, lock=lock, press=press, capture_input=capture_input)


def test_hotkey_counts_every_press_and_closes_once(hotkey_backend):
    host = hotkey_backend
    listener = host.backend.register(["ctrl", "a"])
    try:
        host.press(listener, 2)
        assert listener.poll() == 2
        assert listener.poll() == 0
    finally:
        listener.close()
    listener.close()
    assert listener.poll() == 0
    assert host.lock.depth == 0
    if host.name == "windows":
        assert host.state.unregistered == [listener._id]
        assert host.state.registrations[0][1] & host.module._win32.MOD_NOREPEAT
    else:
        display = host.state.displays[0]
        assert display.closed == 1
        assert display.root.ungrabs == host.state.registrations
        assert len(display.root.ungrabs) == 4


def test_hotkey_aliases_register_the_same_chord(hotkey_backend):
    host = hotkey_backend
    first = host.backend.register(["win", "return"])
    first_calls = list(host.state.registrations)
    first.close()
    host.state.registrations.clear()
    second = host.backend.register(["super", "enter"])
    try:
        if host.name == "windows":
            assert first_calls[0][1:] == host.state.registrations[0][1:]
        else:
            assert first_calls == host.state.registrations
    finally:
        second.close()


@pytest.mark.parametrize("keys", [["ctrl", "unknown"], ["ctrl", "a", "b"], ["a"], ["ctrl"]])
def test_hotkey_rejects_invalid_chords(hotkey_backend, keys):
    with pytest.raises(BackendInvalidError):
        hotkey_backend.backend.register(keys)
    assert hotkey_backend.state.registrations == []


def test_hotkey_capture_returns_canonical_names_and_releases_keyboard(hotkey_backend):
    host = hotkey_backend
    host.capture_input("known")
    assert host.backend.capture(0.2) == ["ctrl", "super", "a"]
    assert host.lock.depth == 0
    if host.name == "linux":
        display = host.state.displays[-1]
        assert display.root.keyboard_ungrabs == 1
        assert display.closed == 1


@pytest.mark.parametrize("mode", ["escape", "unknown"])
def test_hotkey_capture_cancel_or_unknown_returns_empty(hotkey_backend, mode):
    host = hotkey_backend
    host.capture_input(mode)
    assert host.backend.capture(0.2) == []
    assert host.lock.depth == 0
    if host.name == "linux":
        display = host.state.displays[-1]
        assert display.root.keyboard_ungrabs == 1
        assert display.closed == 1


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), -float("inf")])
def test_hotkey_capture_rejects_invalid_timeout(hotkey_backend, timeout):
    with pytest.raises(BackendInvalidError):
        hotkey_backend.backend.capture(timeout)


def test_hotkey_native_failure_is_not_a_missing_press(hotkey_backend):
    host = hotkey_backend
    listener = host.backend.register(["ctrl", "a"])
    host.state.fail = True
    try:
        with pytest.raises(BackendFailedError, match="native failure"):
            listener.poll()
    finally:
        host.state.fail = False
        listener.close()
    assert host.lock.depth == 0


def test_hotkey_capture_failure_releases_keyboard(hotkey_backend, monkeypatch):
    host = hotkey_backend
    if host.name == "linux":
        original = sys.modules["Xlib.display"].Display

        def display_type():
            display = original()
            display.pending_events = lambda: (_ for _ in ()).throw(OSError("native failure"))
            return display

        monkeypatch.setattr(sys.modules["Xlib.display"], "Display", display_type)
    else:
        host.state.fail = True
    with pytest.raises(BackendFailedError, match="native failure"):
        host.backend.capture(0.2)
    assert host.lock.depth == 0
    if host.name == "linux":
        assert host.state.displays[-1].root.keyboard_ungrabs == 1
        assert host.state.displays[-1].closed == 1


def test_hotkey_listeners_do_not_consume_each_others_presses(hotkey_backend):
    host = hotkey_backend
    first = host.backend.register(["ctrl", "a"])
    second = host.backend.register(["ctrl", "b"])
    try:
        host.press(first, 2)
        host.press(second, 1)
        assert first.poll() == 2
        assert second.poll() == 1
        assert first.poll() == second.poll() == 0
        if host.name == "windows":
            assert first._id != second._id
    finally:
        first.close()
        second.close()


def test_hotkey_x11_filters_paired_autorepeat(hotkey_backend):
    host = hotkey_backend
    if host.name != "linux":
        return
    listener = host.backend.register(["ctrl", "a"])
    try:
        host.press(listener, 1)
        listener._display.events.extend([
            SimpleNamespace(type=3, detail=listener._keycode, time=9),
            SimpleNamespace(type=2, detail=listener._keycode, time=9),
        ])
        assert listener.poll() == 1
    finally:
        listener.close()


def test_hotkey_extension_commits_facade_capture(monkeypatch):
    module = importlib.import_module("notmyfault.triggers.hotkey.extension")
    calls, cleanups, statuses = [], [], []
    context = SimpleNamespace(
        register_cleanup=cleanups.append,
        session=SimpleNamespace(set_status=statuses.append),
        commit_value=lambda value: {"value": value},
    )
    monkeypatch.setattr(module, "platform", SimpleNamespace(hotkey=SimpleNamespace(
        capture=lambda timeout: calls.append(timeout) or ["ctrl", "super", "a"],
    )))
    assert module.capture_hotkey(context, {}) == {"value": "ctrl+super+a"}


def test_hotkey_extension_cleanup_cancels_before_commit(monkeypatch):
    module = importlib.import_module("notmyfault.triggers.hotkey.extension")
    cleanups, commits = [], []

    def capture(timeout):
        cleanups[0]()
        return ["ctrl", "a"]

    context = SimpleNamespace(
        register_cleanup=cleanups.append,
        session=SimpleNamespace(set_status=lambda text: None),
        commit_value=commits.append,
        result=lambda data, close: {"data": data, "close": close},
    )
    monkeypatch.setattr(module, "platform", SimpleNamespace(hotkey=SimpleNamespace(capture=capture)))
    assert module.capture_hotkey(context, {}) == {"data": {"cancelled": True}, "close": True}
    assert commits == []


class _P3WifiRunner:
    def __init__(self, platform_name, *, target, password, failure=False, saved=False):
        self.platform_name = platform_name
        self.target = target
        self.password = password
        self.failure = failure
        self.saved = saved
        self.active = None
        self.calls = []
        self.secret_paths = []
        self.secret_modes = []
        self.query_fail = False
        self.readback_stuck = False

    def which(self, *_names):
        return "nmcli"

    def run(self, args, **kwargs):
        self.calls.append(args)
        assert all(self.password not in value for value in args)
        stdout = ""
        if args[0] == "nmcli":
            if args[1:4] == ["-t", "-f", "ACTIVE,SSID"]:
                assert args[-2:] == ["ifname", "wifi2"]
                if self.query_fail:
                    return SimpleNamespace(returncode=1, stdout="", stderr="query failed")
                escaped = (self.active or "").replace("\\", "\\\\").replace(":", "\\:")
                stdout = f"yes:{escaped}\n" if self.active else "no:Other\n"
            elif args[1:4] == ["-t", "-f", "UUID,TYPE"]:
                stdout = "saved-uuid:wifi\n" if self.saved else ""
            elif args[1:3] == ["-g", "802-11-wireless.ssid"]:
                stdout = self.target + "\n"
            elif args[1:3] == ["connection", "up"]:
                assert "ifname" in args and args[args.index("ifname") + 1] == "wifi2"
                path = Path(args[args.index("passwd-file") + 1])
                self.secret_paths.append(path)
                assert self.secret_modes == [0o600]
                if os.name != "nt":
                    assert stat.S_IMODE(path.stat().st_mode) == 0o600
                assert path.read_text(encoding="utf-8") == f"wifi.psk:{self.password}\n"
                if self.failure:
                    raise BackendFailedError(f"unexpected diagnostic {self.password}")
                if not self.readback_stuck:
                    self.active = self.target
            elif args[1:3] == ["device", "disconnect"]:
                self.active = None
        elif args[1:4] == ["wlan", "add", "profile"]:
            assert "user=current" in args
            path = Path(next(value.split("=", 1)[1] for value in args if value.startswith("filename=")))
            self.secret_paths.append(path)
            if os.name != "nt":
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
            root = ET.fromstring(path.read_bytes())
            assert root.findtext("./{*}SSIDConfig/{*}SSID/{*}hex") == self.target.encode("utf-8").hex().upper()
            assert root.findtext("./{*}SSIDConfig/{*}SSID/{*}name") == self.target
            assert root.findtext("./{*}MSM/{*}security/{*}sharedKey/{*}keyMaterial") == self.password
            if self.failure:
                return SimpleNamespace(returncode=1, stdout=self.password, stderr="")
        elif args[1:3] == ["wlan", "connect"]:
            assert "interface=wifi2" in args
            if not self.readback_stuck:
                self.active = self.target
        elif args[1:3] == ["wlan", "disconnect"]:
            assert "interface=wifi2" in args
            self.active = None
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


@pytest.fixture(params=["linux", "windows"])
def p3_wifi_backend(request, monkeypatch, tmp_path):
    name = request.param
    module = importlib.import_module(f"notmyfault.platform.{name}.wifi")
    password = "example-<&-password"
    target = ' 家中:WiFi\\"2 '
    runner = _P3WifiRunner(name, target=target, password=password)
    backend = module.WifiBackend(runner)
    if name == "linux":
        def private_mode(descriptor, mode):
            assert os.fstat(descriptor).st_size == 0
            runner.secret_modes.append(mode)
            if os.name != "nt":
                os.fchmod(descriptor, mode)

        monkeypatch.setattr(module, "os", SimpleNamespace(
            environ=os.environ, fdopen=os.fdopen, fchmod=private_mode, unlink=os.unlink,
        ))
        monkeypatch.setattr(module, "tempfile", SimpleNamespace(
            mkstemp=lambda **kwargs: tempfile.mkstemp(dir=tmp_path, **kwargs),
        ))
    if name == "windows":
        monkeypatch.setattr(module.w, "kernel32", lambda: SimpleNamespace(GetConsoleOutputCP=lambda: 65001, GetOEMCP=lambda: 437))
        guid = "00000002-0000-0000-0000-000000000000"
        monkeypatch.setattr(backend, "_saved_profile", lambda *_: ("saved", "wifi2", guid) if runner.saved else None)
        monkeypatch.setattr(backend, "_select", lambda interface: ("wifi2", guid))
        monkeypatch.setattr(backend, "_identity", lambda record: ("wifi2", guid))
        allocations = {}

        def query(_handle, _guid, _opcode, _reserved, size, output, _type):
            if runner.query_fail:
                return 123
            if runner.active is None:
                return module.w.WLAN_INVALID_STATE
            connection = module.w.WlanConnectionInfo()
            connection.state = module.w.WLAN_CONNECTED
            raw = runner.active.encode("utf-8")
            connection.association.ssid.length = len(raw)
            connection.association.ssid.data[:len(raw)] = raw
            address = ctypes.addressof(connection)
            allocations[address] = connection
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = address
            ctypes.cast(size, ctypes.POINTER(module.w.WLAN_DWORD))[0] = ctypes.sizeof(connection)
            return 0

        @contextmanager
        def interfaces():
            record = module.w.WlanInterfaceInfo()
            record.guid[0] = 2
            record.state = module.w.WLAN_CONNECTED
            yield SimpleNamespace(WlanQueryInterface=query, WlanFreeMemory=lambda pointer: allocations.pop(pointer.value)), None, [record]
            assert not allocations

        monkeypatch.setattr(backend, "_interfaces", interfaces)

        @contextmanager
        def private_profile(content):
            descriptor, path = tempfile.mkstemp(dir=tmp_path)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                yield path
            finally:
                os.unlink(path)

        monkeypatch.setattr(module, "_private_profile", private_profile)
    return name, module, backend, runner


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("saved", [False, True])
def test_p3_wifi_password_transport_and_cleanup(p3_wifi_backend, failure, saved):
    name, _module, backend, runner = p3_wifi_backend
    runner.failure = failure
    runner.saved = saved
    # Windows 已保存配置沿用系统凭据，不导入密码文件。
    should_fail = failure and not (name == "windows" and saved)
    if should_fail:
        with pytest.raises(BackendError) as error:
            backend.connect(runner.target, password=runner.password, interface="wifi2")
        assert error.value.kind == "failed"
        assert runner.password not in str(error.value)
        assert error.value.__suppress_context__
    else:
        assert backend.connect(runner.target, password=runner.password, interface="wifi2") == {
            "ssid": runner.target, "connected": True, "method": "nmcli" if name == "linux" else "netsh",
        }
    assert runner.secret_paths or (name == "windows" and saved)
    assert all(not path.exists() for path in runner.secret_paths)
    assert all(runner.password not in arg for call in runner.calls for arg in call)
    if name == "linux" and not saved:
        added = next(call for call in runner.calls if call[1:3] == ["connection", "add"])
        assert added[added.index("wifi-sec.key-mgmt") + 1] == "wpa-psk"


def test_p3_wifi_readback_target_and_failure(p3_wifi_backend, monkeypatch):
    name, module, backend, runner = p3_wifi_backend
    runner.saved = True
    runner.readback_stuck = True
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    ))
    with pytest.raises(BackendError) as error:
        backend.connect(runner.target, password=runner.password, interface="wifi2")
    assert error.value.kind == "failed"
    assert all(not path.exists() for path in runner.secret_paths)
    runner.query_fail = True
    with pytest.raises(BackendError) as error:
        backend.connect(runner.target, interface="wifi2")
    assert error.value.kind == "failed"


def test_p3_wifi_query_and_disconnect_values(p3_wifi_backend):
    name, _module, backend, runner = p3_wifi_backend
    assert backend.current_ssid("wifi2") is None
    runner.active = runner.target
    assert backend.current_ssid("wifi2") == runner.target
    assert backend.disconnect("wifi2") == {"ssid": "", "connected": False, "method": "nmcli" if name == "linux" else "netsh"}
    assert backend.current_ssid("wifi2") is None


class _P3BluetoothRunner:
    def __init__(self, platform_name):
        self.platform_name = platform_name
        self.current = "off"
        self.failure = False
        self.stuck = False
        self.timeouts = []

    def which(self, *_names):
        return "bluetoothctl"

    def run(self, args, **kwargs):
        import json
        self.timeouts.append(kwargs["timeout"])
        if self.failure:
            return SimpleNamespace(returncode=1, stdout="query failed", stderr="query failed")
        if self.platform_name == "linux":
            if args[1] == "power" and not self.stuck:
                self.current = args[2]
            stdout = "No default controller available" if self.current == "unavailable" else f"Powered: {'yes' if self.current == 'on' else 'no'}"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if self.current == "unavailable":
            payload = {"ok": False, "code": "no_radio"}
        else:
            operation = args[-1]
            before = self.current
            if operation != "query" and not self.stuck:
                self.current = operation
            payload = {"ok": True, "state": self.current, "changed": before != self.current, "radios": []}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


@pytest.fixture(params=["linux", "windows"])
def p3_bluetooth_backend(request, monkeypatch):
    module = importlib.import_module(f"notmyfault.platform.{request.param}.bluetooth")
    clock = [0.0]
    if request.param == "linux":
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    runner = _P3BluetoothRunner(request.param)
    return module.BluetoothBackend(runner), runner


def test_p3_bluetooth_states_and_result_types(p3_bluetooth_backend):
    backend, runner = p3_bluetooth_backend
    assert backend.state() == "off"
    result = backend.set(True)
    assert result["state"] == "on"
    assert result["changed"] is True
    assert isinstance(result["method"], str)
    assert backend.set(True)["changed"] is False
    assert backend.set(False)["state"] == "off"
    assert all(0 < timeout <= 30 for timeout in runner.timeouts)
    runner.current = "unavailable"
    assert backend.state() == "unavailable"
    with pytest.raises(BackendError) as error:
        backend.set(True)
    assert error.value.kind == "unavailable"


@pytest.mark.parametrize("failure", ["query", "unchanged", "invalid"])
def test_p3_bluetooth_failures(p3_bluetooth_backend, failure):
    backend, runner = p3_bluetooth_backend
    runner.failure = failure == "query"
    runner.stuck = failure == "unchanged"
    with pytest.raises(BackendError) as error:
        if failure == "query":
            backend.state()
        else:
            backend.set("on" if failure == "invalid" else True)
    assert error.value.kind == ("invalid" if failure == "invalid" else "failed")
