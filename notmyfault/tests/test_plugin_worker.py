import hashlib
import json
import io
import os
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from notmyfault.core import plugin_worker
from notmyfault.core.plugin_worker import (
    PluginWorkerCrashed,
    PluginWorkerStartupTimeout,
    PluginWorkerTimeout,
)


def write_action(tmp_path, name, source):
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return str(path)


def run_isolated_action(entry, action_info, params, context, **kwargs):
    digest = hashlib.sha256(Path(entry).read_bytes()).hexdigest()
    return plugin_worker.run_isolated_action(
        entry,
        digest,
        action_info,
        params,
        context,
        **kwargs,
    )


SIMPLE_ACTION = """
def run(action_info, params):
    print("动作里的 print 进日志")
    return {"echo": params.get("value")}
"""

CONTEXT_ACTION = """
def run(action_info, params):
    raise RuntimeError("context-v1 不应调用 run()")

def run_with_context(action_info, params, context):
    return {
        "run_id": context.get("run", {}).get("id"),
        "event_value": context.get("event", {}).get("payload", {}).get("value"),
    }
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

NOISY_ACTION = """
def run(action_info, params):
    print("x" * 262144)
    return {"completed": True}
"""

PLUGIN_API_ACTION = """
from notmyfault.plugin_api import platform_services

def run(action_info, params):
    return {"platform": platform_services().platform}
"""

READY_THEN_SLEEP_WORKER = """
import json
import sys
import time

print(json.dumps({"type": "ready", "protocol": 1}), flush=True)
sys.stdin.readline()
time.sleep(30)
"""

READY_RESULT_THEN_SLEEP_WORKER = """
import json
import sys
import time

