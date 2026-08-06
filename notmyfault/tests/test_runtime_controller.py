"""RuntimeController 的代际管理、并发启动与失败上报"""

import threading
import time

from notmyfault.core.runtime_controller import RuntimeController


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


def test_runtime_controller_rejects_restart_while_old_thread_is_stopping():
    controller = RuntimeController(lambda on_event=None: FakeEngine())
    assert controller.start() is True
    assert _wait(lambda: controller.running)

    # 只发出停止信号，线程仍在收尾时不允许启动新一代
    assert controller.request_stop() is True
    if controller.engine_thread.is_alive():
        assert controller.start() is False
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
