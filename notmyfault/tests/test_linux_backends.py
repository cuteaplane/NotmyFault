import tempfile
import subprocess
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from notmyfault.platform.backends import (
    AudioBackend,
    BackendFailedError,
    BackendMissingError,
    BackendPermissionDeniedError,
    BackendUnsupportedError,
    ClipboardBackend,
    CommandRunner,
    DisplayBackend,
    InputBackend,
    PowerProfileBackend,
    OcrBackend,
    ScreenshotBackend,
    WindowBackend,
)


class FakeRunner:
    """记录调用并回放预设结果的假命令执行端"""

    def __init__(self, paths=None, results=None):
        self.paths = paths or {}
        self.results = list(results or [])
        self.calls = []

    def which(self, *names):
        for name in names:
            if name in self.paths:
                return self.paths[name]
        return None

    def run(self, args, timeout=10, input=None, env=None, encoding=None, creationflags=0):
        self.calls.append({
            "args": args,
            "timeout": timeout,
            "input": input,
            "env": env,
            "encoding": encoding,
            "creationflags": creationflags,
        })
        if self.results:
            return self.results.pop(0)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


class LinuxPathTest(unittest.TestCase):
    def setUp(self):
        patcher = patch("notmyfault.platform.backends.sys.platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)


def ok(stdout="", stderr="", returncode=0):
    return types.SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr=stderr
    )


class CommandRunnerTests(unittest.TestCase):
    def test_timeout_raises_backend_failed(self):
        runner = CommandRunner()
        with patch("notmyfault.platform.backends.subprocess.run", side_effect=subprocess.TimeoutExpired(["tool"], 3)):
            with self.assertRaises(BackendFailedError) as ctx:
                runner.run(["tool"], timeout=3)
        self.assertIn("超时", str(ctx.exception))

    def test_output_limit_preserves_short_output_and_bounds_long_output(self):
        runner = CommandRunner()
        for count in (4, 8, 100000):
            with self.subTest(count=count):
                result = runner.read_output(
                    [sys.executable, "-c", f"import sys; sys.stdout.write('a' * {count})"],
                    max_chars=8, timeout=5,
                )
                self.assertEqual(result.stdout, "a" * min(count, 8))
                self.assertEqual(result.returncode, 0)

    def test_missing_command_raises_backend_missing(self):
        runner = CommandRunner()
        with self.assertRaises(BackendMissingError):
            runner.run(["definitely-not-a-command-xyz"])

    def test_errors_carry_kind(self):
        cases = [
            (BackendUnsupportedError("x"), "unsupported"),
            (BackendMissingError("x"), "backend_missing"),
            (BackendPermissionDeniedError("x"), "permission_denied"),
            (BackendFailedError("x"), "backend_failed"),
        ]
        for error, kind in cases:
            self.assertEqual(error.kind, kind)


