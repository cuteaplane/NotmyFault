"""能力模型：探测结构、manifest 校验和兼容判断"""

import ctypes
import unittest
from unittest.mock import MagicMock, patch

from notmyfault.platform import capabilities
from notmyfault.platform.linux import probes as linux_probes

# 测试会把平台改成 windows，ctypes 得先在 posix 下加载，不然会找 FormatError
from notmyfault.platform._run import require_command
from notmyfault.platform.errors import BackendMissingError
from notmyfault.security.plugin_schema import validate_plugin_meta


class ProbeStructureTests(unittest.TestCase):
    def test_all_capabilities_have_four_fields(self):
        report = capabilities.probe_capabilities()
        self.assertEqual(
            set(report), set(capabilities.CAPABILITY_IDS)
        )
        for entry in report.values():
            self.assertEqual(
                set(entry), {"available", "backend", "reason", "degraded"}
            )
            self.assertIsInstance(entry["available"], bool)

    def test_windows_probe_reports_builtin_backends(self):
        with patch("notmyfault.platform.current", return_value="windows"), \
                patch("importlib.util.find_spec", return_value=object()), \
                patch("ctypes.WinDLL", create=True), \
                patch("shutil.which", return_value="powershell.exe"), \
                patch("os.path.isfile", return_value=True):
            report = capabilities.probe_capabilities()
        for entry in report.values():
            self.assertTrue(entry["available"])
            self.assertIsNotNone(entry["backend"])
        for capability in (
            capabilities.DISPLAY_BRIGHTNESS,
            capabilities.AUDIO_CONTROL,
            capabilities.AUDIO_DEVICE_QUERY,
            capabilities.BLUETOOTH_CONTROL,
            capabilities.TTS,
        ):
            self.assertTrue(report[capability]["reason"])

    def test_windows_missing_dependencies_reports_reason(self):
        with patch("notmyfault.platform.current", return_value="windows"), \
                patch("importlib.util.find_spec", return_value=None), \
                patch("ctypes.WinDLL", side_effect=OSError("DLL unavailable"), create=True), \
                patch("shutil.which", return_value=None), \
                patch("os.path.isfile", return_value=False):
            report = capabilities.probe_capabilities()
        for entry in report.values():
            self.assertFalse(entry["available"])
            self.assertTrue(entry["reason"])
        self.assertIn("pycaw", report[capabilities.AUDIO_CONTROL]["reason"])
        with patch("notmyfault.platform.current", return_value="windows"), \
                patch("ctypes.WinDLL", create=True) as dll:
            del dll.return_value.SendInput
            self.assertFalse(capabilities.probe_capability(capabilities.INPUT_SEND)["available"])
        with patch("notmyfault.platform.current", return_value="windows"), \
                patch("importlib.util.find_spec", return_value=object()), \
                patch("ctypes.WinDLL", side_effect=OSError("DDC unavailable"), create=True), \
                patch("shutil.which", return_value="powershell.exe"):
            self.assertTrue(capabilities.probe_capability(capabilities.AUDIO_CONTROL)["available"])
            self.assertFalse(capabilities.probe_capability(capabilities.INPUT_SEND)["available"])
            brightness = capabilities.probe_capability(capabilities.DISPLAY_BRIGHTNESS)
            self.assertTrue(brightness["available"])
            self.assertTrue(brightness["degraded"])
        with patch("notmyfault.platform.current", return_value="windows"), \
                patch("importlib.util.find_spec", return_value=None), \
                patch("ctypes.WinDLL", create=True):
            self.assertFalse(capabilities.probe_capability(capabilities.AUDIO_CONTROL)["available"])
            self.assertTrue(capabilities.probe_capability(capabilities.INPUT_SEND)["available"])

    def test_linux_missing_backend_reports_reason(self):
        with patch("notmyfault.platform.current", return_value="linux"), \
                patch("notmyfault.platform._run.shutil.which", return_value=None):
            report = capabilities.probe_capabilities()
        brightness = report[capabilities.DISPLAY_BRIGHTNESS]
        self.assertFalse(brightness["available"])
        self.assertIn("brightnessctl", brightness["reason"])

    def test_linux_tray_backend_failure_reports_unavailable(self):
        import builtins

        real_import = builtins.__import__

        def import_with_failed_tray(name, *args, **kwargs):
            if name == "pystray":
                raise RuntimeError('Bad display name ""')
            return real_import(name, *args, **kwargs)

        with patch("notmyfault.platform.current", return_value="linux"), patch(
            "notmyfault.platform.linux.desktop.session_type", return_value="x11"
        ), patch("builtins.__import__", side_effect=import_with_failed_tray):
            result = capabilities.probe_capabilities()[capabilities.TRAY]
        self.assertFalse(result["available"])
        self.assertIsNone(result["backend"])
        self.assertIn('Bad display name ""', result["reason"])

    def test_macos_does_not_use_linux_probe(self):
        linux_probe = MagicMock()
        with patch("notmyfault.platform.current", return_value="macos"), \
                patch.dict(linux_probes.PROBES, {capabilities.CLIPBOARD_READ: linux_probe}):
            result = capabilities.probe_capability(capabilities.CLIPBOARD_READ)
        linux_probe.assert_not_called()
        self.assertFalse(result["available"])
        self.assertIn("暂未提供", result["reason"])

    def test_linux_clipboard_probe_matches_wayland_backend(self):
        def only_xclip(name):
            return "/usr/bin/xclip" if name == "xclip" else None

        with patch("notmyfault.platform.linux.desktop.session_type", return_value="wayland"), \
                patch("notmyfault.platform._run.shutil.which", side_effect=only_xclip):
            result = linux_probes.PROBES[capabilities.CLIPBOARD_READ]()
        self.assertFalse(result["available"])
        self.assertIn("wl-paste", result["reason"])

    def test_linux_clipboard_probe_matches_x11_backend(self):
        def only_wayland(name):
            return "/usr/bin/wl-paste" if name == "wl-paste" else None

        with patch("notmyfault.platform.linux.desktop.session_type", return_value="x11"), \
                patch("notmyfault.platform._run.shutil.which", side_effect=only_wayland):
            result = linux_probes.PROBES[capabilities.CLIPBOARD_READ]()
        self.assertFalse(result["available"])
        self.assertIn("xclip", result["reason"])


    def test_linux_display_power_and_media_and_idle_probes(self):
        def which(name):
            return f"/usr/bin/{name}" if name in {"gdbus", "playerctl", "xprintidle"} else None

        with patch("notmyfault.platform.linux.desktop.session_type", return_value="wayland"), \
                patch("notmyfault.platform.linux.desktop.desktop_environment", return_value="gnome"), \
                patch("notmyfault.platform._run.shutil.which", side_effect=which):
            power = linux_probes.PROBES[capabilities.DISPLAY_POWER]()
            media = linux_probes.PROBES[capabilities.MEDIA_CONTROL]()
            idle = linux_probes.PROBES[capabilities.IDLE_QUERY]()
        self.assertTrue(power["available"])
        self.assertEqual(power["backend"], "gnome-ScreenSaver")
        self.assertTrue(media["available"])
        self.assertTrue(idle["available"])
        self.assertIn("IdleMonitor", idle["backend"])

        with patch("notmyfault.platform.linux.desktop.session_type", return_value="wayland"), \
                patch("notmyfault.platform.linux.desktop.desktop_environment", return_value="unknown"), \
                patch("notmyfault.platform._run.shutil.which", side_effect=which):
            power = linux_probes.PROBES[capabilities.DISPLAY_POWER]()
        self.assertFalse(power["available"])
        self.assertIn("Wayland", power["reason"])

    def test_wayland_screen_capture_requires_portal_client(self):
        with patch("notmyfault.platform.linux.desktop.session_type", return_value="wayland"), \
                patch("importlib.util.find_spec", return_value=None), \
                patch("notmyfault.platform._run.shutil.which", return_value="/usr/bin/grim"):
            result = linux_probes.PROBES[capabilities.SCREEN_CAPTURE]()
        self.assertFalse(result["available"])
        self.assertIn("dbus-next", result["reason"])

    def test_screen_ocr_needs_tesseract_and_a_capture_backend(self):
        def which(name):
            if name in {"tesseract", "gnome-screenshot"}:
                return f"/usr/bin/{name}"
            return None

        with patch("notmyfault.platform.linux.desktop.session_type", return_value="x11"),                 patch("notmyfault.platform._run.shutil.which", side_effect=which):
            ready = linux_probes.PROBES[capabilities.SCREEN_OCR]()
        self.assertTrue(ready["available"])
        self.assertEqual(ready["backend"], "tesseract")
        self.assertFalse(ready["degraded"])

        def only_screenshot(name):
            return "/usr/bin/gnome-screenshot" if name == "gnome-screenshot" else None

        with patch("notmyfault.platform.linux.desktop.session_type", return_value="x11"),                 patch("notmyfault.platform._run.shutil.which", side_effect=only_screenshot):
            missing_ocr = linux_probes.PROBES[capabilities.SCREEN_OCR]()
        self.assertFalse(missing_ocr["available"])
        self.assertIn("tesseract", missing_ocr["reason"])

        def only_tesseract(name):
            return "/usr/bin/tesseract" if name == "tesseract" else None

        with patch("notmyfault.platform.linux.desktop.session_type", return_value="x11"),                 patch("notmyfault.platform._run.shutil.which", side_effect=only_tesseract):
            missing_capture = linux_probes.PROBES[capabilities.SCREEN_OCR]()
        self.assertFalse(missing_capture["available"])
        self.assertIn("gnome-screenshot", missing_capture["reason"])

    def test_wayland_screen_ocr_stays_degraded_with_portal(self):
        def which(name):
            return "/usr/bin/tesseract" if name == "tesseract" else None

        with patch("notmyfault.platform.linux.desktop.session_type", return_value="wayland"),                 patch("importlib.util.find_spec", return_value=object()),                 patch("notmyfault.platform._run.shutil.which", side_effect=which):
            result = linux_probes.PROBES[capabilities.SCREEN_OCR]()
        self.assertTrue(result["available"])
        self.assertTrue(result["degraded"])
        self.assertEqual(result["backend"], "tesseract")
        self.assertIn("Portal", result["reason"])

    def test_windows_screen_ocr_requires_tesseract_and_gdi(self):
        with patch("notmyfault.platform.current", return_value="windows"),                 patch("shutil.which", return_value=None):
            missing = capabilities.probe_capability(capabilities.SCREEN_OCR)
        self.assertFalse(missing["available"])
        self.assertIn("tesseract", missing["reason"])

        def which(name):
            return "tesseract.exe" if name == "tesseract" else "powershell.exe"

        with patch("notmyfault.platform.current", return_value="windows"),                 patch("importlib.util.find_spec", return_value=object()),                 patch("ctypes.WinDLL", create=True),                 patch("shutil.which", side_effect=which),                 patch("os.path.isfile", return_value=True):
            ready = capabilities.probe_capability(capabilities.SCREEN_OCR)
        self.assertTrue(ready["available"])
        self.assertEqual(ready["backend"], "tesseract")
        self.assertFalse(ready["degraded"])

        with patch("notmyfault.platform.current", return_value="windows"),                 patch("shutil.which", return_value="tesseract.exe"),                 patch("ctypes.WinDLL", side_effect=OSError("no gdi"), create=True):
            broken = capabilities.probe_capability(capabilities.SCREEN_OCR)
        self.assertFalse(broken["available"])
        self.assertIn("接口不可用", broken["reason"])


