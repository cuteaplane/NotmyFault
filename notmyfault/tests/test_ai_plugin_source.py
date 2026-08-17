"""AI 生成插件源码审查：只读、无副作用，失败抛稳定 code 的领域异常"""

import json
import sys

import pytest

from notmyfault.host import ai_plugin_source
from notmyfault.host.ai_plugin_source import (
    AIPluginSourceError,
    review_plugin_source,
)


def action_manifest(**overrides):
    manifest = {
        "id": "notify_world",
        "name": "通知世界",
        "description": "发一条桌面通知",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": "io.github.notmyfault.notify_world",
    }
    manifest.update(overrides)
    return manifest


def trigger_manifest(**overrides):
    manifest = {
        "id": "watch_folder",
        "name": "监控目录",
        "description": "目录内容变化时触发",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": "io.github.notmyfault.watch_folder",
        "trigger_api": "event-v2",
    }
    manifest.update(overrides)
    return manifest


ACTION_SOURCE = "def run(meta, params):\n    return {'ok': True}\n"
TRIGGER_SOURCE = (
    "def run(meta, config, emit_event, shutdown_event):\n"
    "    shutdown_event.wait()\n"
)


class TestKind:
    def test_rejects_unknown_kind(self):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("widget", action_manifest(), ACTION_SOURCE, "notify_world")
        assert exc.value.code == "invalid_kind"

    def test_rejects_non_str_kind(self):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source(1, action_manifest(), ACTION_SOURCE, "notify_world")
        assert exc.value.code == "invalid_kind"


class TestId:
    def test_rejects_id_mismatch(self):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source(
                "action", action_manifest(id="other_id"), ACTION_SOURCE, "notify_world"
            )
        assert exc.value.code == "id_mismatch"

    @pytest.mark.parametrize("bad_id", [
        "../evil", "a/b", "a\\b", "a b", "a:b", "a.b", ".hidden", "-lead",
    ])
    def test_rejects_path_traversal_id(self, bad_id):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source(
                "action", action_manifest(id=bad_id), ACTION_SOURCE, bad_id
            )
        assert exc.value.code == "invalid_id"

    def test_rejects_non_str_expected_id(self):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", action_manifest(), ACTION_SOURCE, 42)
        assert exc.value.code == "invalid_id"


class TestUnsupportedManifestFields:
    @pytest.mark.parametrize("field,value", [
        ("build", {"command": ["echo", "hi"]}),
        ("entrypoints", {"windows": "win/action.py"}),
        ("components", [{"id": "cmp", "name": "c", "entrypoint": "c.py"}]),
        ("contributes", {"commands": []}),
    ])
    def test_rejects_install_build_fields(self, field, value):
        manifest = action_manifest(**{field: value})
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", manifest, ACTION_SOURCE, "notify_world")
        assert exc.value.code == "unsupported_manifest"


class TestManifestSchema:
    def test_rejects_non_object_manifest(self):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", "not-a-dict", ACTION_SOURCE, "notify_world")
        assert exc.value.code == "invalid_manifest"

    @pytest.mark.parametrize("field", [
        "name", "description", "enabled", "version_code", "version", "package_name",
    ])
    def test_rejects_missing_required_field(self, field):
        manifest = action_manifest()
        del manifest[field]
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", manifest, ACTION_SOURCE, "notify_world")
        assert exc.value.code == "invalid_manifest"

    @pytest.mark.parametrize("overrides", [
        {"enabled": "yes"},
        {"version_code": 0},
        {"version_code": "one"},
        {"version": 3},
        {"package_name": "Not-A-Package"},
        {"package_name": "singleword"},
        {"permissions": ["teleport"]},
    ])
    def test_rejects_invalid_manifest_values(self, overrides):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source(
                "action", action_manifest(**overrides), ACTION_SOURCE, "notify_world"
            )
        assert exc.value.code == "invalid_manifest"

    def test_rejects_trigger_field_on_action(self):
        manifest = action_manifest(trigger_api="event-v2")
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", manifest, ACTION_SOURCE, "notify_world")
        assert exc.value.code == "invalid_manifest"


