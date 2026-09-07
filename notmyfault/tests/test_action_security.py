from pathlib import Path
import io
import tarfile
from urllib.parse import urlparse

import pytest

from notmyfault.actions.create_shortcut import action as create_shortcut
from notmyfault.actions.http_request import action as http_request
from notmyfault.actions.kill_process import action as kill_process
from notmyfault.actions.launch_program import action as launch_program
from notmyfault.security import network
from notmyfault.actions.file_operation import action as file_operation


@pytest.mark.parametrize("member_type", [tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.SYMTYPE])
def test_tar_rejects_special_members_before_extracting_any_file(tmp_path, member_type):
    archive = tmp_path / "archive.tar"
    with tarfile.open(archive, "w") as output:
        normal = tarfile.TarInfo("first.txt")
        normal.size = 2
        output.addfile(normal, io.BytesIO(b"ok"))
        special = tarfile.TarInfo("special")
        special.type = member_type
        special.linkname = "first.txt"
        output.addfile(special)
    target = tmp_path / "out"
    target.mkdir()
    with pytest.raises(ValueError):
        file_operation._safe_unpack(str(archive), str(target))
    assert list(target.iterdir()) == []


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


@pytest.mark.parametrize("slow_body", [False, True])
def test_http_action_connects_to_the_validated_address(monkeypatch, slow_body):
    captured = {}
    now = [0.0]
    monkeypatch.setattr(http_request.time, "monotonic", lambda: now[0])

    class Response:
        status = 200
        reason = "OK"

        body = b"done"
        def read1(self, limit):
            if slow_body:
                now[0] = 31
            value, self.body = self.body[:limit], self.body[limit:]
            return value

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

    if slow_body:
        with pytest.raises(RuntimeError, match="总时限"):
            http_request.run({}, {"url": "http://example.test/path?q=1"})
        return
    result = http_request.run({}, {"url": "http://example.test/path?q=1"})

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
    assert calls[0][0] == ('203.0.113.10', 443)
    assert 0 < calls[0][1] <= 5
    assert calls[0][2] is None
    now = [0.0]
    monkeypatch.setattr(http_request.time, "monotonic", lambda: now[0])
    def slow_connection(address, timeout, source_address):
        now[0] += 3
        calls.append(timeout)
        raise OSError("unreachable")
    calls.clear()
    monkeypatch.setattr(http_request.socket, "create_connection", slow_connection)
    with pytest.raises(TimeoutError):
        http_request._open_pinned_socket(("203.0.113.1", "203.0.113.2", "203.0.113.3"), 443, 5)
    assert calls == [5, 2]


def test_linux_shortcut_rejects_multiline_fields():
    with pytest.raises(ValueError, match="换行"):
        create_shortcut._run_linux(
            {},
            {
                "name": "safe",
                "target_path": "/usr/bin/tool\nX-GNOME-Autostart-enabled=true",
                "location": "start_menu",
            },
        )


def test_linux_shortcut_quotes_exec_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    result = create_shortcut._run_linux(
        {},
        {
            "name": "safe",
            "target_path": "/opt/My Tool/tool",
            "arguments": "--label 'two words' --dollar '$HOME' --slash '\\part'",
            "location": "start_menu",
            "working_directory": "/tmp/work\\folder",
            "icon_path": "/tmp/icon.png",
        },
    )
    content = Path(result["shortcut_path"]).read_text(encoding="utf-8")
    assert "Path=/tmp/work\\\\folder\n" in content
    assert "Icon=/tmp/icon.png\n" in content
    assert 'Exec="/opt/My Tool/tool" "--label" "two words"' in content
    assert '"\\\\$HOME"' in content
    assert '"\\\\\\\\part"' in content


def test_launch_program_rejects_unbalanced_quotes():
    with pytest.raises(ValueError, match="引号"):
        launch_program._split_args('"unfinished')


@pytest.mark.skipif(launch_program.sys.platform != "win32", reason="仅 Windows 参数规则")
def test_launch_program_preserves_windows_path_backslashes():
    assert launch_program._split_args(
        r'--config C:\Temp\a.txt --label "two words"'
    ) == ["--config", r"C:\Temp\a.txt", "--label", "two words"]


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