class CompatibilityTests(unittest.TestCase):
    def test_manifest_without_requirements_is_compatible(self):
        ok, problems = capabilities.is_capability_compatible({"id": "x"})
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_missing_capability_reports_problem(self):
        with patch("notmyfault.platform.capabilities.probe_capabilities") as probe:
            probe.return_value = {
                capabilities.DISPLAY_BRIGHTNESS: {
                    "available": False,
                    "backend": None,
                    "reason": "未安装 brightnessctl",
                    "degraded": False,
                }
            }
            ok, problems = capabilities.is_capability_compatible(
                {"requires_capabilities": ["display.brightness"]}
            )
        self.assertFalse(ok)
        self.assertEqual(problems[0]["capability"], "display.brightness")

    def test_unknown_capability_id_reports_problem(self):
        with patch("notmyfault.platform.capabilities.probe_capabilities") as probe:
            probe.return_value = {}
            ok, problems = capabilities.is_capability_compatible(
                {"requires_capabilities": ["not.a Capability"]}
            )
        self.assertFalse(ok)
        self.assertIn("未知能力 id", problems[0]["reason"])


class SchemaValidationTests(unittest.TestCase):
    def test_schema_accepts_known_capability(self):
        meta = {
            "id": "cap_plugin",
            "name": "能力插件",
            "description": "测试",
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.cap",
            "execution_api": "context-v1",
            "requires_capabilities": ["display.brightness"],
        }
        is_valid, errors = validate_plugin_meta(meta, "action")
        self.assertTrue(is_valid, errors)

    def test_schema_rejects_unknown_capability(self):
        meta = {
            "id": "cap_plugin",
            "name": "能力插件",
            "description": "测试",
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.cap",
            "execution_api": "context-v1",
            "requires_capabilities": ["teleport.wormhole"],
        }
        is_valid, errors = validate_plugin_meta(meta, "action")
        self.assertFalse(is_valid)
        self.assertTrue(any("未知能力 id" in e for e in errors))

    def test_schema_rejects_non_list(self):
        meta = {
            "id": "cap_plugin",
            "name": "能力插件",
            "description": "测试",
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.cap",
            "execution_api": "context-v1",
            "requires_capabilities": "display.brightness",
        }
        is_valid, errors = validate_plugin_meta(meta, "action")
        self.assertFalse(is_valid)
        self.assertTrue(any("requires_capabilities" in e for e in errors))
