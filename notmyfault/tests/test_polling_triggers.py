import importlib.util
import json
import socket
import threading
from pathlib import Path

import pytest

from notmyfault.security.plugin_schema import check_payload_contract


TRIGGERS = Path(__file__).resolve().parents[1] / "triggers"


def load_trigger(name):
    folder = TRIGGERS / name
    spec = importlib.util.spec_from_file_location(f"polling_{name}", folder / "trigger.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    meta = json.loads((folder / "trigger.json").read_text(encoding="utf-8"))
    return module, meta


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


@pytest.mark.parametrize("kind", ["file", "directory", "any"])
@pytest.mark.parametrize("target", ["exists", "missing", "changed"])
def test_path_reports_only_selected_transitions(tmp_path, kind, target):
    module, meta = load_trigger("path_exists")
    path = tmp_path / "watched"
    create = path.mkdir if kind == "directory" else path.touch
    remove = path.rmdir if kind == "directory" else path.unlink
    create()
    events = []
    steps = PollSteps([lambda: None, remove, lambda: None, create, lambda: None])

    module.run(meta, {"path": str(path), "kind": kind, "state": target}, events.append, steps)

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
def test_file_content_reports_selected_text_transitions(
    tmp_path, encoding, case_sensitive, text, target
):
    module, meta = load_trigger("file_content")
    path = tmp_path / "status.txt"
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
        "path": str(path), "text": text, "state": target,
        "encoding": encoding, "case_sensitive": case_sensitive,
    }, events.append, steps)

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


@pytest.mark.parametrize("target", ["reachable", "unreachable", "changed"])
def test_tcp_port_reports_local_service_transitions(target):
    module, meta = load_trigger("tcp_port")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    events = []
    steps = PollSteps([lambda: None, lambda: listener.listen(8), lambda: None, listener.close])

    try:
        module.run(meta, {
            "host": "localhost", "port": port, "state": target, "timeout": 0.1,
        }, events.append, steps)
    finally:
        listener.close()

    expected = [state for state in ["reachable", "unreachable"] if target in (state, "changed")]
    assert [event["state"] for event in events] == expected
    assert all(event["reachable"] == (event["state"] == "reachable") for event in events)
    assert all(check_payload_contract(meta["outputs"], event) == [] for event in events)


@pytest.mark.parametrize("name,config", [
    ("path_exists", {"path": "watched.txt"}),
    ("file_content", {"path": "watched.txt", "text": "ready"}),
    ("tcp_port", {"host": "127.0.0.1", "port": 80}),
])
def test_polling_wait_stops_promptly(name, config, monkeypatch):
    module, meta = load_trigger(name)
    trigger_class = {
        "path_exists": "PathExistsTrigger", "file_content": "FileContentTrigger",
        "tcp_port": "TcpPortTrigger",
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


def test_tcp_stop_during_connect_does_not_emit_a_late_event(monkeypatch):
    module, meta = load_trigger("tcp_port")
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
    module.run(meta, {"timeout": 0.1}, events.append, shutdown)

    assert len(attempts) == 2
    assert events == []


@pytest.mark.parametrize("host", ["service.example", "localhost"])
@pytest.mark.parametrize("families", [
    (socket.AF_INET6, socket.AF_INET),
    (socket.AF_INET, socket.AF_INET6),
])
def test_tcp_hostname_tries_both_address_families(monkeypatch, host, families):
    module, meta = load_trigger("tcp_port")
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
    }, events.append, PollSteps([lambda: None]))

    assert resolutions == [(host, 8080), (host, 8080)]
    assert [family for family, address in attempts] == list(families) * 2
    assert closed == list(families) * 2
    assert events == [{"host": host, "port": 8080, "state": "reachable", "reachable": True}]


def test_tcp_pending_dns_can_stop_without_more_samples(monkeypatch):
    module, meta = load_trigger("tcp_port")
    shutdown = threading.Event()
    resolving = threading.Event()
    release = threading.Event()
    resolved = threading.Event()
    extra_sample = threading.Event()
    resolutions = []
    events = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    address = listener.getsockname()

    def resolve(host, port, family, kind):
        resolutions.append(host)
        if len(resolutions) == 1:
            return [(socket.AF_INET, kind, 6, "", address)]
        if len(resolutions) > 2:
            extra_sample.set()
        resolving.set()
        release.wait(5)
        resolved.set()
        return []

    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    worker = threading.Thread(target=module.run, args=(
        meta, {"host": "service.example", "interval": 0.2, "state": "changed"},
        events.append, shutdown,
    ))
    worker.start()
    try:
        assert resolving.wait(2)
        assert not extra_sample.wait(0.5)
        assert events == []
        shutdown.set()
        worker.join(1)
        assert not worker.is_alive()
        assert resolutions == ["service.example", "service.example"]
        assert events == []
    finally:
        shutdown.set()
        release.set()
        worker.join(2)
        listener.close()
        assert resolved.wait(2)


@pytest.mark.parametrize("name,config", [
    ("path_exists", {"path": "watched.txt", "interval": float("nan")}),
    ("file_content", {"path": "watched.txt", "text": "ready", "max_kib": 16385}),
    ("tcp_port", {"host": "", "port": 80}),
    ("tcp_port", {"host": "127.0.0.1", "port": 65536}),
])
def test_invalid_polling_configuration_is_rejected(name, config):
    module, meta = load_trigger(name)
    with pytest.raises(ValueError):
        module.run(meta, config, lambda event: None, threading.Event())