class InputBackendTests(LinuxPathTest):
    def test_type_text_uses_xdotool_with_clearmodifiers(self):
        runner = FakeRunner(paths={"xdotool": "/usr/bin/xdotool"})
        InputBackend(runner).type_text("a中😀")
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xdotool", "type", "--clearmodifiers", "--", "a中😀"],
        )
        self.assertEqual(runner.calls[0]["timeout"], 30)

    def test_type_text_uses_ydotool_without_flags(self):
        runner = FakeRunner(paths={"ydotool": "/usr/bin/ydotool"})
        InputBackend(runner).type_text("hi")
        self.assertEqual(
            runner.calls[0]["args"], ["/usr/bin/ydotool", "type", "--", "hi"]
        )

    def test_ydotool_sets_socket_env_when_present(self):
        runner = FakeRunner(paths={"ydotool": "/usr/bin/ydotool"})
        with patch("notmyfault.platform.linux_support.session_type", return_value="wayland"), \
                patch.dict(
                    "os.environ",
                    {"XDG_RUNTIME_DIR": "/run/user/1000", "YDOTOOL_SOCKET": ""},
                    clear=False,
                ), \
                patch(
                    "notmyfault.platform.backends.os.path.exists",
                    side_effect=lambda p: p == "/run/user/1000/.ydotool_socket",
                ):
            InputBackend(runner).type_text("hi")
        self.assertEqual(
            runner.calls[0]["env"].get("YDOTOOL_SOCKET"),
            "/run/user/1000/.ydotool_socket",
        )

    def test_ydotool_failure_mentions_socket(self):
        runner = FakeRunner(
            paths={"ydotool": "/usr/bin/ydotool"},
            results=[ok(stderr="connect: No such file or directory", returncode=1)],
        )
        with patch("notmyfault.platform.linux_support.session_type", return_value="wayland"), \
                patch.dict("os.environ", {"YDOTOOL_SOCKET": "/tmp/.ydotool_socket"}, clear=False):
            with self.assertRaises(BackendFailedError) as ctx:
                InputBackend(runner).type_text("hi")
        self.assertIn("ydotoold", str(ctx.exception))
        self.assertIn("/tmp/.ydotool_socket", str(ctx.exception))

    def test_hotkey_maps_keys_for_xdotool(self):
        runner = FakeRunner(paths={"xdotool": "/usr/bin/xdotool"})
        InputBackend(runner).send_hotkey(["ctrl", "win", "enter"])
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xdotool", "key", "--clearmodifiers", "ctrl+super+Return"],
        )
        with patch("notmyfault.platform.linux_support.session_type", return_value="wayland"):
            runner.paths["ydotool"] = "/usr/bin/ydotool"
            InputBackend(runner).send_hotkey(["ctrl", "win", "enter"])
        self.assertEqual(runner.calls[-1]["args"],
                         ["/usr/bin/ydotool", "key", "29:1", "125:1", "28:1", "28:0", "125:0", "29:0"])

    def test_missing_tool_raises_backend_missing(self):
        runner = FakeRunner()
        with self.assertRaises(BackendMissingError) as ctx:
            InputBackend(runner).type_text("hi")
        self.assertIn("依赖缺失", str(ctx.exception))

    def test_nonzero_exit_raises_backend_failed(self):
        runner = FakeRunner(
            paths={"xdotool": "/usr/bin/xdotool"},
            results=[ok(stderr="boom", returncode=1)],
        )
        with self.assertRaises(BackendFailedError) as ctx:
            InputBackend(runner).type_text("hi")
        self.assertIn("boom", str(ctx.exception))

    def test_windows_raises_unsupported(self):
        runner = FakeRunner(paths={"xdotool": "/usr/bin/xdotool"})
        with patch("notmyfault.platform.backends.sys.platform", "win32"):
            with self.assertRaises(BackendUnsupportedError):
                InputBackend(runner).type_text("hi")

    def test_move_mouse_absolute_and_relative_xdotool(self):
        runner = FakeRunner(paths={"xdotool": "/usr/bin/xdotool"})
        backend = InputBackend(runner)
        backend.move_mouse(100, -20, True)
        backend.move_mouse(-5, 10, False)
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xdotool", "mousemove", "--", "100", "-20"],
        )
        self.assertEqual(
            runner.calls[1]["args"],
            ["/usr/bin/xdotool", "mousemove_relative", "--", "-5", "10"],
        )

    def test_move_mouse_absolute_and_relative_ydotool(self):
        runner = FakeRunner(paths={"ydotool": "/usr/bin/ydotool"})
        with patch("notmyfault.platform.linux_support.session_type", return_value="wayland"):
            backend = InputBackend(runner)
            backend.move_mouse(100, 200, True)
            backend.move_mouse(-5, 10, False)
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/ydotool", "mousemove", "--absolute", "-x", "100", "-y", "200"],
        )
        self.assertEqual(
            runner.calls[1]["args"],
            ["/usr/bin/ydotool", "mousemove", "-x", "-5", "-y", "10"],
        )

    def test_click_and_button_xdotool(self):
        runner = FakeRunner(paths={"xdotool": "/usr/bin/xdotool"})
        backend = InputBackend(runner)
        backend.click_mouse("right", 2)
        backend.mouse_button("middle", True)
        backend.mouse_button("middle", False)
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xdotool", "click", "--repeat", "2", "3"],
        )
        self.assertEqual(runner.calls[1]["args"], ["/usr/bin/xdotool", "mousedown", "2"])
        self.assertEqual(runner.calls[2]["args"], ["/usr/bin/xdotool", "mouseup", "2"])

    def test_click_and_button_ydotool(self):
        runner = FakeRunner(paths={"ydotool": "/usr/bin/ydotool"})
        with patch("notmyfault.platform.linux_support.session_type", return_value="wayland"):
            backend = InputBackend(runner)
            backend.click_mouse("left", 1)
            backend.mouse_button("right", True)
            backend.mouse_button("right", False)
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/ydotool", "click", "--repeat", "1", "0xC0"],
        )
        self.assertEqual(runner.calls[1]["args"], ["/usr/bin/ydotool", "click", "0x41"])
        self.assertEqual(runner.calls[2]["args"], ["/usr/bin/ydotool", "click", "0x81"])

    def test_mouse_rejects_invalid_button_and_coords(self):
        runner = FakeRunner(paths={"xdotool": "/usr/bin/xdotool"})
        backend = InputBackend(runner)
        with self.assertRaises(ValueError):
            backend.click_mouse("side")
        with self.assertRaises(ValueError):
            backend.move_mouse(1.5, 2)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            backend.click_mouse("left", 0)

    def test_get_mouse_position_xdotool_parses_shell_output(self):
        runner = FakeRunner(
            paths={"xdotool": "/usr/bin/xdotool"},
            results=[ok(stdout="X=12\nY=34\nSCREEN=0\nWINDOW=4194305\n")],
        )
        self.assertEqual(InputBackend(runner).get_mouse_position(), (12, 34))
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xdotool", "getmouselocation", "--shell"],
        )

    def test_get_mouse_position_ydotool_raises(self):
        runner = FakeRunner(paths={"ydotool": "/usr/bin/ydotool"})
        with patch("notmyfault.platform.linux_support.session_type", return_value="wayland"):
            with self.assertRaises(BackendFailedError) as ctx:
                InputBackend(runner).get_mouse_position()
        self.assertIn("当前输入后端无法读取鼠标位置", str(ctx.exception))
        self.assertIn("xdotool", str(ctx.exception))
        self.assertEqual(runner.calls, [])