class RequireCommandTests(unittest.TestCase):
    def test_missing_backend_raises_unified_error(self):
        with patch("notmyfault.platform._run.shutil.which", return_value=None):
            with self.assertRaises(BackendMissingError) as ctx:
                require_command("模拟按键", "xdotool", "ydotool")
        self.assertIn("依赖缺失", str(ctx.exception))
        self.assertIn("xdotool", str(ctx.exception))
        self.assertIn("ydotool", str(ctx.exception))

    def test_found_backend_returns_path(self):
        def fake_which(name):
            return f"/usr/bin/{name}" if name == "ydotool" else None

        with patch("notmyfault.platform._run.shutil.which", side_effect=fake_which):
            self.assertEqual(
                require_command("模拟按键", "xdotool", "ydotool"), "/usr/bin/ydotool"
            )


    def test_degraded_capability_still_compatible(self):
        # degraded 不算缺失，loader 不跳过，界面显示“部分可用”
        with patch("notmyfault.platform.capabilities.probe_capabilities") as probe:
            probe.return_value = {
                capabilities.DISPLAY_BRIGHTNESS: {
                    "available": True,
                    "backend": "brightnessctl",
                    "reason": "DDC/CI 外接显示器不可控",
                    "degraded": True,
                }
            }
            ok, problems = capabilities.is_capability_compatible(
                {"requires_capabilities": ["display.brightness"]}
            )
        self.assertTrue(ok)
        self.assertEqual(problems, [])


