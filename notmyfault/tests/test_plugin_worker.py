"""isolated action worker：成功、异常、超时、崩溃、上下文削减和引擎回收"""

import json
import os
import threading
import time

import pytest

from notmyfault.core import plugin_worker
from notmyfault.core.plugin_worker import (
    PluginWorkerCrashed,
    PluginWorkerTimeout,
    run_isolated_action,
)


def write_action(tmp_path, name, source):
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return str(path)


SIMPLE_ACTION = """
def run(action_info, params):
    print("动作里的 print 进日志")
    return {"echo": params.get("value")}
"""

RAISING_ACTION = """
def run(action_info, params):
    raise ValueError("动作故意失败")
"""

SLEEPY_ACTION = """
import time

def run(action_info, params):
    time.sleep(30)
    return {}
"""

CRASHING_ACTION = """
import os

def run(action_info, params):
    os._exit(3)
"""

IMPORT_CRASH_ACTION = """
raise RuntimeError("模块 import 阶段就崩")
"""


class TestRunIsolatedAction:
    def test_success_returns_result(self, tmp_path):
        entry = write_action(tmp_path, "simple", SIMPLE_ACTION)
        ok, result = run_isolated_action(entry, {"id": "simple"}, {"value": 42}, {})
        assert ok is True
        assert result == {"echo": 42}

    def test_raising_action_reports_error(self, tmp_path):
        entry = write_action(tmp_path, "raising", RAISING_ACTION)
        ok, error = run_isolated_action(entry, {}, {}, {})
        assert ok is False
        assert "动作故意失败" in error

    def test_execute_timeout_kills_worker(self, tmp_path):
        entry = write_action(tmp_path, "sleepy", SLEEPY_ACTION)
        started = time.time()
        with pytest.raises(PluginWorkerTimeout):
            run_isolated_action(entry, {}, {}, {}, execute_timeout=1.0)
        assert time.time() - started < 10

    def test_crash_raises_and_emits_event(self, tmp_path):
        events = []
        plugin_worker.set_crash_callback(
            lambda kind, data: events.append((kind, data))
        )
        try:
            entry = write_action(tmp_path, "crashing", CRASHING_ACTION)
            with pytest.raises(PluginWorkerCrashed):
                run_isolated_action(entry, {}, {}, {})
        finally:
            plugin_worker.set_crash_callback(None)
        assert events and events[0][0] == "plugin_worker_crashed"
        assert events[0][1]["returncode"] == 3

    def test_import_crash_is_action_failure_not_worker_crash(self, tmp_path):
        # import 阶段的异常在 worker 里被抓住，按动作失败回报，引擎不当作崩溃
        entry = write_action(tmp_path, "importcrash", IMPORT_CRASH_ACTION)
        ok, error = run_isolated_action(entry, {}, {}, {})
        assert ok is False
        assert "模块 import 阶段就崩" in error

    def test_context_is_sanitized(self):
        context = {
            "run": {"id": "run_1"},
            "event": {"payload": {"x": 1}},
            "_run_cancel_event": threading.Event(),
            "steps": {"s1": {"status": "ok"}},
        }
        cleaned = plugin_worker._sanitize_context(context)
        assert cleaned["run"]["id"] == "run_1"
        assert "_run_cancel_event" not in cleaned
        assert isinstance(json.dumps(cleaned), str)

    def test_shutdown_all_terminates_live_worker(self, tmp_path):
        entry = write_action(tmp_path, "sleepy2", SLEEPY_ACTION)
        result = {}

        def run_until_shutdown():
            try:
                run_isolated_action(entry, {}, {}, {}, execute_timeout=30)
            except PluginWorkerCrashed as exc:
                result["error"] = exc

        thread = threading.Thread(
            target=run_until_shutdown,
            daemon=True,
        )
        thread.start()
        deadline = time.time() + 5
        while not plugin_worker._live_processes and time.time() < deadline:
            time.sleep(0.05)
        assert plugin_worker._live_processes
        plugin_worker.shutdown_all()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert isinstance(result.get("error"), PluginWorkerCrashed)