class AudioBackendTests(LinuxPathTest):
    def test_set_volume_and_mute_use_wpctl(self):
        runner = FakeRunner(paths={"wpctl": "/usr/bin/wpctl"})
        backend = AudioBackend(runner)
        backend.set_volume(35)
        backend.set_mute(True)
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "35%"],
        )
        self.assertEqual(
            runner.calls[1]["args"],
            ["/usr/bin/wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
        )

    def test_missing_wpctl_raises_backend_missing(self):
        with self.assertRaises(BackendMissingError):
            AudioBackend(FakeRunner()).set_volume(50)

    def test_default_devices_from_wpctl_status(self):
        status = (
            "Audio\n"
            "Sinks:\n"
            "  12. Family 17h HD Audio Controller Analog Stereo [Out]\n"
            "  * 41. Analog Stereo [Out]\n"
            "Sources:\n"
            "  13. Family 17h HD Audio Controller Digital Stereo [In]\n"
            "  * 55. Digital Stereo [In]\n"
        )
        runner = FakeRunner(
            paths={"wpctl": "/usr/bin/wpctl"},
            results=[ok(stdout=status)],
        )
        self.assertEqual(
            AudioBackend(runner).default_devices(),
            {"render": "41", "capture": "55"},
        )

    def test_wpctl_devices_long_sink_list_still_finds_capture(self):
        # 设备很多时 Sources 段也认得出
        filler = "  12. Some Audio Device [Out]\n" * 30
        status = (
            "Audio\nSinks:\n"
            f"{filler}"
            "  * 41. Analog Stereo [Out]\n"
            "Sources:\n"
            "  * 55. Digital Stereo [In]\n"
        )
        runner = FakeRunner(
            paths={"wpctl": "/usr/bin/wpctl"},
            results=[ok(stdout=status)],
        )
        self.assertEqual(
            AudioBackend(runner).default_devices(),
            {"render": "41", "capture": "55"},
        )

    def test_default_devices_from_pactl(self):
        runner = FakeRunner(
            paths={"pactl": "/usr/bin/pactl"},
            results=[ok(stdout="sink-a"), ok(stdout="source-b")],
        )
        self.assertEqual(
            AudioBackend(runner).default_devices(),
            {"render": "sink-a", "capture": "source-b"},
        )

    def test_default_devices_empty_without_backends(self):
        self.assertEqual(AudioBackend(FakeRunner()).default_devices(), {})

    def test_default_devices_survives_query_failure(self):
        runner = FakeRunner(
            paths={"wpctl": "/usr/bin/wpctl"},
            results=[BackendFailedError("命令超时")],
        )

        def explode(args, timeout=10, input=None, env=None):
            raise runner.results.pop(0)

        runner.run = explode
        self.assertEqual(AudioBackend(runner).default_devices(), {})

    def test_set_default_device_uses_wpctl(self):
        runner = FakeRunner(paths={"wpctl": "/usr/bin/wpctl", "pactl": "/usr/bin/pactl"})
        AudioBackend(runner).set_default_device("render", "41")
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/wpctl", "set-default", "41"],
        )

    def test_set_default_device_falls_back_to_pactl(self):
        runner = FakeRunner(paths={"pactl": "/usr/bin/pactl"})
        AudioBackend(runner).set_default_device("capture", "mic-1")
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/pactl", "set-default-source", "mic-1"],
        )
        runner = FakeRunner(paths={"pactl": "/usr/bin/pactl"})
        AudioBackend(runner).set_default_device("render", "sink-a")
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/pactl", "set-default-sink", "sink-a"],
        )

    def test_set_default_device_missing_tools_raises(self):
        with self.assertRaises(BackendMissingError):
            AudioBackend(FakeRunner()).set_default_device("render", "41")

    def test_set_default_device_rejects_bad_flow(self):
        with self.assertRaises(ValueError):
            AudioBackend(FakeRunner(paths={"wpctl": "/usr/bin/wpctl"})).set_default_device(
                "output", "41"
            )


