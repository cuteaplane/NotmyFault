import importlib.util
import json
import socket
import threading
from pathlib import Path

import pytest

from notmyfault.security.plugin_schema import check_payload_contract
from notmyfault.triggers.base import PollingTrigger


TRIGGERS = Path(__file__).resolve().parents[1] / "triggers"


def test_partial_trigger_setup_releases_resources():
    calls = []
    class Trigger(PollingTrigger):
        def setup(self):
            calls.append("opened")
            raise RuntimeError("setup failed")
        def teardown(self):
            calls.append("closed")
    with pytest.raises(RuntimeError, match="setup failed"):
        Trigger({}, {}, lambda event: None, threading.Event()).run()
    assert calls == ["opened", "closed"]


def test_folder_read_error_keeps_the_last_complete_snapshot(tmp_path, monkeypatch):
    tmp_path = tmp_path / " watched\u3000"
    tmp_path.mkdir()
    module, meta = load_trigger("folder_monitor")
    path = tmp_path / "watched.txt"
    path.touch()
    events = []
    trigger = module.FolderMonitorTrigger(meta, {"folder_path": str(tmp_path), "event_type": "all"}, events.append, threading.Event())
    trigger.validate()
    trigger.setup()
    original_stat = module.os.stat
    def failing_stat(target, *args, **kwargs):
        if str(target) == str(path):
            raise PermissionError("unreadable")
        return original_stat(target, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "stat", failing_stat)
        with pytest.raises(PermissionError):
            trigger.poll()
    trigger.poll()
    assert events == []
    path.unlink()
    trigger.poll()
    assert [event["event"] for event in events] == ["deleted"]


