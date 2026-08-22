"""能力模型：探测结构、manifest 校验和兼容判断"""

import unittest
from unittest.mock import patch

from notmyfault.platform import capabilities
from notmyfault.platform.linux_support import BackendMissingError, require_command
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
        with patch("notmyfault.platform.capabilities.os.name", "nt"), \
                patch("importlib.util.find_spec", return_value=object()):
            report = capabilities.probe_capabilities()
        for entry in report.values():
            self.assertTrue(entry["available"])
            self.assertIsNotNone(entry["backend"])

    def test_windows_missing_pycaw_reports_reason(self):
        with patch("notmyfault.platform.capabilities.os.name", "nt"), \
                patch("importlib.util.find_spec", return_value=None):
            report = capabilities.probe_capabilities()
        audio = report[capabilities.AUDIO_CONTROL]
        self.assertFalse(audio["available"])
        self.assertIn("pycaw", audio["reason"])

    def test_linux_missing_backend_reports_reason(self):
        with patch("notmyfault.platform.capabilities.os.name", "posix"), \
                patch("notmyfault.platform.linux_support.shutil.which", return_value=None):
            report = capabilities.probe_capabilities()
        brightness = report[capabilities.DISPLAY_BRIGHTNESS]
        self.assertFalse(brightness["available"])
        self.assertIn("brightnessctl", brightness["reason"])


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
            "enabled": True,
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.cap",
            "requires_capabilities": ["display.brightness"],
        }
        is_valid, errors = validate_plugin_meta(meta, "action")
        self.assertTrue(is_valid, errors)

    def test_schema_rejects_unknown_capability(self):
        meta = {
            "id": "cap_plugin",
            "name": "能力插件",
            "description": "测试",
            "enabled": True,
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.cap",
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
            "enabled": True,
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.cap",
            "requires_capabilities": "display.brightness",
        }
        is_valid, errors = validate_plugin_meta(meta, "action")
        self.assertFalse(is_valid)
        self.assertTrue(any("requires_capabilities" in e for e in errors))
class RequireCommandTests(unittest.TestCase):
    def test_missing_backend_raises_unified_error(self):
        from notmyfault.platform.linux_support import BackendMissingError, require_command

        with patch("notmyfault.platform.linux_support.shutil.which", return_value=None):
            with self.assertRaises(BackendMissingError) as ctx:
                require_command("模拟按键", "xdotool", "ydotool")
        self.assertIn("依赖缺失", str(ctx.exception))
        self.assertIn("xdotool", str(ctx.exception))
        self.assertIn("ydotool", str(ctx.exception))

    def test_found_backend_returns_path(self):
        from notmyfault.platform.linux_support import require_command

        def fake_which(name):
            return f"/usr/bin/{name}" if name == "ydotool" else None

        with patch("notmyfault.platform.linux_support.shutil.which", side_effect=fake_which):
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