class ClipboardBackendTests(LinuxPathTest):
    def _wayland(self):
        return patch("notmyfault.platform.linux_support.session_type", return_value="wayland")

    def _x11(self):
        return patch("notmyfault.platform.linux_support.session_type", return_value="x11")

    def test_read_prefers_wayland_paste_on_wayland(self):
        runner = FakeRunner(
            paths={"wl-paste": "/usr/bin/wl-paste", "xclip": "/usr/bin/xclip"},
            results=[ok(stdout="hello")],
        )
        with self._wayland():
            self.assertEqual(ClipboardBackend(runner).read_text(), "hello")
        self.assertEqual(
            runner.calls[0]["args"], ["/usr/bin/wl-paste", "--no-newline"]
        )

    def test_x11_ignores_wl_clipboard_even_if_installed(self):
        # X11 会话装了 wl-clipboard 也不能走 wl-paste，它在 X11 下会失败
        runner = FakeRunner(
            paths={"wl-paste": "/usr/bin/wl-paste", "xclip": "/usr/bin/xclip"},
            results=[ok(stdout="text")],
        )
        with self._x11():
            self.assertEqual(ClipboardBackend(runner).read_text(), "text")
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xclip", "-selection", "clipboard", "-o"],
        )

    def test_read_falls_back_to_xclip(self):
        runner = FakeRunner(
            paths={"xclip": "/usr/bin/xclip"}, results=[ok(stdout="text")]
        )
        with self._x11():
            self.assertEqual(ClipboardBackend(runner).read_text(), "text")
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xclip", "-selection", "clipboard", "-o"],
        )

    def test_read_returns_none_on_nonzero_exit(self):
        runner = FakeRunner(
            paths={"wl-paste": "/usr/bin/wl-paste"},
            results=[ok(returncode=1)],
        )
        with self._wayland():
            self.assertIsNone(ClipboardBackend(runner).read_text())

    def test_write_sends_text_on_stdin(self):
        runner = FakeRunner(paths={"wl-copy": "/usr/bin/wl-copy"})
        with self._wayland():
            ClipboardBackend(runner).write_text("内容")
        self.assertEqual(runner.calls[0]["input"], "内容")
        self.assertEqual(runner.calls[0]["args"], ["/usr/bin/wl-copy"])

    def test_write_missing_backend_raises(self):
        with self.assertRaises(BackendMissingError):
            ClipboardBackend(FakeRunner()).write_text("x")

    def test_write_failure_raises_backend_failed(self):
        runner = FakeRunner(
            paths={"xsel": "/usr/bin/xsel"},
            results=[ok(stderr="no display", returncode=1)],
        )
        with self._x11(), self.assertRaises(BackendFailedError):
            ClipboardBackend(runner).write_text("x")