def load_trigger(name):
    folder = TRIGGERS / name
    spec = importlib.util.spec_from_file_location(f"polling_{name}", folder / "trigger.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    meta = json.loads((folder / "trigger.json").read_text(encoding="utf-8"))
    return module, meta


@pytest.mark.parametrize("event_type", ["created", "deleted", "modified", "all"])
@pytest.mark.parametrize("fail_at", [1, 2])
def test_folder_reports_each_change_once_after_delivery_failure(tmp_path, event_type, fail_at):
    module, meta = load_trigger("folder_monitor")
    old_files = {}
    for change in ("deleted", "modified"):
        for name in ("a", "b"):
            path = tmp_path / f"{change}-{name}.txt"
            path.write_text("old", encoding="utf-8")
            old_files[(change, str(path))] = path
    events = []
    attempts = 0

    def emit(payload):
        nonlocal attempts
        attempts += 1
        if attempts == fail_at:
            raise RuntimeError("事件提交失败")
        events.append(payload)

    trigger = module.FolderMonitorTrigger(
        meta, {"folder_path": str(tmp_path), "event_type": event_type}, emit, threading.Event(),
    )
    trigger.validate()
    trigger.setup()
    changes = set(old_files)
    for (change, _), path in old_files.items():
        if change == "deleted":
            path.unlink()
        else:
            path.write_text("updated", encoding="utf-8")
    for name in ("a", "b"):
        path = tmp_path / f"created-{name}.txt"
        path.write_text("new", encoding="utf-8")
        changes.add(("created", str(path)))
    with pytest.raises(RuntimeError):
        trigger.poll()
    assert len(events) == fail_at - 1
    trigger.poll()
    trigger.poll()
    expected = {item for item in changes if event_type in (item[0], "all")}
    assert {(event["event"], event["path"]) for event in events} == expected
    assert len(events) == len(expected)
    assert all(check_payload_contract(meta["outputs"], event) == [] for event in events)


@pytest.mark.parametrize("target", ["quiescent", "active", "changed"])
@pytest.mark.parametrize("fail_delivery", [False, True])
def test_directory_quiescent_reports_selected_transitions(tmp_path, monkeypatch, target, fail_delivery):
    monkeypatch.chdir(tmp_path)
    tmp_path = tmp_path / "\u3000"
    tmp_path.mkdir()
    module, meta = load_trigger("directory_quiescent")
    (tmp_path / "a.txt").write_text("ready", encoding="utf-8")
    clock = {"t": 1000.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    events = []
    failed_states = set()

    def emit(payload):
        if fail_delivery and payload["state"] not in failed_states:
            failed_states.add(payload["state"])
            raise RuntimeError("事件提交失败")
        events.append(payload)

    trigger = module.DirectoryQuiescentTrigger(
        meta,
        {
            "folder_path": tmp_path.name,
            "quiet_seconds": 10,
            "state": target,
            "interval": 1,
        },
        emit,
        threading.Event(),
    )
    trigger.validate()
    trigger.setup()

    def poll_with_retry():
        try:
            trigger.poll()
        except RuntimeError:
            assert fail_delivery
        trigger.poll()

    trigger.poll()
    assert events == []
    clock["t"] = 1010.0
    poll_with_retry()
    (tmp_path / "b.txt").write_text("new", encoding="utf-8")
    clock["t"] = 1011.0
    poll_with_retry()
    clock["t"] = 1021.0
    poll_with_retry()
    expected = [state for state in ["quiescent", "active", "quiescent"] if target in (state, "changed")]
    assert [event["state"] for event in events] == expected
    counts = [count for state, count in [("quiescent", 1), ("active", 2), ("quiescent", 2)] if target in (state, "changed")]
    assert [event["file_count"] for event in events] == counts
    assert all(event["folder"] == str(tmp_path.resolve()) for event in events)
    assert all(check_payload_contract(meta["outputs"], event) == [] for event in events)


def test_directory_quiescent_ignores_non_matching_files(tmp_path, monkeypatch):
    module, meta = load_trigger("directory_quiescent")
    (tmp_path / "keep.pdf").write_text("a", encoding="utf-8")
    clock = {"t": 100.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    events = []
    trigger = module.DirectoryQuiescentTrigger(
        meta,
        {
            "folder_path": str(tmp_path),
            "quiet_seconds": 5,
            "file_pattern": "*.pdf",
            "state": "changed",
        },
        events.append,
        threading.Event(),
    )
    trigger.validate()
    trigger.setup()
    (tmp_path / "noise.txt").write_text("ignore", encoding="utf-8")
    clock["t"] = 101.0
    trigger.poll()
    clock["t"] = 105.0
    trigger.poll()
    assert [event["state"] for event in events] == ["quiescent"]
    assert events[0]["file_count"] == 1


def test_directory_read_error_keeps_the_last_complete_snapshot(tmp_path, monkeypatch):
    module, meta = load_trigger("directory_quiescent")
    path = tmp_path / "watched.txt"
    path.write_text("x", encoding="utf-8")
    clock = {"t": 50.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    events = []
    trigger = module.DirectoryQuiescentTrigger(
        meta,
        {"folder_path": str(tmp_path), "quiet_seconds": 5, "state": "changed"},
        events.append,
        threading.Event(),
    )
    trigger.validate()
    trigger.setup()
    original_stat = module.os.stat

    def failing_stat(target, *args, **kwargs):
        if str(target) == str(path):
            raise PermissionError("unreadable")
        return original_stat(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "stat", failing_stat)
        trigger.poll()
    assert events == []
    clock["t"] = 55.0
    trigger.poll()
    assert [event["state"] for event in events] == ["quiescent"]


class PollSteps:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, timeout):
        step = next(self.steps, None)
        if step is None:
            self.stopped = True
        else:
            step()
        return self.stopped


def recording_emitter(events, fail_first):
    failed = False

    def emit(payload):
        nonlocal failed
        if fail_first and not failed:
            failed = True
            raise RuntimeError("事件提交失败")
        events.append(payload)

    return emit


@pytest.mark.parametrize("kind", ["file", "directory", "any"])
@pytest.mark.parametrize("target", ["exists", "missing", "changed"])
@pytest.mark.parametrize("fail_first", [False, True])
@pytest.mark.parametrize("name", ["watched", "\u3000"])
def test_path_reports_only_selected_transitions(tmp_path, monkeypatch, kind, target, fail_first, name):
    monkeypatch.chdir(tmp_path)
    module, meta = load_trigger("path_exists")
    path = tmp_path / name
    create = path.mkdir if kind == "directory" else path.touch
    remove = path.rmdir if kind == "directory" else path.unlink
    create()
    events = []
    steps = PollSteps([lambda: None, remove, lambda: None, create, lambda: None])

    module.run(meta, {"path": name, "kind": kind, "state": target}, recording_emitter(events, fail_first), steps)

    expected = [state for state in ["missing", "exists"] if target in (state, "changed")]
    assert [event["state"] for event in events] == expected
    assert all(event["exists"] == (event["state"] == "exists") for event in events)
    assert all(check_payload_contract(meta["outputs"], event) == [] for event in events)


def test_path_read_failure_keeps_the_last_successful_state(tmp_path, monkeypatch):
    module, meta = load_trigger("path_exists")
    path = tmp_path / "watched.txt"
    path.touch()
    original_stat = Path.stat
    events = []

    def fail_stat(self, *args, **kwargs):
        if self == path:
            raise PermissionError("测试读取失败")
        return original_stat(self, *args, **kwargs)

    steps = PollSteps([
        lambda: monkeypatch.setattr(Path, "stat", fail_stat),
        lambda: monkeypatch.setattr(Path, "stat", original_stat),
        path.unlink,
        lambda: None,
    ])
    module.run(meta, {"path": str(path), "state": "changed"}, events.append, steps)

    assert [event["state"] for event in events] == ["missing"]


@pytest.mark.parametrize("encoding,case_sensitive,text", [
    ("utf-8-sig", False, "READY"),
    ("utf-16", True, "ready"),
    ("gb18030", True, "ready"),
])
@pytest.mark.parametrize("target", ["contains", "absent", "changed"])
@pytest.mark.parametrize("fail_first", [False, True])
@pytest.mark.parametrize("name", ["status.txt", "\u3000"])
def test_file_content_reports_selected_text_transitions(
    tmp_path, monkeypatch, encoding, case_sensitive, text, target, fail_first, name
):
    monkeypatch.chdir(tmp_path)
    module, meta = load_trigger("file_content")
    path = tmp_path / name
    path.write_text("准备 ready", encoding=encoding)
    events = []
    steps = PollSteps([
        lambda: None,
        lambda: path.write_text("正在工作", encoding=encoding),
        lambda: None,
        lambda: path.write_text("准备 ready", encoding=encoding),
        lambda: None,
    ])

    module.run(meta, {
        "path": name, "text": text, "state": target,
        "encoding": encoding, "case_sensitive": case_sensitive,
    }, recording_emitter(events, fail_first), steps)

    expected = [state for state in ["absent", "contains"] if target in (state, "changed")]
    assert [event["state"] for event in events] == expected
    assert all(event["contains"] == (event["state"] == "contains") for event in events)
    assert all(check_payload_contract(meta["outputs"], event) == [] for event in events)


@pytest.mark.parametrize("failure", ["missing", "encoding", "oversized"])
def test_file_read_failure_is_not_a_text_transition(tmp_path, failure):
    module, meta = load_trigger("file_content")
    path = tmp_path / "status.txt"
    path.write_text("ready", encoding="utf-8")
    events = []
    failures = {
        "missing": path.unlink,
        "encoding": lambda: path.write_bytes(b"\xff"),
        "oversized": lambda: path.write_text("x" * 1025, encoding="utf-8"),
    }
    steps = PollSteps([
        failures[failure],
        lambda: path.write_text("ready", encoding="utf-8"),
        lambda: path.write_text("working", encoding="utf-8"),
        lambda: None,
    ])

    module.run(meta, {
        "path": str(path), "text": "ready", "state": "changed", "max_kib": 1,
    }, events.append, steps)

    assert [event["state"] for event in events] == ["absent"]


def test_file_first_successful_read_records_state_without_firing(tmp_path):
    module, meta = load_trigger("file_content")
    path = tmp_path / "status.txt"
    events = []
    steps = PollSteps([
        lambda: path.write_text("ready", encoding="utf-8"),
        lambda: None,
        lambda: path.write_text("working", encoding="utf-8"),
    ])

    module.run(meta, {
        "path": str(path), "text": "ready", "state": "changed",
    }, events.append, steps)

    assert [event["state"] for event in events] == ["absent"]


@pytest.mark.parametrize("plugin_id,target", [
    ("tcp_port", "reachable"), ("tcp_port", "unreachable"), ("tcp_port", "changed"),
    ("network_status", "connected"), ("network_status", "disconnected"),
])
@pytest.mark.parametrize("family,host", [
    (socket.AF_INET, "127.0.0.1"),
    pytest.param(socket.AF_INET6, "::1", marks=pytest.mark.skipif(not socket.has_ipv6, reason="系统未提供 IPv6 socket")),
])
@pytest.mark.parametrize("fail_first", [False, True])
def test_tcp_probes_report_local_service_transitions(plugin_id, target, family, host, fail_first):
    module, meta = load_trigger(plugin_id)
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.bind((host, 0))
    port = listener.getsockname()[1]
    events = []
    steps = PollSteps([lambda: None, lambda: listener.listen(8), lambda: None, listener.close, lambda: None])

    try:
        module.run(meta, {
            "host": host, "port": port, "state": target, "timeout": 0.1,
        }, recording_emitter(events, fail_first), steps)
    finally:
        listener.close()

    transitions = ["reachable", "unreachable"] if plugin_id == "tcp_port" else ["connected", "disconnected"]
    expected = [state for state in transitions if target in (state, "changed")]
    assert [event["state"] for event in events] == expected
    if plugin_id == "tcp_port":
        assert all(event["reachable"] == (event["state"] == "reachable") for event in events)
    assert all(check_payload_contract(meta["outputs"], event) == [] for event in events)


@pytest.mark.parametrize("name,config", [
    ("path_exists", {"path": "watched.txt"}),
    ("file_content", {"path": "watched.txt", "text": "ready"}),
    ("tcp_port", {"host": "127.0.0.1", "port": 80}),
    ("directory_quiescent", {"folder_path": "."}),
])
def test_polling_wait_stops_promptly(name, config, monkeypatch, tmp_path):
    module, meta = load_trigger(name)
    if name == "directory_quiescent":
        config = {**config, "folder_path": str(tmp_path)}
    trigger_class = {
        "path_exists": "PathExistsTrigger", "file_content": "FileContentTrigger",
        "tcp_port": "TcpPortTrigger",
        "directory_quiescent": "DirectoryQuiescentTrigger",
    }[name]
    sampled = threading.Event()
    shutdown = threading.Event()
    monkeypatch.setattr(getattr(module, trigger_class), "poll", lambda self: sampled.set())
    worker = threading.Thread(target=module.run, args=(
        meta, {**config, "interval": 3600}, lambda event: None, shutdown,
    ))
    worker.start()
    try:
        assert sampled.wait(2)
        shutdown.set()
        worker.join(1)
        assert not worker.is_alive()
    finally:
        shutdown.set()
        worker.join(2)


@pytest.mark.parametrize("plugin_id", ["tcp_port", "network_status"])
def test_tcp_stop_during_connect_does_not_emit_a_late_event(monkeypatch, plugin_id):
    module, meta = load_trigger(plugin_id)
    shutdown = PollSteps([lambda: None])
    attempts = []
    events = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def connect(address, timeout):
        assert timeout == 0.1
        attempts.append(address)
        if len(attempts) == 1:
            raise ConnectionRefusedError()
        shutdown.set()
        return Connection()

    monkeypatch.setattr(module.socket, "create_connection", connect)
    module.run(meta, {
        "timeout": 0.1, "state": "reachable" if plugin_id == "tcp_port" else "connected",
    }, events.append, shutdown)

    assert len(attempts) == 2
    assert events == []


@pytest.mark.parametrize("plugin_id", ["tcp_port", "network_status"])
@pytest.mark.parametrize("host", ["service.example", "localhost"])
@pytest.mark.parametrize("families", [
    (socket.AF_INET6, socket.AF_INET),
    (socket.AF_INET, socket.AF_INET6),
])
def test_tcp_hostname_tries_both_address_families(monkeypatch, plugin_id, host, families):
    module, meta = load_trigger(plugin_id)
    resolutions = []
    attempts = []
    closed = []
    events = []
    addresses = {
        socket.AF_INET: ("127.0.0.1", 8080),
        socket.AF_INET6: ("::1", 8080, 0, 0),
    }

    def resolve(name, port, family, kind):
        resolutions.append((name, port))
        return [(af, kind, 6, "", addresses[af]) for af in families]

    class Connection:
        def __init__(self, family, kind, protocol):
            self.family = family

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def settimeout(self, timeout):
            assert timeout == 0.1

        def connect(self, address):
            attempts.append((self.family, address))
            if len(resolutions) == 1 or self.family == families[0]:
                raise ConnectionRefusedError()

        def close(self):
            closed.append(self.family)

    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(module.socket, "socket", Connection)
    module.run(meta, {
        "host": host, "port": 8080, "timeout": 0.1,
        "state": "reachable" if plugin_id == "tcp_port" else "connected",
    }, events.append, PollSteps([lambda: None]))

    assert resolutions == [(host, 8080), (host, 8080)]
    assert [family for family, address in attempts] == list(families) * 2
    assert closed == list(families) * 2
    expected = {"host": host, "port": 8080, "state": "reachable", "reachable": True} if plugin_id == "tcp_port" else {"state": "connected"}
    assert events == [expected]


@pytest.mark.parametrize("plugin_id", ["tcp_port", "network_status"])
@pytest.mark.parametrize("blocking_call", [1, 2])
def test_tcp_pending_dns_can_stop_without_more_samples(monkeypatch, plugin_id, blocking_call):
    module, meta = load_trigger(plugin_id)
    if plugin_id == "network_status":
        monkeypatch.setattr(module.NetworkStatusTrigger, "interval", 0.2)
    resolving = threading.Event()
    release = threading.Event()
    resolved = threading.Event()
    checked_pending = threading.Event()

    class ObservedStop(threading.Event):
        checks = 0

        def is_set(self):
            if resolving.is_set() and not release.is_set():
                self.checks += 1
                if self.checks >= 3:
                    checked_pending.set()
            return super().is_set()

    shutdown = ObservedStop()
    resolutions = []
    events = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    address = listener.getsockname()

    def resolve(host, port, family, kind):
        resolutions.append(host)
        if len(resolutions) < blocking_call:
            return [(socket.AF_INET, kind, 6, "", address)]
        resolving.set()
        release.wait(5)
        resolved.set()
        return []

    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    worker = threading.Thread(target=module.run, args=(
        meta, {"host": "service.example", "interval": 0.2,
               "state": "changed" if plugin_id == "tcp_port" else "disconnected"},
        events.append, shutdown,
    ))
    worker.start()
    try:
        assert resolving.wait(2)
        assert checked_pending.wait(3)
        assert len(resolutions) == blocking_call
        assert events == []
        shutdown.set()
        worker.join(1)
        assert not worker.is_alive()
        assert resolutions == ["service.example"] * blocking_call
        assert events == []
    finally:
        shutdown.set()
        release.set()
        worker.join(2)
        listener.close()
        assert resolved.wait(2)
    assert resolutions == ["service.example"] * blocking_call
    assert events == []


@pytest.mark.parametrize("name,config", [
    ("path_exists", {"path": "watched.txt", "interval": float("nan")}),
    ("file_content", {"path": "watched.txt", "text": "ready", "max_kib": 16385}),
    ("tcp_port", {"host": "", "port": 80}),
    ("tcp_port", {"host": "127.0.0.1", "port": 65536}),
    ("directory_quiescent", {"folder_path": ".", "quiet_seconds": -1}),
    ("directory_quiescent", {"folder_path": ".", "interval": float("nan")}),
])
def test_invalid_polling_configuration_is_rejected(name, config, tmp_path):
    module, meta = load_trigger(name)
    if name == "directory_quiescent":
        config = {**config, "folder_path": str(tmp_path)}
    with pytest.raises(ValueError):
        module.run(meta, config, lambda event: None, threading.Event())