class TestLoaderIsolatedMode:
    def _make_plugin(self, tmp_path, execution_mode="isolated"):
        from types import SimpleNamespace

        from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
        from notmyfault.security.security import SecurityMode

        plugin_dir = tmp_path / "actions" / "iso_action"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "action.py").write_text(
            "def run(action_info, params):\n"
            "    return {'doubled': params.get('n', 0) * 2}\n",
            encoding="utf-8",
        )
        meta = {
            "id": "iso_action",
            "name": "隔离动作",
            "description": "测试 isolated 执行",
            "enabled": True,
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.iso",
            "permissions": [],
            "execution_mode": execution_mode,
        }
        (plugin_dir / "action.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )

        class SudoStub:
            def authorize_plugin(self, plugin_id, token, module=None):
                pass

            def deauthorize_plugin(self, plugin_id, token):
                pass

        registry = PluginRegistry()
        loader = PluginLoader(
            registry=registry,
            config={},
            diagnostics=SimpleNamespace(record_plugin_error=lambda *a: None),
            security_mode=SecurityMode.PERMISSIVE,
            sudo=SudoStub(),
            engine_token="token",
            integrity_errors=[],
        )
        return loader

    def test_isolated_action_runs_in_worker(self, tmp_path):
        loader = self._make_plugin(tmp_path)
        meta_store, func_store = {}, {}
        loaded, failed = loader.load(
            base_dir=str(tmp_path),
            plugins_dir="actions",
            json_filename="action.json",
            py_filename="action.py",
            module_prefix="notmyfault.action_iso_",
            meta_store=meta_store,
            func_store=func_store,
            store_name="Actioner",
            origin="user",
        )
        assert (loaded, failed) == (1, 0)
        assert "iso_action" not in func_store

        run = loader.materialize_pending_action("iso_action")
        assert run is not None
        # isolated 动作不 import 进引擎进程
        assert loader._registry.get_module("iso_action") is None
        assert func_store["iso_action"]({}, {"n": 21}) == {"doubled": 42}

    def test_schema_rejects_builtin_isolated(self):
        from notmyfault.security.plugin_schema import validate_plugin_meta

        meta = {
            "id": "iso_builtin",
            "name": "内置隔离",
            "description": "测试",
            "enabled": True,
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.isob",
            "origin": "builtin",
            "execution_mode": "isolated",
        }
        is_valid, errors = validate_plugin_meta(meta, "action")
        assert not is_valid
        assert any("内置插件不允许" in e for e in errors)

    def test_schema_rejects_unknown_mode(self):
        from notmyfault.security.plugin_schema import validate_plugin_meta

        meta = {
            "id": "iso_bad",
            "name": "坏模式",
            "description": "测试",
            "enabled": True,
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.isoc",
            "execution_mode": "cloud",
        }
        is_valid, errors = validate_plugin_meta(meta, "action")
        assert not is_valid
        assert any("execution_mode" in e for e in errors)


class TestEnginesVersion:
    def test_matching_version_loads(self):
        from notmyfault.core.plugin_api import HOST_API_VERSION, engines_compatibility

        ok, reason = engines_compatibility(
            {"engines": {"notmyfault_api": HOST_API_VERSION}}
        )
        assert ok and reason == ""

    def test_mismatched_version_reports_reason(self):
        from notmyfault.core.plugin_api import engines_compatibility

        ok, reason = engines_compatibility({"engines": {"notmyfault_api": 2}})
        assert not ok
        assert "宿主 API" in reason

    def test_no_declaration_is_fine(self):
        from notmyfault.core.plugin_api import engines_compatibility

        assert engines_compatibility({}) == (True, "")

    def test_schema_rejects_non_integer(self):
        from notmyfault.security.plugin_schema import validate_plugin_meta

        meta = {
            "id": "eng_bad",
            "name": "坏版本",
            "description": "测试",
            "enabled": True,
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.eng",
            "engines": {"notmyfault_api": "1.0"},
        }
        is_valid, errors = validate_plugin_meta(meta, "action")
        assert not is_valid
        assert any("notmyfault_api" in e for e in errors)