class WindowBackendTests(LinuxPathTest):
    def setUp(self):
        super().setUp()
        patcher = patch(
            "notmyfault.platform.linux_support.session_type",
            return_value="x11",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _window_line(
        self,
        wid="0x2",
        pid=100,
        x=10,
        y=20,
        w=800,
        h=600,
        wm_class="app.App",
        host="host",
        title="记事本",
    ):
        return f"{wid} 0 {pid} {x} {y} {w} {h} {wm_class} {host} {title}"

    def test_title_match_and_pin(self):
        listing = (
            self._window_line(wid="0x1", title="旧窗口")
            + "\n"
            + self._window_line(wid="0x2", title="记事本")
            + "\n"
        )
        runner = FakeRunner(
            paths={"wmctrl": "/usr/bin/wmctrl", "xprop": "/usr/bin/xprop"},
            results=[
                ok(stdout=listing),
                ok(),
                ok(stdout="_NET_WM_STATE_ABOVE"),
            ],
        )

        result = WindowBackend(runner).set_pinned("pin", "title", "记事本")

        self.assertEqual(result, {"state": "pinned", "window_id": "0x2"})
        self.assertEqual(runner.calls[0]["args"], ["/usr/bin/wmctrl", "-lGpx"])
        self.assertEqual(
            runner.calls[1]["args"],
            ["/usr/bin/wmctrl", "-i", "-r", "0x2", "-b", "add,above"],
        )

    def test_toggle_reads_xprop_state(self):
        runner = FakeRunner(
            paths={"wmctrl": "/usr/bin/wmctrl", "xprop": "/usr/bin/xprop"},
            results=[
                ok(stdout="_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2"),
                ok(stdout="_NET_WM_STATE(ATOM) = _NET_WM_STATE_ABOVE"),
                ok(),
                ok(stdout="_NET_WM_STATE(ATOM) = "),
            ],
        )

        result = WindowBackend(runner).set_pinned("toggle", "active")

        self.assertEqual(result["state"], "unpinned")
        self.assertEqual(
            runner.calls[-2]["args"],
            ["/usr/bin/wmctrl", "-i", "-r", "0x2", "-b", "remove,above"],
        )

    def test_list_and_info_use_wmctrl_geometry(self):
        listing = self._window_line() + "\n"
        runner = FakeRunner(
            paths={"wmctrl": "/usr/bin/wmctrl", "xprop": "/usr/bin/xprop"},
            results=[
                ok(stdout=listing),
                ok(stdout=listing),
                ok(stdout="_NET_WM_STATE(ATOM) = _NET_WM_STATE_MAXIMIZED_VERT, _NET_WM_STATE_MAXIMIZED_HORZ"),
            ],
        )
        with patch("psutil.Process") as process_cls:
            process_cls.return_value.name.return_value = "app"
            backend = WindowBackend(runner)
            handles = backend.handles()
            info = backend.info(0x2)

        self.assertEqual(handles, [0x2])
        self.assertEqual(info["title"], "记事本")
        self.assertEqual(info["class_name"], "App")
        self.assertEqual(info["process_name"], "app")
        self.assertTrue(info["maximized"])
        self.assertIsNone(info["opacity"])

    def test_activate_move_close_commands(self):
        runner = FakeRunner(paths={"wmctrl": "/usr/bin/wmctrl", "xprop": "/usr/bin/xprop"})
        backend = WindowBackend(runner)
        backend.activate(0x2)
        backend.position(0x2, {"x": 1, "y": 2, "width": 300, "height": 400})
        backend.close(0x2)
        self.assertEqual(
            [call["args"] for call in runner.calls],
            [
                ["/usr/bin/wmctrl", "-i", "-a", "0x2"],
                ["/usr/bin/wmctrl", "-i", "-r", "0x2", "-e", "0,1,2,300,400"],
                ["/usr/bin/wmctrl", "-i", "-c", "0x2"],
            ],
        )

    def test_show_minimize_prefers_xdotool(self):
        runner = FakeRunner(
            paths={
                "wmctrl": "/usr/bin/wmctrl",
                "xprop": "/usr/bin/xprop",
                "xdotool": "/usr/bin/xdotool",
            }
        )
        WindowBackend(runner).show(0x2, 6)
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xdotool", "windowminimize", "0x2"],
        )

    def test_monitors_from_xrandr(self):
        output = (
            "Monitors: 2\n"
            " 0: +*HDMI-1 1920/527x1080/296+0+0  HDMI-1\n"
            " 1: +DP-1 1440/309x900/174+-1440+0  DP-1\n"
        )
        runner = FakeRunner(
            paths={"xrandr": "/usr/bin/xrandr", "wmctrl": "/usr/bin/wmctrl", "xprop": "/usr/bin/xprop"},
            results=[ok(stdout=output)],
        )
        monitors = WindowBackend(runner).monitors()
        self.assertEqual(len(monitors), 2)
        self.assertTrue(monitors[0]["primary"])
        self.assertEqual(monitors[0]["bounds"]["width"], 1920)
        self.assertEqual(monitors[1]["bounds"]["x"], -1440)

    def test_list_titles_falls_back_to_xdotool(self):
        runner = FakeRunner(
            paths={"xdotool": "/usr/bin/xdotool"},
            results=[ok(stdout="A\n\nB\n")],
        )
        titles = WindowBackend(runner).list_titles()
        self.assertEqual(titles, {0: "A", 2: "B"})
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xdotool", "search", "--name", "", "getwindowname", "%@"],
        )

    def test_wayland_rejects_window_ops(self):
        runner = FakeRunner(paths={"wmctrl": "/usr/bin/wmctrl", "xprop": "/usr/bin/xprop"})
        with patch(
            "notmyfault.platform.linux_support.session_type",
            return_value="wayland",
        ):
            with self.assertRaises(BackendUnsupportedError):
                WindowBackend(runner).handles()

    def test_info_for_unmapped_window_is_hidden(self):
        runner = FakeRunner(
            paths={"wmctrl": "/usr/bin/wmctrl", "xprop": "/usr/bin/xprop"},
            results=[
                ok(stdout=""),
                ok(stdout='WM_CLASS(STRING) = "a", "b"'),
            ],
        )
        info = WindowBackend(runner).info(0x2)
        self.assertEqual(info["hwnd"], 0x2)
        self.assertFalse(info["visible"])

    def test_opacity_unsupported(self):
        with self.assertRaises(BackendUnsupportedError):
            WindowBackend(FakeRunner()).opacity(1, 50)

    def test_missing_wmctrl_raises(self):
        with self.assertRaises(BackendMissingError):
            WindowBackend(FakeRunner()).set_pinned("pin", "title", "记事本")

    def test_window_command_failure_raises(self):
        runner = FakeRunner(
            paths={"wmctrl": "/usr/bin/wmctrl", "xprop": "/usr/bin/xprop"},
            results=[ok(stderr="no display", returncode=1)],
        )
        with self.assertRaisesRegex(BackendFailedError, "no display"):
            WindowBackend(runner).set_pinned("pin", "active")


