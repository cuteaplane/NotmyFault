"""Linux backend 门面：CommandRunner 结构化错误 + input / audio / clipboard 的命令拼装"""

import subprocess
import sys
import time
import types
import unittest
from unittest.mock import patch

from notmyfault.platform.backends import (
    AudioBackend,
    BackendFailedError,
    BackendMissingError,
    BackendPermissionDeniedError,
    BackendUnsupportedError,
    ClipboardBackend,
    CommandRunner,
    InputBackend,
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
    """跑在 Windows 上也假装 posix，专测 Linux 分支的命令拼装"""

    def setUp(self):
        patcher = patch("notmyfault.platform.backends.os.name", "posix")
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
        with patch("notmyfault.platform.backends.os.name", "nt"):
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
        with patch(
            "notmyfault.platform.linux_support.session_type", return_value="x11"
        ):
            self.assertEqual(ClipboardBackend(runner).read_text(), "text")
        self.assertEqual(
            runner.calls[0]["args"],
            ["/usr/bin/xclip", "-selection", "clipboard", "-o"],
        )

    def test_read_falls_back_to_xclip(self):
        runner = FakeRunner(
            paths={"xclip": "/usr/bin/xclip"}, results=[ok(stdout="text")]
        )
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
        with self.assertRaises(BackendFailedError):
            ClipboardBackend(runner).write_text("x")