print(json.dumps({"type": "ready", "protocol": 1}), flush=True)
sys.stdin.readline()
result = {"type": "result", "ok": True, "result": {"completed": True}}
print(json.dumps(result), flush=True)
time.sleep(30)
"""


class TestRunIsolatedAction:
    def test_runpy_cannot_bypass_strict_package_guard(self):
        code = (
            "import runpy\n"
            "runpy.run_module('notmyfault.security.security')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        assert result.returncode != 0
        assert "ImportError" in result.stderr
        assert "NMF_STRICT_IMPORT_DENIED" in result.stderr

    def test_success_returns_result(self, tmp_path):
        entry = write_action(tmp_path, "simple", SIMPLE_ACTION)
        ok, result = run_isolated_action(entry, {"id": "simple"}, {"value": 42}, {})
        assert ok is True
        assert result == {"echo": 42}

    def test_public_plugin_api_is_available_in_strict_worker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTMYFAULT_MODE", "strict")
        entry = write_action(tmp_path, "plugin_api", PLUGIN_API_ACTION)
        ok, result = run_isolated_action(entry, {"id": "plugin_api"}, {}, {})
        assert ok is True
        assert result["platform"] in {"windows", "linux", "macos"}

    def test_worker_imports_sibling_module(self, tmp_path):
        (tmp_path / "helper.py").write_text(
            "def triple(value):\n    return value * 3\n",
            encoding="utf-8",
        )
        entry = write_action(
            tmp_path,
            "with_helper",
            "from helper import triple\n\n"
            "def run(action_info, params):\n"
            "    return {'result': triple(params['value'])}\n",
        )
        ok, result = run_isolated_action(
            entry, {"id": "with_helper"}, {"value": 14}, {}
        )
        assert ok is True
        assert result == {"result": 42}

    def test_large_plugin_output_does_not_block_protocol(self, tmp_path):
        entry = write_action(tmp_path, "noisy", NOISY_ACTION)
        logs = io.StringIO()
        with redirect_stderr(logs):
            ok, result = run_isolated_action(
                entry, {"id": "noisy"}, {}, {}, execute_timeout=5.0
            )
        assert ok is True
        assert result == {"completed": True}
        assert len(logs.getvalue()) >= 262144

    def test_raising_action_reports_error(self, tmp_path):
        entry = write_action(tmp_path, "raising", RAISING_ACTION)
        ok, error = run_isolated_action(entry, {}, {}, {})
        assert ok is False
        assert "动作故意失败" in error

    def test_execute_timeout_kills_worker(self, monkeypatch, tmp_path):
        real_popen = plugin_worker.subprocess.Popen

        def start_ready_worker(*args, **kwargs):
            return real_popen(
                [sys.executable, "-c", READY_THEN_SLEEP_WORKER],
                **kwargs,
            )

        monkeypatch.setattr(plugin_worker.subprocess, "Popen", start_ready_worker)
        entry = write_action(tmp_path, "sleepy", SLEEPY_ACTION)
        started = time.time()
        with pytest.raises(PluginWorkerTimeout):
            run_isolated_action(entry, {}, {}, {}, execute_timeout=1.0)
        assert time.time() - started < 1.75

    def test_startup_timeout_kills_worker(self, monkeypatch, tmp_path):
        real_popen = plugin_worker.subprocess.Popen

        def start_silent_worker(*args, **kwargs):
            return real_popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                **kwargs,
            )

        monkeypatch.setattr(plugin_worker.subprocess, "Popen", start_silent_worker)
        entry = write_action(tmp_path, "simple_startup", SIMPLE_ACTION)
        started = time.time()
        with pytest.raises(PluginWorkerStartupTimeout):
            run_isolated_action(entry, {}, {}, {}, startup_timeout=0.2)
        assert time.time() - started < 1.0
        assert not plugin_worker._live_processes

    def test_exit_timeout_kills_worker_after_result(self, monkeypatch, tmp_path):
        real_popen = plugin_worker.subprocess.Popen

        def start_hanging_worker(*args, **kwargs):
            return real_popen(
                [sys.executable, "-c", READY_RESULT_THEN_SLEEP_WORKER],
                **kwargs,
            )

        monkeypatch.setattr(plugin_worker.subprocess, "Popen", start_hanging_worker)
        monkeypatch.setattr(plugin_worker, "EXIT_TIMEOUT", 0.2)
        entry = write_action(tmp_path, "hanging_exit", SIMPLE_ACTION)
        started = time.time()

        ok, result = run_isolated_action(entry, {}, {}, {})

        assert ok is True
        assert result == {"completed": True}
        assert time.time() - started < 1.0
        assert not plugin_worker._live_processes

    def test_context_v1_receives_sanitized_context(self, tmp_path):
        entry = write_action(tmp_path, "context", CONTEXT_ACTION)
        context = {
            "run": {"id": "run_context_1"},
            "event": {"payload": {"value": 42}},
            "_run_cancel_event": threading.Event(),
        }
        ok, result = run_isolated_action(
            entry,
            {"id": "context", "execution_api": "context-v1"},
            {},
            context,
        )
        assert ok is True
        assert result == {"run_id": "run_context_1", "event_value": 42}

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
    def _make_plugin(
        self,
        tmp_path,
        execution_mode="isolated",
        context_api=False,
        source=None,
    ):
        from types import SimpleNamespace

        from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
        from notmyfault.security.security import SecurityMode

        plugin_dir = tmp_path / "actions" / "iso_action"
        plugin_dir.mkdir(parents=True)
        if source is None:
            source = (
                CONTEXT_ACTION
                if context_api
                else "def run(action_info, params):\n"
                     "    return {'doubled': params.get('n', 0) * 2}\n"
            )
        (plugin_dir / "action.py").write_text(source, encoding="utf-8")
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
        if context_api:
            meta["execution_api"] = "context-v1"
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
            plugin_manifest_path=str(tmp_path / "manifest.json"),
        )
        return loader

    @pytest.mark.parametrize("origin", ["builtin", "user"])
    def test_isolated_action_runs_in_worker(self, tmp_path, origin):
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
            origin=origin,
        )
        assert (loaded, failed) == (1, 0)
        assert "iso_action" not in func_store

        run = loader.materialize_pending_action("iso_action")
        assert run is not None
        # isolated 动作不 import 进引擎进程
        assert loader._registry.get_module("iso_action") is None
        assert func_store["iso_action"]({}, {"n": 21}) == {"doubled": 42}

    def test_isolated_context_action_runs_through_registered_proxy(self, tmp_path):
        from notmyfault.core.workflow import invoke_action

        loader = self._make_plugin(tmp_path, context_api=True)
        meta_store, func_store = {}, {}
        loaded, failed = loader.load(
            base_dir=str(tmp_path),
            plugins_dir="actions",
            json_filename="action.json",
            py_filename="action.py",
            module_prefix="notmyfault.action_iso_context_",
            meta_store=meta_store,
            func_store=func_store,
            store_name="Actioner",
            origin="user",
        )
        assert (loaded, failed) == (1, 0)

        assert loader.materialize_pending_action("iso_action") is not None
        run = func_store["iso_action"]
        module = loader._registry.get_module("iso_action")
        result = invoke_action(
            run,
            module,
            meta_store["iso_action"],
            {},
            {
                "run": {"id": "run_proxy_1"},
                "event": {"payload": {"value": 7}},
            },
        )
        assert result == {"run_id": "run_proxy_1", "event_value": 7}

    def test_in_process_user_plugin_imports_public_plugin_api(self, tmp_path):
        loader = self._make_plugin(
            tmp_path,
            execution_mode="in-process",
            source=PLUGIN_API_ACTION,
        )
        meta_store, func_store = {}, {}
        loaded, failed = loader.load(
            base_dir=str(tmp_path),
            plugins_dir="actions",
            json_filename="action.json",
            py_filename="action.py",
            module_prefix="notmyfault.action_public_api_",
            meta_store=meta_store,
            func_store=func_store,
            store_name="Actioner",
            origin="user",
        )
        assert (loaded, failed) == (1, 0)
        assert loader.materialize_pending_action("iso_action") is not None
        result = func_store["iso_action"]({}, {})
        assert result["platform"] in {"windows", "linux", "macos"}
