"""HTTP 请求动作，仅接受 http 和 https URL"""

import http.client
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

from notmyfault.plugin_api import network_security_api

resolve_public_http_url = network_security_api().resolve_public_http_url


_ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD")
_MAX_RESPONSE_BYTES = 1024 * 1024
_BLOCKED_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "transfer-encoding",
        "upgrade",
    }
)


def _open_pinned_socket(
    addresses: tuple[str, ...],
    port: int,
    timeout: float,
    source_address=None,
):
    last_error = None
    deadline = time.monotonic() + timeout
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("连接超过总时限")
        try:
            return socket.create_connection(
                (address, port),
                remaining,
                source_address,
            )
        except OSError as error:
            last_error = error
    if last_error is None:
        raise OSError("没有可连接的公网地址")
    raise last_error


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, addresses: tuple[str, ...], **kwargs):
        self._addresses = addresses
        super().__init__(host, **kwargs)

    def connect(self):
        self.sock = _open_pinned_socket(
            self._addresses,
            self.port,
            self.timeout,
            self.source_address,
        )
        self._active_socket = self.sock


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, addresses: tuple[str, ...], **kwargs):
        self._addresses = addresses
        super().__init__(host, **kwargs)

    def connect(self):
        raw_socket = _open_pinned_socket(
            self._addresses,
            self.port,
            self.timeout,
            self.source_address,
        )
        self._active_socket = raw_socket
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise
        self._active_socket = self.sock


def _redact_url(url: str) -> str:
    """查询参数可能包含 token 等凭据，日志只保留 URL 路径"""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    return parsed._replace(query="***").geturl()


def run_with_context(action_info, params, context):
    method = str(params.get("method", "GET")).upper()
    url = str(params.get("url", "")).strip()
    body = params.get("body", "")
    headers_raw = params.get("headers", "")
    try:
        timeout = max(1, min(float(params.get("timeout_seconds", 30)), 300))
    except (TypeError, ValueError):
        timeout = 30

    if not url:
        raise ValueError("未指定 URL")
    if method not in _ALLOWED_METHODS:
        raise ValueError(f"不支持的 HTTP 方法: {method}")
    cancellation = context.get("runtime", {}).get("cancellation")
    if cancellation:
        cancellation.raise_if_cancelled()
    deadline = time.monotonic() + timeout
    parsed, addresses = resolve_public_http_url(url)
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise RuntimeError("请求超过总时限")

    print(f"[Action:http_request] {method} {_redact_url(url)}")

    headers = {}

    if headers_raw:
        for line in str(headers_raw).strip().split("\n"):
            line = line.strip()
            if ":" in line:
                key, val = line.split(":", 1)
                key = key.strip()
                val = val.strip()
                if (
                    not key
                    or key.lower() in _BLOCKED_HEADERS
                    or key.lower().startswith("proxy-")
                    or "\r" in key
                    or "\n" in key
                    or "\r" in val
                    or "\n" in val
                ):
                    raise ValueError(f"不允许的 HTTP 请求头: {key or '(空)'}")
                headers[key] = val

    data = None
    if method in ("POST", "PUT", "PATCH") and body:
        data = str(body).encode("utf-8")
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json"

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    if parsed.scheme.lower() == "https":
        connection = _PinnedHTTPSConnection(
            parsed.hostname,
            addresses,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        connection = _PinnedHTTPConnection(
            parsed.hostname,
            addresses,
            port=port,
            timeout=timeout,
        )
    finished = threading.Event()
    def interrupt_connection():
        while not finished.wait(0.05):
            if time.monotonic() >= deadline or (cancellation and cancellation.is_cancelled()):
                sock = getattr(connection, "_active_socket", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
    watcher = threading.Thread(target=interrupt_connection, daemon=True)
    watcher.start()
    try:
        connection.request(method, target, body=data, headers=headers)
        response = connection.getresponse()
        status = response.status
        if status >= 300:
            raise RuntimeError(f"HTTP {status}: {response.reason}")
        raw_body = bytearray()
        while len(raw_body) <= _MAX_RESPONSE_BYTES:
            if cancellation:
                cancellation.raise_if_cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError("请求超过总时限")
            chunk = response.read1(min(65536, _MAX_RESPONSE_BYTES + 1 - len(raw_body)))
            if not chunk:
                break
            raw_body.extend(chunk)
        if cancellation:
            cancellation.raise_if_cancelled()
        if time.monotonic() >= deadline:
            raise TimeoutError("请求超过总时限")
        truncated = len(raw_body) > _MAX_RESPONSE_BYTES
        resp_body = raw_body[:_MAX_RESPONSE_BYTES].decode(
            "utf-8", errors="replace"
        )
        print(f"[Action:http_request] {method} {_redact_url(url)} -> {status}")
        return {
            "status": status,
            "body": resp_body,
            "truncated": truncated,
        }
    except OSError as e:
        if cancellation:
            cancellation.raise_if_cancelled()
        if time.monotonic() >= deadline:
            raise RuntimeError("请求超过总时限") from e
        raise RuntimeError("请求失败") from e
    except http.client.HTTPException as e:
        if cancellation:
            cancellation.raise_if_cancelled()
        raise RuntimeError("请求异常") from e
    finally:
        finished.set()
        watcher.join(timeout=1)
        connection.close()


def run(action_info, params):
    return run_with_context(action_info, params, {})