class TestSourceSyntax:
    def test_rejects_non_str_source(self):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", action_manifest(), b"def run(): pass", "notify_world")
        assert exc.value.code == "invalid_source"

    @pytest.mark.parametrize("source", ["def run(:\n", "if True print(1)\n", "\x00"])
    def test_rejects_invalid_syntax(self, source):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", action_manifest(), source, "notify_world")
        assert exc.value.code == "invalid_source"


class TestEntrypoints:
    def test_action_requires_run(self):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", action_manifest(), "x = 1\n", "notify_world")
        assert exc.value.code == "missing_entrypoint"

    def test_action_run_requires_two_params(self):
        source = "def run(meta):\n    return {}\n"
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", action_manifest(), source, "notify_world")
        assert exc.value.code == "missing_entrypoint"

    def test_context_v1_requires_run_with_context(self):
        manifest = action_manifest(execution_api="context-v1")
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", manifest, ACTION_SOURCE, "notify_world")
        assert exc.value.code == "missing_entrypoint"

    def test_context_v1_run_with_context_requires_three_params(self):
        manifest = action_manifest(execution_api="context-v1")
        source = (
            "def run(meta, params):\n    return {}\n"
            "def run_with_context(meta, params):\n    return {}\n"
        )
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", manifest, source, "notify_world")
        assert exc.value.code == "missing_entrypoint"

    def test_precondition_api_requires_check_precondition(self):
        manifest = action_manifest(precondition_api="context-v1")
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", manifest, ACTION_SOURCE, "notify_world")
        assert exc.value.code == "missing_entrypoint"

    def test_trigger_requires_run(self):
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("trigger", trigger_manifest(), "x = 1\n", "watch_folder")
        assert exc.value.code == "missing_entrypoint"

    def test_event_v2_trigger_requires_four_params(self):
        source = "def run(meta, config, emit_event):\n    pass\n"
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("trigger", trigger_manifest(), source, "watch_folder")
        assert exc.value.code == "missing_entrypoint"


class TestDangerousSource:
    def test_rejects_sudo_import(self):
        source = (
            "import notmyfault.security.sudo\n\n"
            "def run(meta, params):\n    return {}\n"
        )
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", action_manifest(), source, "notify_world")
        assert exc.value.code == "forbidden_import"

    def test_rejects_self_elevation(self):
        source = "def run(meta, params):\n    verb = 'runas'\n"
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", action_manifest(), source, "notify_world")
        assert exc.value.code == "forbidden_capability"

    @pytest.mark.parametrize("body", [
        "    return eval('1')",
        "    exec('x = 1')",
        "    return compile('x', '<s>', 'eval')",
        "    return __import__('os')",
        "    import importlib\n    return importlib.import_module('os')",
    ])
    def test_rejects_dynamic_exec(self, body):
        source = "def run(meta, params):\n" + body + "\n"
        with pytest.raises(AIPluginSourceError) as exc:
            review_plugin_source("action", action_manifest(), source, "notify_world")
        assert exc.value.code == "forbidden_capability"


