import http.client
import math
import os
import socket
import ssl
import tempfile
from pathlib import Path
from urllib.parse import urljoin

from notmyfault.plugin_api import network_security_api


resolve_public_http_url = network_security_api().resolve_public_http_url


def _connect(addresses, port, timeout):
    last_error = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout)
        except OSError as error:
            last_error = error
    raise OSError("无法连接下载服务器") from last_error


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, addresses, **kwargs):
        self.addresses = addresses
        super().__init__(host, **kwargs)

    def connect(self):
        self.sock = _connect(self.addresses, self.port, self.timeout)


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, addresses, **kwargs):
        self.addresses = addresses
        super().__init__(host, **kwargs)

    def connect(self):
        raw = _connect(self.addresses, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _open_response(url, timeout, cancellation):
    for redirect in range(6):
        if cancellation:
            cancellation.raise_if_cancelled()
        parsed, addresses = resolve_public_http_url(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection = (
            _HTTPSConnection(parsed.hostname, addresses, port=port, timeout=timeout,
                             context=ssl.create_default_context())
            if parsed.scheme == "https"
            else _HTTPConnection(parsed.hostname, addresses, port=port, timeout=timeout)
        )
        target = parsed.path or "/"
        if parsed.params:
            target += ";" + parsed.params
        if parsed.query:
            target += "?" + parsed.query
        try:
            connection.request("GET", target, headers={"Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or redirect == 5:
                    raise RuntimeError("下载重定向缺少地址或次数超过 5 次")
                url = urljoin(url, location)
                connection.close()
                continue
            if response.status != 200:
                raise RuntimeError(f"下载失败，HTTP 状态码 {response.status}")
            return connection, response
        except BaseException:
            connection.close()
            raise
    raise RuntimeError("下载重定向次数过多")


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    url = str(params.get("url", "")).strip()
    raw_path = str(params.get("file_path", "")).strip()
    if not url or not raw_path:
        raise ValueError("请指定下载地址和保存文件路径")
    timeout = float(params.get("timeout_seconds", 15))
    max_mb = float(params.get("max_mb", 100))
    if not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise ValueError("网络超时必须在 1 到 120 秒之间")
    if not math.isfinite(max_mb) or not 0 < max_mb <= 1024:
        raise ValueError("下载大小上限必须大于 0 且不超过 1024 MiB")
    limit = int(max_mb * 1024 * 1024)
    destination = Path(raw_path).expanduser().absolute()
    overwrite = bool(params.get("overwrite", False))
    if not destination.parent.is_dir():
        raise ValueError("保存目录不存在，请先创建目录")
    if destination.is_symlink() or destination.is_dir():
        raise ValueError("保存路径必须是普通文件路径")
    if destination.exists() and not overwrite:
        raise FileExistsError("目标文件已存在，请更换路径或允许覆盖")
    cancellation = context.get("runtime", {}).get("cancellation")
    connection, response = _open_response(url, timeout, cancellation)
    temporary = None
    try:
        declared = response.getheader("Content-Length")
        expected = int(declared) if declared is not None else None
        if expected is not None and (expected < 0 or expected > limit):
            raise ValueError("服务器返回的文件大小超过下载上限或无效")
        total = 0
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".nmf-download-", delete=False
        ) as output:
            temporary = Path(output.name)
            while True:
                if cancellation:
                    cancellation.raise_if_cancelled()
                chunk = response.read1(min(65536, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError("下载内容超过大小上限")
                output.write(chunk)
        if expected is not None and total != expected:
            raise RuntimeError("下载中断，文件长度与服务器声明不符")
        if cancellation:
            cancellation.raise_if_cancelled()
        if overwrite:
            os.replace(temporary, destination)
        elif os.name == "nt":
            os.rename(temporary, destination)
        else:
            # 创建硬链接时目标必须不存在，检查路径之后新增的文件也会保留。
            os.link(temporary, destination)
        return {"file": str(destination), "bytes": total, "status": response.status}
    finally:
        connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
