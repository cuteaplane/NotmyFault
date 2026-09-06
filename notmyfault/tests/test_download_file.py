import io
import threading
from urllib.parse import urlparse

import pytest

from notmyfault.actions.download_file import action as download
from notmyfault.core.workflow import ActionCancellation, ActionCancelled


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def getheader(self, name):
        return self.headers.get(name)

    def read1(self, size):
        return self.read(size)


class Connection:
    def __init__(self, response):
        self.response = response
        self.closed = False

    def close(self):
        self.closed = True


def install_response(monkeypatch, response):
    connection = Connection(response)
    monkeypatch.setattr(download, "_open_response", lambda *args: (connection, response))
    return connection


def test_download_publishes_complete_file_and_preserves_existing_target(tmp_path, monkeypatch):
    destination = tmp_path / "report.csv"
    content = "name,total\n报告,12\n".encode()
    connection = install_response(monkeypatch, Response(content, {"Content-Length": str(len(content))}))
    params = {"url": "https://example.com/report.csv", "file_path": str(destination)}
    result = download.run_with_context({}, params, {})

    assert destination.read_bytes() == content
    assert result == {"file": str(destination), "bytes": len(content), "status": 200}
    assert connection.closed
    with pytest.raises(FileExistsError):
        download.run_with_context({}, params, {})
    assert destination.read_bytes() == content
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("response,extra", [
    (lambda: Response(b"too large"), {"max_mb": 0.000001}),
    (lambda: Response(b"short", {"Content-Length": "12"}), {}),
])
def test_failed_download_keeps_existing_file_and_removes_partial_data(tmp_path, monkeypatch, response, extra):
    destination = tmp_path / "report.txt"
    destination.write_bytes(b"original")
    connection = install_response(monkeypatch, response())

    with pytest.raises((ValueError, RuntimeError)):
        download.run_with_context({}, {
            "url": "https://example.com/report.txt", "file_path": str(destination),
            "overwrite": True, **extra,
        }, {})

    assert destination.read_bytes() == b"original"
    assert connection.closed
    assert list(tmp_path.iterdir()) == [destination]


def test_cancelled_download_never_publishes_partial_file(tmp_path, monkeypatch):
    cancelled = threading.Event()

    class CancellingResponse(Response):
        def read(self, size):
            data = super().read(size)
            cancelled.set()
            return data

    install_response(monkeypatch, CancellingResponse(b"data"))
    destination = tmp_path / "report.txt"
    context = {"runtime": {"cancellation": ActionCancellation(run_event=cancelled)}}
    with pytest.raises(ActionCancelled):
        download.run_with_context({}, {
            "url": "https://example.com/report.txt", "file_path": str(destination),
        }, context)
    assert list(tmp_path.iterdir()) == []


def test_download_revalidates_redirect_before_connecting(monkeypatch):
    seen = []
    connections = []

    def resolve(url):
        seen.append(url)
        if "127.0.0.1" in url:
            raise ValueError("URL 不能指向本机、内网或保留地址")
        return urlparse(url), ("93.184.215.14",)

    class RedirectConnection(Connection):
        def __init__(self, host, addresses, **kwargs):
            response = Response(b"", {"Location": "http://127.0.0.1/private"})
            response.status = 302
            super().__init__(response)
            connections.append(self)

        def request(self, method, target, **kwargs):
            self.target = target

        def getresponse(self):
            return self.response

    monkeypatch.setattr(download, "resolve_public_http_url", resolve)
    monkeypatch.setattr(download, "_HTTPConnection", RedirectConnection)
    with pytest.raises(ValueError, match="内网"):
        download._open_response("http://example.com/report.csv;version=2?download=1", 5, None)
    assert seen == ["http://example.com/report.csv;version=2?download=1", "http://127.0.0.1/private"]
    assert len(connections) == 1 and connections[0].closed
    assert connections[0].target == "/report.csv;version=2?download=1"


@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443)])
def test_download_connects_only_to_previously_checked_addresses(monkeypatch, scheme, port):
    calls = []
    requests = []
    checked = []
    server_names = []
    response = Response(b"content")

    class Socket:
        def sendall(self, data):
            requests.append(data)

        def close(self):
            pass

    class TLSContext:
        def wrap_socket(self, sock, *, server_hostname):
            server_names.append(server_hostname)
            return sock

    def resolve(url):
        checked.append(url)
        return urlparse(url), ("93.184.215.14",)

    def connect(address, timeout):
        calls.append((address, timeout))
        return Socket()

    monkeypatch.setattr(download, "resolve_public_http_url", resolve)
    monkeypatch.setattr(download.socket, "create_connection", connect)
    monkeypatch.setattr(download.ssl, "create_default_context", TLSContext)
    monkeypatch.setattr(download.http.client.HTTPConnection, "getresponse", lambda self: response)

    url = f"{scheme}://example.com/report.csv"
    connection, opened = download._open_response(url, 5, None)
    try:
        assert opened is response
        assert checked == [url]
        assert calls == [(("93.184.215.14", port), 5)]
        assert b"Host: example.com\r\n" in b"".join(requests)
        assert server_names == (["example.com"] if scheme == "https" else [])
    finally:
        connection.close()