class DisplayBackendTests(LinuxPathTest):
    def test_brightness_uses_brightnessctl(self):
        runner = FakeRunner(paths={"brightnessctl": "/usr/bin/brightnessctl"})

        DisplayBackend(runner).set_brightness(40)

        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/brightnessctl", "set", "40%"],
        )

    def test_gnome_power_uses_gdbus(self):
        runner = FakeRunner(paths={"gdbus": "/usr/bin/gdbus"})
        with patch(
            "notmyfault.platform.linux_support.desktop_environment",
            return_value="gnome",
        ):
            DisplayBackend(runner).set_power("off")

        self.assertEqual(runner.calls[0]["args"][-1], "true")
        self.assertIn("org.gnome.ScreenSaver.SetActive", runner.calls[0]["args"])

    def test_x11_power_uses_xset(self):
        runner = FakeRunner(paths={"xset": "/usr/bin/xset"})
        with patch(
            "notmyfault.platform.linux_support.desktop_environment",
            return_value="unknown",
        ), patch(
            "notmyfault.platform.linux_support.session_type",
            return_value="x11",
        ):
            DisplayBackend(runner).set_power("on")

        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xset", "dpms", "force", "on"],
        )

    def test_kde_power_uses_screensaver(self):
        runner = FakeRunner(paths={"gdbus": "/usr/bin/gdbus"})
        with patch(
            "notmyfault.platform.linux_support.desktop_environment",
            return_value="kde",
        ), patch(
            "notmyfault.platform.linux_support.session_type",
            return_value="wayland",
        ):
            DisplayBackend(runner).set_power("off")
        self.assertIn("org.freedesktop.ScreenSaver.SetActive", runner.calls[0]["args"])
        self.assertEqual(runner.calls[0]["args"][4], "org.kde.screensaver")
        self.assertEqual(runner.calls[0]["args"][-1], "true")

    def test_wayland_unknown_desktop_power_rejected(self):
        runner = FakeRunner(paths={"xset": "/usr/bin/xset", "gdbus": "/usr/bin/gdbus"})
        with patch(
            "notmyfault.platform.linux_support.desktop_environment",
            return_value="unknown",
        ), patch(
            "notmyfault.platform.linux_support.session_type",
            return_value="wayland",
        ):
            with self.assertRaises(BackendUnsupportedError) as ctx:
                DisplayBackend(runner).set_power("off")
        self.assertIn("Wayland", str(ctx.exception))

    def test_missing_brightnessctl_raises(self):
        with self.assertRaises(BackendMissingError):
            DisplayBackend(FakeRunner()).set_brightness(40)


