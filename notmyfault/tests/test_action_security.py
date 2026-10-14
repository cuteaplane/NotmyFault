from pathlib import Path
import ast
import io
import os
import tarfile
import zipfile
from types import SimpleNamespace
from threading import Event
from urllib.parse import urlparse

import pytest

from notmyfault.actions.create_shortcut import action as create_shortcut
from notmyfault.actions.http_request import action as http_request
from notmyfault.actions.download_file import action as download_file
from notmyfault.core.workflow import ActionCancellation, ActionCancelled
from notmyfault.actions.kill_process import action as kill_process
from notmyfault.actions.launch_program import action as launch_program
from notmyfault.security import network
from notmyfault.actions.file_operation import action as file_operation
from notmyfault.actions.archive import action as archive_action


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
    with pytest.raises(ValueError, match="拒绝解压"):
        file_operation.run_with_context({}, {"operation": "extract", "source": str(archive), "destination": str(target)}, {})
    assert list(target.iterdir()) == []


def test_builtin_plugins_never_block_on_subprocess_without_timeout():
    """插件里的阻塞式外部调用必须带 timeout，否则动作线程和关机收尾都会卡住"""
    package = Path(__file__).resolve().parents[1]
    blocking = {"run", "check_output", "check_call", "call"}
    offenders = []
    for root in ("actions", "triggers"):
        for path in sorted((package / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"
                    and func.attr in blocking
                ):
                    continue
                if not any(keyword.arg == "timeout" for keyword in node.keywords):
                    offenders.append(f"{path.parent.name}:{node.lineno}")
    assert offenders == [], f"这些调用没有 timeout: {offenders}"


def test_zip_rejects_archives_over_entry_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(file_operation, "_MAX_ENTRIES", 2)
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for index in range(3):
            output.writestr(f"{index}.txt", "x")
    target = tmp_path / "out"
    target.mkdir()

    with pytest.raises(ValueError, match="条目过多"):
        file_operation.run_with_context(
            {}, {"operation": "extract", "source": str(archive), "destination": str(target)}
        , {})
    assert list(target.iterdir()) == []


def test_zip_rejects_archives_over_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(file_operation, "_MAX_TOTAL_BYTES", 4)
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("big.txt", "0123456789")
    target = tmp_path / "out"
    target.mkdir()

    with pytest.raises(ValueError, match="大小超过上限"):
        file_operation.run_with_context(
            {}, {"operation": "extract", "source": str(archive), "destination": str(target)}
        , {})
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("path_kind", [
    "parent", "absolute", "link", "windows_alias", "dot_alias", "removed_parent", "empty",
])
def test_zip_checks_extracted_paths_before_writing_members(tmp_path, path_kind):
    archive = tmp_path / "unsafe.zip"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    link = None
    if path_kind == "parent":
        member = "../outside/escape.txt"
    elif path_kind == "absolute":
        member = str(outside / "escape.txt")
    elif path_kind == "empty":
        member = "."
    else:
        raw_directory = {
            "link": "linked", "windows_alias": "linked?", "dot_alias": "linked.",
            "removed_parent": "safe/../linked",
        }[path_kind]
        member = f"{raw_directory}/escape.txt"
        directory = "safe/linked" if path_kind == "removed_parent" else raw_directory
        if os.name == "nt":
            directory = directory.replace("?", "_").rstrip(".")
        link = target / directory
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        else:
            link.symlink_to(outside, target_is_directory=True)
    try:
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("first.txt", "normal")
            output.writestr(member, "escaped")
        with pytest.raises(ValueError, match="非法|越界"):
            file_operation.run_with_context({}, {
                "operation": "extract", "source": str(archive), "destination": str(target),
            }, {})
        assert not (target / "first.txt").exists()
        assert list(outside.iterdir()) == []
    finally:
        if link is not None:
            link.rmdir() if os.name == "nt" else link.unlink()


@pytest.mark.parametrize("operation", ["copy", "move", "compress"])
@pytest.mark.parametrize("root_source", [False, True])
def test_file_operations_reject_destinations_inside_root_and_regular_sources(
    tmp_path, monkeypatch, operation, root_source,
):
    source = Path(tmp_path.anchor) if root_source else tmp_path / "source"
    if not root_source:
        source.mkdir()
    destination = tmp_path / "child" if root_source else source / "child"

    def forbidden_operation(*args, **kwargs):
        pytest.fail("目标在源路径内时仍开始文件操作")

    for name in ("copytree", "move", "make_archive"):
        monkeypatch.setattr(file_operation.shutil, name, forbidden_operation)
    with pytest.raises(ValueError, match="内部"):
        file_operation.run_with_context({}, {
            "operation": operation, "source": str(source), "destination": str(destination),
            "overwrite": True,
        }, {})
    assert not destination.exists()


@pytest.mark.parametrize("format", ["zip", "tar", "archive"])
def test_archive_checks_accept_members_under_filesystem_roots(tmp_path, monkeypatch, format):
    root = tmp_path.anchor
    extracted = []
    if format == "archive":
        assert archive_action._member_destination("child.txt", root) == os.path.join(root, "child.txt")
        return
    archive = tmp_path / f"archive.{format}"
    if format == "zip":
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("child.txt", "content")
        monkeypatch.setattr(zipfile.ZipFile, "extractall", lambda self, path: extracted.append(path))
    else:
        with tarfile.open(archive, "w") as output:
            member = tarfile.TarInfo("child.txt")
            member.size = 7
            output.addfile(member, io.BytesIO(b"content"))
        monkeypatch.setattr(tarfile.TarFile, "extractall", lambda self, path, **kwargs: extracted.append(path))
    file_operation._safe_unpack(str(archive), root)
    assert extracted == [root]


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
        http_request.run_with_context({}, {"url": "http://127.0.0.1/admin"}, {})
    assert opened == []


def test_http_action_rejects_transport_headers(monkeypatch):
    monkeypatch.setattr(
        http_request,
        "resolve_public_http_url",
        lambda url: (urlparse(url), ("203.0.113.10",)),
    )
    with pytest.raises(ValueError, match="Host"):
        http_request.run_with_context(
            {},
            {"url": "https://example.com", "headers": "Host: localhost"},
        {})


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("method,status,headers,payload,expected,outcome", [
    ("GET", 200, b"Content-Length: 4\r\n", b"done", "done", None),
    ("GET", 200, b"Content-Length: 12\r\n", b"short", None, "正文长度"),
    ("GET", 200, b"Content-Length: 12\r\n", b"12345678", None, "正文长度"),
    ("GET", 200, b"Content-Length: 1\r\n", b"", None, "正文长度"),
    ("GET", 200, b"Content-Length: 0\r\n", b"", "", None),
    ("GET", 200, b"Transfer-Encoding: chunked\r\n", b"4\r\ndone\r\n0\r\n\r\n", "done", None),
    ("GET", 200, b"Transfer-Encoding: chunked\r\n", b"4\r\ndo", None, "请求异常"),
    ("GET", 200, b"Transfer-Encoding: chunked\r\n", b"4\r\ndone\r\n", None, "请求异常"),
    ("GET", 200, b"", b"done", "done", None),
    ("HEAD", 200, b"Content-Length: 12\r\n", b"", "", None),
    ("GET", 204, b"Content-Length: 12\r\n", b"", "", None),
    ("GET", 200, b"Content-Length: 11\r\n", b"01234567890", "01234567", "truncated"),
    ("GET", 200, b"Content-Length: 4\r\n", b"done", None, "总时限"),
    ("GET", 200, b"Content-Length: 4\r\n", b"done", None, "已取消"),
    ("GET", 200, b"Content-Length: 12\r\n", b"", None, "已取消"),
    ("GET", 200, b"Content-Length: 12\r\n", b"", None, "总时限"),
])
def test_http_action_preserves_url_and_validates_response(
    monkeypatch, scheme, method, status, headers, payload, expected, outcome,
):
    captured = {}
    now = [0.0]
    cancelled = Event()
    context = {"runtime": {"cancellation": ActionCancellation(run_event=cancelled)}}
    monkeypatch.setattr(http_request.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(http_request, "_MAX_RESPONSE_BYTES", 8)
    wire = f"HTTP/1.1 {status} OK\r\n".encode() + headers + b"\r\n" + payload
    response = http_request.http.client.HTTPResponse(
        SimpleNamespace(makefile=lambda *args: io.BytesIO(wire)), method=method,
    )
    response.begin()
    read1 = response.read1

    def read_response(limit):
        value = read1(limit)
        if outcome == "总时限":
            now[0] = 31
        elif outcome == "已取消":
            cancelled.set()
        return value

    response.read1 = read_response

    class Connection:
        def __init__(self, host, addresses, **kwargs):
            captured["host"] = host
            captured["addresses"] = addresses
            captured["kwargs"] = kwargs

        def request(self, method, target, body=None, headers=None):
            captured["request"] = (method, target, body, headers)

        @staticmethod
        def getresponse():
            return response

        @staticmethod
        def close():
            captured["closed"] = True
            response.close()

    monkeypatch.setattr(
        http_request,
        "resolve_public_http_url",
        lambda url: (urlparse(url), ("203.0.113.10",)),
    )
    monkeypatch.setattr(http_request, "_PinnedHTTPConnection", Connection)
    monkeypatch.setattr(http_request, "_PinnedHTTPSConnection", Connection)

    params = {"url": f"{scheme}://example.test/path;version=2?q=1#fragment", "method": method}
    if outcome not in (None, "truncated"):
        expected_error = ActionCancelled if outcome == "已取消" else RuntimeError
        with pytest.raises(expected_error, match=outcome):
            http_request.run_with_context({}, params, context)
    else:
        result = http_request.run_with_context({}, params, context)
        assert result == {"status": status, "body": expected, "truncated": outcome == "truncated"}
    assert captured["host"] == "example.test"
    assert captured["addresses"] == ("203.0.113.10",)
    assert captured["request"][:2] == (method, "/path;version=2?q=1")
    assert captured["closed"]


@pytest.mark.parametrize("module", [http_request, download_file], ids=["http_request", "download_file"])
@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
def test_https_actions_interrupt_blocked_handshake(monkeypatch, module, interruption):
    cancelled = Event()
    interrupted = Event()
    clock = [0.0]

    class RawSocket:
        detached = False
        closed = False

        def shutdown(self, how):
            if self.detached:
                raise OSError("socket 已转交 TLS")
            interrupted.set()

        def close(self):
            self.closed = True

    class TLSSocket:
        closed = False

        def do_handshake(self):
            if interruption == "cancel":
                cancelled.set()
            else:
                clock[0] = 31
            if not interrupted.wait(0.5):
                pytest.fail("取消线程未中断 TLS 握手")
            raise OSError("TLS 握手已中断")

        def shutdown(self, how):
            interrupted.set()

        def sendall(self, data):
            pytest.fail("中断握手后仍发送 HTTP 请求")

        def close(self):
            self.closed = True

    raw = RawSocket()
    tls = TLSSocket()

    class TLSContext:
        def wrap_socket(self, sock, *, server_hostname, do_handshake_on_connect=True):
            assert sock is raw and server_hostname == "example.test"
            raw.detached = True
            if do_handshake_on_connect:
                try:
                    tls.do_handshake()
                except BaseException:
                    tls.close()
                    raise
            return tls

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    connector = "_open_pinned_socket" if module is http_request else "_connect"
    monkeypatch.setattr(module, connector, lambda *args: raw)
    monkeypatch.setattr(module.ssl, "create_default_context", TLSContext)
    monkeypatch.setattr(module, "resolve_public_http_url", lambda url: (urlparse(url), ("203.0.113.10",)))
    params = {"url": "https://example.test/file", "timeout_seconds": 30}
    if module is download_file:
        destination = SimpleNamespace(
            parent=SimpleNamespace(is_dir=lambda: True),
            is_symlink=lambda: False, is_dir=lambda: False, exists=lambda: True,
        )
        path = SimpleNamespace(expanduser=lambda: SimpleNamespace(absolute=lambda: destination))
        monkeypatch.setattr(module, "Path", lambda value: path)
        monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", lambda **kwargs: pytest.fail("中断握手后仍创建下载文件"))
        params.update(file_path="existing.txt", overwrite=True)
    context = {"runtime": {"cancellation": ActionCancellation(run_event=cancelled)}}
    expected = ActionCancelled if interruption == "cancel" else RuntimeError
    with pytest.raises(expected, match="已取消" if interruption == "cancel" else "总时限"):
        module.run_with_context({}, params, context)
    assert interrupted.is_set()
    assert raw.closed and tls.closed


@pytest.mark.parametrize("module", [http_request, download_file], ids=["http_request", "download_file"])
def test_network_actions_stop_after_dns_cancellation(tmp_path, monkeypatch, module):
    cancelled = Event()
    destination = tmp_path / "download.txt"
    destination.write_bytes(b"original")

    def resolve(url):
        cancelled.set()
        return urlparse(url), ("203.0.113.10",)

    def open_connection(*args, **kwargs):
        pytest.fail("收到取消信号后仍开始连接")

    monkeypatch.setattr(module, "resolve_public_http_url", resolve)
    connection_name = "_PinnedHTTPConnection" if module is http_request else "_HTTPConnection"
    monkeypatch.setattr(module, connection_name, open_connection)
    context = {"runtime": {"cancellation": ActionCancellation(run_event=cancelled)}}
    with pytest.raises(ActionCancelled):
        module.run_with_context({}, {
            "url": "http://example.test/download", "method": "POST", "body": "data",
            "file_path": str(destination), "overwrite": True,
        }, context)
    assert destination.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [destination]


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


def test_linux_shortcut_rejects_multiline_fields(monkeypatch):
    monkeypatch.setattr(create_shortcut.sys, "platform", "linux")
    with pytest.raises(ValueError, match="换行"):
        create_shortcut.run_with_context(
            {},
            {
                "name": "safe",
                "target_path": "/usr/bin/tool\nX-GNOME-Autostart-enabled=true",
                "location": "start_menu",
            },
        {})


@pytest.mark.parametrize("prefix,escaped_prefix", [("", ""), (" ", r"\s"), ("\t", r"\t")])
def test_linux_shortcut_quotes_exec_tokens(tmp_path, monkeypatch, prefix, escaped_prefix):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(create_shortcut.sys, "platform", "linux")
    result = create_shortcut.run_with_context(
        {},
        {
            "name": "safe",
            "target_path": " ./My Tool/tool\u3000",
            "arguments": "--label 'two words' --dollar '$HOME' --slash '\\part' --tab 'a\tb'",
            "location": "start_menu",
            "working_directory": prefix + "/tmp/work\\folder ",
            "icon_path": prefix + "/tmp/icon.png ",
        },
    {})
    content = Path(result["shortcut_path"]).read_text(encoding="utf-8")
    assert f"Path={escaped_prefix}/tmp/work\\\\folder\\s\n" in content
    assert f"Icon={escaped_prefix}/tmp/icon.png\\s\n" in content
    assert 'Exec=" ./My Tool/tool\u3000" "--label" "two words"' in content
    assert '"\\\\$HOME"' in content
    assert '"\\\\\\\\part"' in content
    assert r'"a\tb"' in content


def test_launch_program_rejects_unbalanced_quotes():
    with pytest.raises(ValueError, match="引号"):
        launch_program.run_with_context({}, {"path": "tool", "args": '"unfinished'}, {})


@pytest.mark.parametrize("platform", [
    "linux",
    pytest.param("win32", marks=pytest.mark.skipif(os.name != "nt", reason="Windows 参数解析需要原生接口")),
])
@pytest.mark.parametrize("path,directory", [
    ("tool", ""), (" tool", " work"), ("./tool ", "./work "), (" tools/tool", " work/space "),
])
def test_launch_program_preserves_paths_and_platform_arguments(monkeypatch, platform, path, directory):
    started = []
    monkeypatch.setattr(launch_program.sys, "platform", platform)
    monkeypatch.setattr(launch_program.subprocess, "Popen", lambda args, **kwargs: started.append((args, kwargs)))
    if platform == "win32":
        arguments = r'--config C:\Temp\a.txt --label "two words" --empty ""'
        expected = [path, "--config", r"C:\Temp\a.txt", "--label", "two words", "--empty", ""]
    else:
        arguments = '--config "/tmp/a b.txt" --label "two words" --empty ""'
        expected = [path, "--config", "/tmp/a b.txt", "--label", "two words", "--empty", ""]
    launch_program.run_with_context({}, {"path": path, "args": arguments, "working_directory": directory}, {})
    assert len(started) == 1
    assert started[0][0] == expected
    assert started[0][1]["cwd"] == (directory or None)


def test_kill_process_rejects_critical_windows_process(monkeypatch):
    monkeypatch.setattr(kill_process.os, "name", "nt")
    with pytest.raises(ValueError, match="关键系统进程"):
        kill_process.run_with_context({}, {"process_name": "lsass"}, {})


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
        kill_process.run_with_context({}, {"process_name": "demo"}, {})