class CatalogAvailabilityTests(unittest.TestCase):
    def test_required_and_optional_availability(self):
        from notmyfault.host.api.services.plugin_catalog import PluginCatalogService

        for required_available, degraded, optional_available, expected in [
            (True, False, True, "available"),
            (True, True, True, "partial"),
            (True, False, False, "partial"),
            (False, False, False, "unavailable"),
        ]:
            with self.subTest(
                required_available=required_available,
                degraded=degraded,
                optional_available=optional_available,
            ):
                meta = {
                    "requires_capabilities": ["media.control"],
                    "optional_capabilities": ["audio.control"],
                }
                report = {
                    "media.control": capabilities.entry(
                        required_available, "playerctl", "媒体服务受限", degraded,
                    ),
                    "audio.control": capabilities.entry(
                        optional_available, None, "未安装音量工具",
                    ),
                }
                PluginCatalogService._annotate_availability(meta, report)
                self.assertEqual(meta["availability"], expected)
                self.assertEqual(meta["capability_compatible"], required_available)
                reasons = "；".join(meta["unavailable_reasons"])
                if not optional_available:
                    self.assertIn("部分功能不可用", reasons)
                    self.assertIn("audio.control", reasons)
                    self.assertIn("未安装音量工具", reasons)
                if not required_available or degraded:
                    self.assertIn("media.control", reasons)
                    self.assertIn("媒体服务受限", reasons)

    def test_optional_only_plugin_is_partial_when_probe_is_missing(self):
        from notmyfault.host.api.services.plugin_catalog import PluginCatalogService

        meta = {"optional_capabilities": ["display.power"]}
        PluginCatalogService._annotate_availability(meta, {})
        self.assertEqual(meta["availability"], "partial")
        self.assertTrue(meta["capability_compatible"])
        self.assertIn("display.power", meta["unavailable_reasons"][0])
