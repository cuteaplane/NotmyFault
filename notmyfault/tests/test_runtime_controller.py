"""RuntimeController 的代际管理、并发启动与失败上报"""

import threading
import time
import runpy
import signal
from pathlib import Path

from notmyfault.core.runtime_controller import RuntimeController
from notmyfault.security.security import SecurityMode
from notmyfault.tests.api_support import create_test_engine


class FakeEngine:
    def __init__(self):
        self.started = threading.Event()
        self.shutdown_event = None

    def start(self, shutdown_event):
        self.shutdown_event = shutdown_event
        self.started.set()
        shutdown_event.wait(timeout=10)


def _wait(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_runtime_controller_owns_generation_and_current_engine():
    created = []

    def factory(on_event=None):
        engine = FakeEngine()
        created.append(engine)
        return engine

    controller = RuntimeController(factory)
    assert controller.status().generation == 0
    assert controller.current_engine is None

    assert controller.start() is True
    assert _wait(lambda: controller.running)
    assert controller.current_engine is created[0]
    assert controller.status().generation == 1

    assert controller.stop() is True
    assert controller.state == "stopped"
    assert controller.current_engine is None

    # 再次启动产生新一代引擎，代际递增
    assert controller.start() is True
    assert _wait(lambda: controller.running)
    assert controller.status().generation == 2
    assert controller.current_engine is created[1]
    controller.stop()


def test_desktop_runner_forwards_event_sink_to_engine_factory(monkeypatch, tmp_path):
    launcher_path = Path(__file__).resolve().parents[2] / "NOTMYFAULT.pyw"
    namespace = runpy.run_path(str(launcher_path), run_name="notmyfault_launcher_test")
    observed = {}
    engine = object()

    def create_engine(*, store, on_event=None):
        observed["store"] = store
        observed["on_event"] = on_event
        return engine

    monkeypatch.setattr(signal, "signal", lambda *args: None)
    paths = namespace["ApplicationPaths"](
        config_dir=tmp_path / "config",
        package_root=tmp_path / "package",
        project_root=tmp_path,
    )
    store = namespace["SignedConfigStore"](paths)
    runner = namespace["EngineRunner"](paths, store, create_engine)
    event_sink = lambda event_type, data: None

    created = runner._runtime._engine_factory(on_event=event_sink)

    assert created is engine
    assert observed == {"store": runner._store, "on_event": event_sink}
    from types import SimpleNamespace

    notifications = []
    runner._tray = SimpleNamespace(
        set_engine_state=lambda state: None,
        show_balloon=lambda *args: notifications.append(args),
    )
    runner._handle_engine_state("starting")
    runner._handle_engine_state("stopped")
    assert notifications == []
    runner._handle_engine_state("running")
    assert notifications == [("NotmyFault", "引擎已启动")]
    cleanup = []
    runner._runtime = SimpleNamespace(
        request_stop=lambda: cleanup.append("request_stop"),
        stop=lambda timeout: cleanup.append("engine_stop") or True,
    )
    runner._tray.stop = lambda: cleanup.append("tray_stop")
    runner._run_history = SimpleNamespace(flush=lambda: cleanup.append("history_flush") or True)
    runner._cleanup()
    assert cleanup == ["request_stop", "tray_stop", "engine_stop", "history_flush"]


def test_runtime_controller_rejects_restart_while_old_thread_is_stopping():
    class DirtyEngine(FakeEngine):
        _shutdown_clean = False
        release = False

        def shutdown(self, timeout):
            self._shutdown_clean = self.release

    engine = DirtyEngine()
    controller = RuntimeController(lambda on_event=None: engine)
    assert controller.start() is True
    assert _wait(lambda: controller.running)

    # 只发出停止信号，线程仍在收尾时不允许启动新一代
    assert controller.request_stop() is True
    if controller.engine_thread.is_alive():
        assert controller.start() is False
    assert controller.stop() is False
    assert controller.state == "stopping"
    assert controller.current_engine is engine
    assert controller.start() is False
    engine.release = True
    assert controller.stop() is True
    assert controller.current_engine is None
    assert controller.start() is True
    controller.stop()


def test_runtime_controller_reports_factory_failure():
    failures = []
    events = []

    def factory(on_event=None):
        raise RuntimeError("factory exploded")

    controller = RuntimeController(
        factory,
        event_sink=lambda t, d: events.append((t, d)),
        failure_listener=failures.append,
    )
    assert controller.start() is True
    assert _wait(lambda: controller.state == "stopped")
    assert len(failures) == 1
    assert str(failures[0]) == "factory exploded"
    assert controller.status().last_error == "factory exploded"
    assert ("error", {"error": "factory exploded"}) in events


def test_runtime_controller_reports_integrity_start_failure(monkeypatch):
    from notmyfault.core import engine as engine_module

    alerts = []
    events = []

    def factory(on_event=None):
        engine = create_test_engine({"rules": []}, on_event=on_event)
        engine._security_mode = SecurityMode.STRICT
        engine._alert_user = lambda *args, **kwargs: alerts.append((args, kwargs))
        return engine

    monkeypatch.setattr(
        engine_module,
        "verify_core_integrity",
        lambda: (False, ["notmyfault/core/engine.py"]),
    )
    controller = RuntimeController(
        factory,
        event_sink=lambda event_type, data: events.append((event_type, data)),
    )

    assert controller.start() is True
    assert _wait(lambda: controller.state == "stopped")
    assert controller.current_engine is None
    assert controller.status().last_error == "核心文件完整性校验失败"
    assert ("error", {"error": "核心文件完整性校验失败"}) in events
    assert alerts[0][0][0] == "NotmyFault 完整性校验失败"


def test_runtime_controller_serializes_concurrent_start_requests():
    started = threading.Event()

    class BlockingEngine(FakeEngine):
        def start(self, shutdown_event):
            started.set()
            super().start(shutdown_event)

    created = []

    def factory(on_event=None):
        engine = BlockingEngine()
        created.append(engine)
        return engine

    controller = RuntimeController(factory)
    results = []
    barrier = threading.Barrier(4)

    def attempt():
        barrier.wait()
        results.append(controller.start())

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    started.wait(timeout=5)
    for t in threads:
        t.join(timeout=5)
    assert results.count(True) == 1
    assert len(created) == 1
    controller.stop()
