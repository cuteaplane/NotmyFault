import tempfile
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

    def run(self, args, timeout=10, input=None):
        self.calls.append({"args": args, "timeout": timeout, "input": input})
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
        with self.assertRaises(BackendFailedError) as ctx:
            runner.run(
                [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3
            )
        self.assertIn("超时", str(ctx.exception))

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

    def test_hotkey_maps_keys_for_xdotool(self):
        runner = FakeRunner(paths={"xdotool": "/usr/bin/xdotool"})
        InputBackend(runner).send_hotkey(["ctrl", "win", "enter"])
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xdotool", "key", "--clearmodifiers", "ctrl+super+Return"],
        )

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

        def explode(args, timeout=10, input=None):
            raise runner.results.pop(0)

        runner.run = explode
        self.assertEqual(AudioBackend(runner).default_devices(), {})


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
    def test_title_match_and_pin(self):
        runner = FakeRunner(
            paths={"wmctrl": "/usr/bin/wmctrl"},
            results=[ok(stdout="0x1 host 旧窗口\n0x2 host 记事本\n"), ok()],
        )

        result = WindowBackend(runner).set_pinned("pin", "title", "记事本")

        self.assertEqual(result, {"state": "pinned", "window_id": "0x2"})
        self.assertEqual(runner.calls[0]["args"], ["/usr/bin/wmctrl", "-l"])
        self.assertEqual(
            runner.calls[1]["args"],
            ["/usr/bin/wmctrl", "-i", "-r", "0x2", "-b", "add,above"],
        )

    def test_toggle_reads_xprop_state(self):
        runner = FakeRunner(
            paths={"wmctrl": "/usr/bin/wmctrl", "xprop": "/usr/bin/xprop"},
            results=[
                ok(stdout="0x2 host 记事本\n"),
                ok(stdout="_NET_WM_STATE(ATOM) = _NET_WM_STATE_ABOVE"),
                ok(),
            ],
        )

        result = WindowBackend(runner).set_pinned("toggle", "active")

        self.assertEqual(result["state"], "unpinned")
        self.assertEqual(
            runner.calls[-1]["args"],
            ["/usr/bin/wmctrl", "-i", "-r", "0x2", "-b", "remove,above"],
        )

    def test_missing_wmctrl_raises(self):
        with self.assertRaises(BackendMissingError):
            WindowBackend(FakeRunner()).set_pinned("pin", "active")

    def test_window_command_failure_raises(self):
        runner = FakeRunner(
            paths={"wmctrl": "/usr/bin/wmctrl"},
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
        ):
            DisplayBackend(runner).set_power("on")

        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xset", "dpms", "force", "on"],
        )

    def test_missing_brightnessctl_raises(self):
        with self.assertRaises(BackendMissingError):
            DisplayBackend(FakeRunner()).set_brightness(40)


class ScreenshotBackendTests(LinuxPathTest):
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