class TestAcceptMinimal:
    def test_accepts_minimal_action(self):
        result = review_plugin_source(
            "action", action_manifest(), ACTION_SOURCE, "notify_world"
        )
        assert result["kind"] == "action"
        assert result["id"] == "notify_world"
        assert result["manifest"] == action_manifest()
        assert result["source"] == ACTION_SOURCE
        assert result["findings"] == {
            "capabilities": [],
            "uses_sudo": False,
            "borrowed": [],
        }

    def test_accepts_minimal_trigger(self):
        result = review_plugin_source(
            "trigger", trigger_manifest(), TRIGGER_SOURCE, "watch_folder"
        )
        assert result["kind"] == "trigger"
        assert result["id"] == "watch_folder"
        assert result["findings"]["capabilities"] == []
        assert result["findings"]["uses_sudo"] is False

    def test_accepts_context_v1_action(self):
        manifest = action_manifest(execution_api="context-v1")
        source = (
            "def run(meta, params):\n    return {}\n"
            "def run_with_context(meta, params, context):\n    return {}\n"
        )
        result = review_plugin_source("action", manifest, source, "notify_world")
        assert result["kind"] == "action"

    def test_accepts_precondition_action(self):
        manifest = action_manifest(precondition_api="context-v1")
        source = (
            "def run(meta, params):\n    return {}\n"
            "def check_precondition(meta, params, context):\n    return True\n"
        )
        result = review_plugin_source("action", manifest, source, "notify_world")
        assert result["kind"] == "action"

    def test_accepts_legacy_trigger_without_trigger_api(self):
        manifest = trigger_manifest()
        del manifest["trigger_api"]
        source = "def run(meta, config, emit_event):\n    pass\n"
        result = review_plugin_source("trigger", manifest, source, "watch_folder")
        assert result["kind"] == "trigger"


class TestReviewMetadata:
    def test_preserves_capabilities(self):
        source = "import subprocess\n\ndef run(meta, params):\n    return {}\n"
        result = review_plugin_source("action", action_manifest(), source, "notify_world")
        assert "external_binary" in result["findings"]["capabilities"]

    def test_preserves_borrowed_indicators(self):
        source = (
            "import notmyfault.action_clipboard_set\n\n"
            "def run(meta, params):\n    return {}\n"
        )
        result = review_plugin_source("action", action_manifest(), source, "notify_world")
        assert result["findings"]["borrowed"]

    def test_returns_fresh_json_safe_artifact(self):
        manifest = action_manifest(permissions=["notification"])
        result = review_plugin_source("action", manifest, ACTION_SOURCE, "notify_world")
        json.dumps(result)
        assert result["manifest"] is not manifest
        assert result["manifest"]["permissions"] is not manifest["permissions"]
        manifest["permissions"].append("network")
        assert result["manifest"]["permissions"] == ["notification"]


class TestNoSideEffects:
    def test_module_exposes_no_execution_api(self):
        forbidden = {
            "install", "uninstall", "sign", "load", "execute", "exec", "run",
            "write", "save", "build", "compile", "subprocess", "importlib",
            "os", "pathlib", "open", "eval", "persist", "tempfile", "sys",
        }
        exposed = set(vars(ai_plugin_source))
        assert not (forbidden & exposed)

    def test_review_touches_no_modules_paths_or_files(self, tmp_path):
        review_plugin_source("action", action_manifest(), ACTION_SOURCE, "notify_world")

        before_modules = set(sys.modules)
        before_path = list(sys.path)
        before_files = sorted(p.name for p in tmp_path.iterdir())

        review_plugin_source(
            "trigger", trigger_manifest(), TRIGGER_SOURCE, "watch_folder"
        )

        assert set(sys.modules) == before_modules
        assert list(sys.path) == before_path
        assert sorted(p.name for p in tmp_path.iterdir()) == before_files

    def test_reuses_schema_and_analyze(self, monkeypatch):
        calls = []
        real_validate = ai_plugin_source.validate_plugin_meta
        real_analyze = ai_plugin_source.analyze_plugin_source

        def spy_validate(meta, plugin_type):
            calls.append(("validate", plugin_type))
            return real_validate(meta, plugin_type)

        def spy_analyze(source, *, include_borrowed=True):
            calls.append(("analyze", include_borrowed))
            return real_analyze(source, include_borrowed=include_borrowed)

        monkeypatch.setattr(ai_plugin_source, "validate_plugin_meta", spy_validate)
        monkeypatch.setattr(ai_plugin_source, "analyze_plugin_source", spy_analyze)

        review_plugin_source("action", action_manifest(), ACTION_SOURCE, "notify_world")

        assert ("validate", "action") in calls
        assert ("analyze", True) in calls