class ScreenshotBackendTests(LinuxPathTest):
    def test_portal_subscribes_before_request_and_closes_pending_request(self):
        for responds in (True, False):
            class Bus:
                unique_name = ":1.42"
                def __init__(self):
                    self.calls = []
                    self.disconnected = False
                async def connect(self):
                    return self
                def add_message_handler(self, handler):
                    self.handler = handler
                def disconnect(self):
                    self.disconnected = True
                async def call(self, message):
                    self.calls.append(message.member)
                    if message.member == "Screenshot":
                        self.request_path = "/org/freedesktop/portal/desktop/request/1_42/" + message.body[1]["handle_token"].value
                        if responds:
                            self.handler(types.SimpleNamespace(
                                message_type=4, path=self.request_path,
                                interface="org.freedesktop.portal.Request", member="Response",
                                body=[0, {"uri": types.SimpleNamespace(value="file:///tmp/capture.png")}],
                            ))
                        return types.SimpleNamespace(message_type=2, body=[self.request_path])
                    return types.SimpleNamespace(message_type=2, body=[])
            bus = Bus()
            source = Path(__file__).resolve().parents[1] / "platform" / "portal_screenshot.py"
            spec = importlib.util.spec_from_file_location("portal_under_test", source)
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {
                "dbus_next": types.SimpleNamespace(Message=lambda **kwargs: types.SimpleNamespace(**kwargs),
                    MessageType=types.SimpleNamespace(ERROR=3, SIGNAL=4),
                    Variant=lambda signature, value: types.SimpleNamespace(value=value)),
                "dbus_next.aio": types.SimpleNamespace(MessageBus=lambda: bus),
            }):
                spec.loader.exec_module(module)
            with patch.object(module.shutil, "copyfile"):
                if responds:
                    asyncio.run(module._take_screenshot("unused.png", False, timeout=0.05))
                else:
                    with self.assertRaises(TimeoutError):
                        asyncio.run(module._take_screenshot("unused.png", False, timeout=0.005))
            self.assertEqual(bus.calls, ["AddMatch", "Screenshot"] + ([] if responds else ["Close"]))
            self.assertTrue(bus.disconnected)

    def test_active_window_uses_gnome_screenshot(self):
        runner = FakeRunner(paths={"gnome-screenshot": "/usr/bin/gnome-screenshot"})
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "notmyfault.platform.linux_support.session_type",
            return_value="x11",
        ):
            destination = str(Path(temp_dir) / "shot.png")
            result = ScreenshotBackend(runner).capture(
                destination,
                "active_window",
                "png",
            )

        self.assertEqual(result, destination)
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/gnome-screenshot", "-w", "-f", destination],
        )
        self.assertEqual(runner.calls[0]["timeout"], 30)

    def test_fullscreen_falls_back_to_spectacle(self):
        runner = FakeRunner(paths={"spectacle": "/usr/bin/spectacle"})
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "notmyfault.platform.linux_support.session_type",
            return_value="x11",
        ):
            destination = str(Path(temp_dir) / "shot.png")
            ScreenshotBackend(runner).capture(destination, "fullscreen", "png")

        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/spectacle", "-b", "-n", "-f", "-o", destination],
        )

    def test_wayland_uses_portal(self):
        calls = []
        portal = types.ModuleType("notmyfault.platform.portal_screenshot")
        portal.take_screenshot = (
            lambda path, interactive: calls.append((path, interactive))
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "notmyfault.platform.linux_support.session_type",
            return_value="wayland",
        ), patch.dict(
            sys.modules,
            {"notmyfault.platform.portal_screenshot": portal},
        ):
            destination = str(Path(temp_dir) / "shot.png")
            result = ScreenshotBackend(FakeRunner()).capture(
                destination,
                "active_window",
                "png",
            )

        self.assertEqual(result, destination)
        self.assertEqual(calls, [(destination, True)])

    def test_missing_screenshot_command_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "notmyfault.platform.linux_support.session_type",
            return_value="x11",
        ):
            with self.assertRaises(BackendMissingError):
                ScreenshotBackend(FakeRunner()).capture(
                    str(Path(temp_dir) / "shot.png"),
                    "fullscreen",
                    "png",
                )


