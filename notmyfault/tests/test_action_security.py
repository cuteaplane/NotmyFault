from pathlib import Path
from urllib.parse import urlparse

import pytest

from notmyfault.actions.create_shortcut import action as create_shortcut
from notmyfault.actions.http_request import action as http_request
from notmyfault.actions.kill_process import action as kill_process
from notmyfault.actions.launch_program import action as launch_program
from notmyfault.security import network


def test_network_validator_rejects_private_resolution(monkeypatch):
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("10.1.2.3", 0))],
    )
    with pytest.raises(ValueError, match="内网"):
        network.validate_public_http_url("https://example.test/path")


def test_network_validator_fails_closed_when_dns_fails(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("dns unavailable")

    monkeypatch.setattr(network.socket, "getaddrinfo", fail)
    with pytest.raises(ValueError, match="无法解析"):
        network.validate_public_http_url("https://example.test/path")


def test_http_action_rejects_localhost_before_opening_network(monkeypatch):
    opened = []
    monkeypatch.setattr(
        http_request,
        "_PinnedHTTPConnection",
        lambda *args, **kwargs: opened.append((args, kwargs)),
    )
    with pytest.raises(ValueError, match="内网"):
        http_request.run({}, {"url": "http://127.0.0.1/admin"})
    assert opened == []


def test_http_action_rejects_transport_headers(monkeypatch):
    monkeypatch.setattr(
        http_request,
        "resolve_public_http_url",
        lambda url: (urlparse(url), ("203.0.113.10",)),
    )
    with pytest.raises(ValueError, match="Host"):
        http_request.run(
            {},
            {"url": "https://example.com", "headers": "Host: localhost"},
        )


def test_http_action_connects_to_the_validated_address(monkeypatch):
    captured = {}

    class Response:
        status = 200
        reason = "OK"

        @staticmethod
        def read(limit):
            return b"done"

    class Connection:
        def __init__(self, host, addresses, **kwargs):
            captured["host"] = host
            captured["addresses"] = addresses
            captured["kwargs"] = kwargs

        def request(self, method, target, body=None, headers=None):
            captured["request"] = (method, target, body, headers)

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(
        http_request,
        "resolve_public_http_url",
        lambda url: (urlparse(url), ("203.0.113.10",)),
    )
    monkeypatch.setattr(http_request, "_PinnedHTTPConnection", Connection)

    result = http_request.run(
        {},
        {"url": "http://example.test/path?q=1"},
    )

    assert result == {"status": 200, "body": "done", "truncated": False}
    assert captured["host"] == "example.test"
    assert captured["addresses"] == ("203.0.113.10",)
    assert captured["request"][:2] == ("GET", "/path?q=1")


def test_pinned_socket_never_resolves_the_hostname_again(monkeypatch):
    calls = []

    def fake_create_connection(address, timeout, source_address):
        calls.append((address, timeout, source_address))
        return object()

    monkeypatch.setattr(
        http_request.socket,
        "create_connection",
        fake_create_connection,
    )

    sock = http_request._open_pinned_socket(
        ("203.0.113.10",),
        443,
        5,
    )

    assert sock is not None
    assert calls == [(('203.0.113.10', 443), 5, None)]


def test_linux_shortcut_rejects_multiline_fields():
    with pytest.raises(ValueError, match="换行"):
        create_shortcut._run_linux(
            {},
            {
                "name": "safe",
                "target_path": "/usr/bin/tool\nX-GNOME-Autostart-enabled=true",
                "location": "applications",
            },
        )


def test_linux_shortcut_quotes_exec_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    result = create_shortcut._run_linux(
        {},
        {
            "name": "safe",
            "target_path": "/opt/My Tool/tool",
            "arguments": "--label 'two words'",
            "location": "applications",
        },
    )
    content = Path(result["shortcut_path"]).read_text(encoding="utf-8")
    assert 'Exec="/opt/My Tool/tool" "--label" "two words"' in content


def test_launch_program_rejects_unbalanced_quotes():
    with pytest.raises(ValueError, match="引号"):
        launch_program._split_args('"unfinished')


def test_kill_process_rejects_critical_windows_process(monkeypatch):
    monkeypatch.setattr(kill_process.os, "name", "nt")
    with pytest.raises(ValueError, match="关键系统进程"):
        kill_process.run({}, {"process_name": "lsass"})


def test_kill_process_handles_missing_pid_on_access_denied(monkeypatch):
    class DeniedProcess:
        info = {"name": "demo"}

        def terminate(self):
            raise kill_process.psutil.AccessDenied()

    monkeypatch.setattr(kill_process.os, "name", "posix")
    monkeypatch.setattr(
        kill_process.psutil,
        "process_iter",
        lambda fields: [DeniedProcess()],
    )
    with pytest.raises(RuntimeError, match="权限不足"):
        kill_process.run({}, {"process_name": "demo"})
