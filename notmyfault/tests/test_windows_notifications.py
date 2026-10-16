import importlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 通知")


@pytest.fixture
def notifications(monkeypatch):
    module = importlib.import_module("Win_toaster.show_notification")
    windows_toasts = importlib.import_module("windows_toasts")
    winrt_runtime = importlib.import_module("winrt.runtime")
    fake_windows_toasts = SimpleNamespace(**vars(windows_toasts))
    fake_runtime = SimpleNamespace(**vars(winrt_runtime))
    state = SimpleNamespace(
        events=[],
        on_show=None,
        released=threading.Event(),
        runtime=fake_runtime,
        Toast=windows_toasts.Toast,
    )

    def record(operation, toast=None, owner=None):
        state.events.append((operation, toast, owner, threading.current_thread()))

    def initialize(apartment):
        record("initialize")

    def uninitialize():
        record("uninitialize")
        state.released.set()

    class Toaster:
        def __init__(self, *args):
            self.owner = threading.current_thread()

        def show_toast(self, toast):
            record("show", toast, self.owner)
            if state.on_show is not None:
                state.on_show(toast)

        def remove_toast(self, toast):
            record("remove", toast, self.owner)

    fake_windows_toasts.InteractableWindowsToaster = Toaster
    fake_runtime.init_apartment = initialize
    fake_runtime.uninit_apartment = uninitialize
    monkeypatch.setitem(sys.modules, "windows_toasts", fake_windows_toasts)
    monkeypatch.setitem(sys.modules, "winrt.runtime", fake_runtime)
    return module, state


def test_parallel_notifications_send_and_remove_on_their_own_threads(notifications):
    module, state = notifications
    toasts = [state.Toast([title, ""]) for title in ("提醒", "第二条", "第三条")]
    dismiss = threading.Event()
    workers = []
    try:
        with ThreadPoolExecutor(max_workers=3) as callers:
            pending = [callers.submit(module.show_toast, toast, 30, dismiss) for toast in toasts]
            workers = [result.result(timeout=5) for result in pending]
        assert all(worker.is_alive() for worker in workers)
        assert not any(event[0] == "remove" for event in state.events)
    finally:
        dismiss.set()
        for worker in workers:
            worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    for toast, worker in zip(toasts, workers):
        events = [event for event in state.events if event[1] is toast]
        assert [(event[0], event[2], event[3]) for event in events] == [
            ("show", worker, worker), ("remove", worker, worker),
        ]
        assert [event[0] for event in state.events if event[3] is worker] == [
            "initialize", "show", "remove", "uninitialize",
        ]


@pytest.mark.parametrize("stage", ["initialize", "show"])
def test_notification_send_failure_reaches_the_caller(notifications, monkeypatch, stage):
    module, state = notifications

    def fail(*args):
        raise OSError("notification unavailable")

    if stage == "initialize":
        monkeypatch.setattr(state.runtime, "init_apartment", fail)
    else:
        state.on_show = fail

    with pytest.raises(OSError, match="notification unavailable"):
        module.show_notification("提醒", "", display_seconds=0)
    if stage != "initialize":
        assert state.released.wait(timeout=5)
    assert not any(event[0] == "remove" for event in state.events)


def test_missing_notification_sdk_import_reaches_the_caller(notifications, monkeypatch):
    module, state = notifications
    toast = state.Toast()
    monkeypatch.setitem(sys.modules, "windows_toasts", None)
    errors = []
    finished = threading.Event()

    def send():
        try:
            module.show_toast(toast, 0)
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    threading.Thread(target=send, daemon=True).start()
    assert finished.wait(timeout=5)
    assert len(errors) == 1 and isinstance(errors[0], ImportError)



@pytest.mark.parametrize("decision", ["approve", "deny", "cancel"])
def test_admin_notification_applies_decision_and_removes_toast(notifications, decision):
    from notmyfault.security import admin_prompt

    module, state = notifications

    def decide(toast):
        if decision == "cancel":
            admin_prompt.cancel_pending_admin_requests()
        else:
            toast.on_activated(SimpleNamespace(arguments=decision))

    state.on_show = decide
    assert admin_prompt.confirm_admin_request("plugin", "program.exe", timeout=1) is (
        decision == "approve"
    )
    assert [event[0] for event in state.events] == [
        "initialize", "show", "remove", "uninitialize",
    ]
    assert not admin_prompt._pending_decisions