class PowerProfileBackendTests(LinuxPathTest):
    def test_set_profile_maps_plans(self):
        runner = FakeRunner(paths={"powerprofilesctl": "/usr/bin/powerprofilesctl"})
        profile = PowerProfileBackend(runner).set_profile("high_performance")
        self.assertEqual(profile, "performance")
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/powerprofilesctl", "set", "performance"],
        )

    def test_missing_powerprofilesctl_raises(self):
        with self.assertRaises(BackendMissingError):
            PowerProfileBackend(FakeRunner()).set_profile("balanced")

    def test_custom_plan_rejected(self):
        runner = FakeRunner(paths={"powerprofilesctl": "/usr/bin/powerprofilesctl"})
        with self.assertRaises(ValueError):
            PowerProfileBackend(runner).set_profile("custom")


class LinuxSupportHelpersTests(unittest.TestCase):
    def test_current_session_id_prefers_env(self):
        from notmyfault.platform import linux_support

        with patch.dict("os.environ", {"XDG_SESSION_ID": "42"}, clear=False):
            self.assertEqual(linux_support.current_session_id(), "42")

    def test_current_session_id_falls_back_to_loginctl_display(self):
        from notmyfault.platform import linux_support

        with patch.dict("os.environ", {"XDG_SESSION_ID": "", "USER": "alice"}, clear=False), \
                patch("notmyfault.platform.linux_support.command_path", return_value="/bin/loginctl"), \
                patch(
                    "notmyfault.platform.linux_support.run_command",
                    return_value=ok(stdout="Display=c2\n"),
                ) as run:
            self.assertEqual(linux_support.current_session_id(), "c2")
        self.assertEqual(
            run.call_args.args[0],
            ["/bin/loginctl", "show-user", "alice", "-p", "Display"],
        )

    def test_idle_probe_reports_kde_backend(self):
        from notmyfault.platform import linux_support

        with patch("notmyfault.platform.linux_support.desktop_environment", return_value="kde"), \
                patch("notmyfault.platform.linux_support.command_path", side_effect=lambda *names: "/usr/bin/gdbus" if "gdbus" in names else None):
            backend, reason = linux_support.idle_probe()
        self.assertEqual(backend, "kde-ScreenSaver.GetSessionIdleTime")
        self.assertIsNone(reason)



class OcrBackendTests(unittest.TestCase):
    def test_missing_tesseract(self):
        with self.assertRaises(BackendMissingError) as caught:
            OcrBackend(FakeRunner()).recognize("screen.png", "eng")
        self.assertIn("tesseract", str(caught.exception))

    def test_rejects_language_that_is_not_a_tesseract_code(self):
        runner = FakeRunner(paths={"tesseract": "/usr/bin/tesseract"})
        with self.assertRaises(ValueError):
            OcrBackend(runner).recognize("screen.png", "eng;rm")
        self.assertEqual(runner.calls, [])

    def test_reads_utf8_stdout_and_reports_tesseract_error(self):
        runner = FakeRunner(
            paths={"tesseract": "/usr/bin/tesseract"},
            results=[ok("你好\n")],
        )
        text = OcrBackend(runner).recognize("/tmp/screen.png", "chi_sim+eng")
        self.assertEqual(text, "你好")
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/tesseract", "/tmp/screen.png", "stdout", "-l", "chi_sim+eng"],
        )
        self.assertEqual(runner.calls[0]["timeout"], 60)
        self.assertEqual(runner.calls[0]["encoding"], "utf-8")

        failed = FakeRunner(
            paths={"tesseract": "/usr/bin/tesseract"},
            results=[ok("", "Error opening data file chi_sim.traineddata", 1)],
        )
        with self.assertRaises(BackendFailedError) as caught:
            OcrBackend(failed).recognize("screen.png", "chi_sim")
        self.assertIn("traineddata", str(caught.exception))
