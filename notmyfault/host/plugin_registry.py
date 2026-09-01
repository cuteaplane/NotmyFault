import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
from typing import Any
from urllib import parse


REGISTRY_MAX_BYTES = 1024 * 1024
PACKAGE_MAX_BYTES = 64 * 1024 * 1024
_PACKAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ALLOWED_PLATFORMS = {"windows", "linux", "macos"}
_ENTRY_FIELDS = {
    "package_name",
    "name",
    "version",
    "download",
    "sha256",
    "homepage",
    "supported_platforms",
}


class PluginRegistryError(ValueError):
    pass


class PluginRegistryClient:
    def load(self, url: str) -> dict[str, Any]:
        return load_plugin_registry(url)

    def download(
        self,
        url: str,
        package_name: str,
        version: str,
    ) -> tuple[bytes, dict[str, Any]]:
        return download_registry_package(url, package_name, version)


def _resolve_remote_url(
    value: Any,
    field: str,
) -> tuple[str, parse.ParseResult, tuple[str, ...]]:
    if not isinstance(value, str) or not value.strip():
        raise PluginRegistryError(f"{field} 必须是 HTTPS 地址")
    url = value.strip()
    parsed = parse.urlparse(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise PluginRegistryError(f"{field} 的端口无效") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise PluginRegistryError(f"{field} 必须是无凭据、无片段的 HTTPS 地址")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as error:
        raise PluginRegistryError(f"{field} 的主机无法解析") from error
    if not addresses:
        raise PluginRegistryError(f"{field} 的主机无法解析")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as error:
            raise PluginRegistryError(f"{field} 的主机地址无效") from error
        if not parsed_address.is_global:
            raise PluginRegistryError(f"{field} 不能指向本机或内网地址")
    return url, parsed, tuple(sorted(addresses))


def _validate_remote_url(value: Any, field: str) -> str:
    return _resolve_remote_url(value, field)[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, timeout: float) -> None:
        super().__init__(
            hostname,
            port=443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self.host,
        )


def _request_target(parsed: parse.ParseResult) -> str:
    path = parsed.path or "/"
    if parsed.params:
        path += ";" + parsed.params
    if parsed.query:
        path += "?" + parsed.query
    return path


def _read_from_address(
    parsed: parse.ParseResult,
    address: str,
    max_bytes: int,
    field: str,
) -> tuple[int, str | None, bytes]:
    connection = _PinnedHTTPSConnection(parsed.hostname or "", address, timeout=20)
    try:
        connection.request(
            "GET",
            _request_target(parsed),
            headers={
                "Accept": "application/json, application/octet-stream",
                "User-Agent": "NotmyFault-plugin-registry/1",
            },
        )
        response = connection.getresponse()
        location = response.getheader("Location")
        if response.status in (301, 302, 303, 307, 308):
            return response.status, location, b""
        length = response.getheader("Content-Length")
        if length:
            try:
                declared_length = int(length)
            except ValueError:
                declared_length = None
            if declared_length is not None and declared_length > max_bytes:
                raise PluginRegistryError(f"{field} 超过大小限制")
        data = response.read(max_bytes + 1)
        return response.status, location, data
    finally:
        connection.close()


def _read_url(url: str, max_bytes: int, field: str) -> bytes:
    current_url = url
    for redirect_count in range(6):
        safe_url, parsed, addresses = _resolve_remote_url(current_url, field)
        last_error: Exception | None = None
        for address in addresses:
            try:
                status, location, data = _read_from_address(
                    parsed,
                    address,
                    max_bytes,
                    field,
                )
                break
            except PluginRegistryError:
                raise
            except (http.client.HTTPException, TimeoutError, OSError) as error:
                last_error = error
        else:
            raise PluginRegistryError(f"读取{field}失败") from last_error

        if status in (301, 302, 303, 307, 308):
            if redirect_count == 5 or not location:
                raise PluginRegistryError(f"读取{field}失败")
            current_url = parse.urljoin(safe_url, location)
            continue
        if status < 200 or status >= 300:
            raise PluginRegistryError(f"读取{field}失败")
        if len(data) > max_bytes:
            raise PluginRegistryError(f"{field} 超过大小限制")
        return data
    raise PluginRegistryError(f"读取{field}失败")


def validate_plugin_registry(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise PluginRegistryError("插件索引根节点必须是对象")
    if payload.get("schema_version") != 1:
        raise PluginRegistryError("插件索引 schema_version 必须是 1")
    plugins = payload.get("plugins")
    if not isinstance(plugins, list):
        raise PluginRegistryError("插件索引 plugins 必须是数组")

    normalized = []
    identities = set()
    for index, entry in enumerate(plugins):
        prefix = f"plugins[{index}]"
        if not isinstance(entry, dict):
            raise PluginRegistryError(f"{prefix} 必须是对象")
        missing = _ENTRY_FIELDS - set(entry)
        if missing:
            raise PluginRegistryError(
                f"{prefix} 缺少字段: {', '.join(sorted(missing))}"
            )
        package_name = entry.get("package_name")
        if not isinstance(package_name, str) or not _PACKAGE_NAME_RE.fullmatch(package_name):
            raise PluginRegistryError(f"{prefix}.package_name 格式无效")
        name = entry.get("name")
        version = entry.get("version")
        homepage = entry.get("homepage")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise PluginRegistryError(f"{prefix}.name 格式无效")
        if not isinstance(version, str) or not version.strip() or len(version) > 64:
            raise PluginRegistryError(f"{prefix}.version 格式无效")
        if not isinstance(homepage, str) or not homepage.strip():
            raise PluginRegistryError(f"{prefix}.homepage 格式无效")
        homepage_url = _validate_remote_url(homepage, f"{prefix}.homepage")
        download_url = _validate_remote_url(entry.get("download"), f"{prefix}.download")
        if not parse.urlparse(download_url).path.lower().endswith(".nmfp"):
            raise PluginRegistryError(f"{prefix}.download 必须指向 .nmfp 文件")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise PluginRegistryError(f"{prefix}.sha256 必须是 64 位十六进制字符串")
        platforms = entry.get("supported_platforms")
        if (
            not isinstance(platforms, list)
            or not platforms
            or any(item not in _ALLOWED_PLATFORMS for item in platforms)
            or len(platforms) != len(set(platforms))
        ):
            raise PluginRegistryError(f"{prefix}.supported_platforms 格式无效")
        identity = (package_name, version)
        if identity in identities:
            raise PluginRegistryError(f"{prefix} 的包名和版本重复")
        identities.add(identity)
        normalized.append({
            "package_name": package_name,
            "name": name.strip(),
            "version": version.strip(),
            "download": download_url,
            "sha256": digest.lower(),
            "homepage": homepage_url,
            "supported_platforms": list(platforms),
        })
    return {"schema_version": 1, "plugins": normalized}


def load_plugin_registry(url: str) -> dict:
    raw = _read_url(url, REGISTRY_MAX_BYTES, "插件索引")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginRegistryError("插件索引不是有效的 UTF-8 JSON") from error
    return validate_plugin_registry(payload)


def download_registry_package(
    registry_url: str,
    package_name: str,
    version: str,
) -> tuple[bytes, dict]:
    registry = load_plugin_registry(registry_url)
    entry = next(
        (
            item
            for item in registry["plugins"]
            if item["package_name"] == package_name and item["version"] == version
        ),
        None,
    )
    if entry is None:
        raise PluginRegistryError("索引中找不到指定插件版本，请刷新索引")
    archive = _read_url(entry["download"], PACKAGE_MAX_BYTES, "插件包")
    actual = hashlib.sha256(archive).hexdigest()
    if actual != entry["sha256"]:
        raise PluginRegistryError("插件包 SHA-256 与索引不一致")
    return archive, entry
