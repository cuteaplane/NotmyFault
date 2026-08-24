import hashlib
import json
import socket

import pytest

from notmyfault.host import plugin_registry
from notmyfault.host.plugin_registry import PluginRegistryError


def registry_entry(**overrides):
    entry = {
        "package_name": "com.example.demo",
        "name": "Demo",
        "version": "1.0.0",
        "download": "https://downloads.example.com/demo.nmfp",
        "sha256": "a" * 64,
        "homepage": "https://example.com/demo",
        "supported_platforms": ["windows", "linux"],
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        plugin_registry.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )


def test_validate_registry_normalizes_entries(public_dns):
    payload = {"schema_version": 1, "plugins": [registry_entry(sha256="A" * 64)]}

    result = plugin_registry.validate_plugin_registry(payload)

    assert result["plugins"][0]["sha256"] == "a" * 64
    assert result["plugins"][0]["supported_platforms"] == ["windows", "linux"]


@pytest.mark.parametrize(
    "entry",
    [
        registry_entry(package_name="Demo"),
        registry_entry(download="http://example.com/demo.nmfp"),
        registry_entry(download="https://example.com/demo.zip"),
        registry_entry(sha256="bad"),
        registry_entry(supported_platforms=["windows", "windows"]),
    ],
)
def test_validate_registry_rejects_invalid_entry(public_dns, entry):
    with pytest.raises(PluginRegistryError):
        plugin_registry.validate_plugin_registry(
            {"schema_version": 1, "plugins": [entry]}
        )


def test_remote_url_rejects_private_address(monkeypatch):
    monkeypatch.setattr(
        plugin_registry.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )

    with pytest.raises(PluginRegistryError, match="内网"):
        plugin_registry._validate_remote_url("https://localhost/registry.json", "索引")


def test_read_url_connects_to_the_validated_address(monkeypatch):
    dns_calls = []
    connections = []

    def resolve(*args, **kwargs):
        dns_calls.append(args[0])
        address = "8.8.8.8" if len(dns_calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    class Response:
        status = 200

        @staticmethod
        def getheader(_name):
            return None

        @staticmethod
        def read(_limit):
            return b"registry"

    class Connection:
        def __init__(self, hostname, address, timeout):
            connections.append((hostname, address, timeout))

        def request(self, method, target, headers):
            assert method == "GET"
            assert target == "/registry.json"
            assert headers["User-Agent"].startswith("NotmyFault-")

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(plugin_registry.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(plugin_registry, "_PinnedHTTPSConnection", Connection)

    assert plugin_registry._read_url(
        "https://registry.example.com/registry.json",
        1024,
        "索引",
    ) == b"registry"
    assert dns_calls == ["registry.example.com"]
    assert connections == [("registry.example.com", "8.8.8.8", 20)]


def test_read_url_validates_each_redirect_address(monkeypatch):
    resolved = {
        "registry.example.com": "8.8.8.8",
        "internal.example.com": "127.0.0.1",
    }

    def resolve(hostname, *args, **kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (resolved[hostname], 443),
            )
        ]

    class RedirectResponse:
        status = 302

        @staticmethod
        def getheader(name):
            return (
                "https://internal.example.com/registry.json"
                if name == "Location"
                else None
            )

        @staticmethod
        def read(_limit):
            raise AssertionError("重定向响应体不应读取")

    class Connection:
        def __init__(self, hostname, address, timeout):
            assert hostname == "registry.example.com"
            assert address == "8.8.8.8"

        @staticmethod
        def request(*args, **kwargs):
            return None

        @staticmethod
        def getresponse():
            return RedirectResponse()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(plugin_registry.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(plugin_registry, "_PinnedHTTPSConnection", Connection)

    with pytest.raises(PluginRegistryError, match="内网"):
        plugin_registry._read_url(
            "https://registry.example.com/registry.json",
            1024,
            "索引",
        )


def test_download_refetches_index_and_verifies_sha256(monkeypatch, public_dns):
    archive = b"nmfp payload"
    entry = registry_entry(sha256=hashlib.sha256(archive).hexdigest())
    responses = iter([
        json.dumps({"schema_version": 1, "plugins": [entry]}).encode(),
        archive,
    ])
    monkeypatch.setattr(
        plugin_registry, "_read_url", lambda url, max_bytes, field: next(responses)
    )

    data, selected = plugin_registry.download_registry_package(
        "https://example.com/registry.json", "com.example.demo", "1.0.0"
    )

    assert data == archive
    assert selected["download"] == entry["download"]


def test_download_rejects_sha256_mismatch(monkeypatch, public_dns):
    entry = registry_entry(sha256="0" * 64)
    responses = iter([
        json.dumps({"schema_version": 1, "plugins": [entry]}).encode(),
        b"tampered",
    ])
    monkeypatch.setattr(
        plugin_registry, "_read_url", lambda url, max_bytes, field: next(responses)
    )

    with pytest.raises(PluginRegistryError, match="SHA-256"):
        plugin_registry.download_registry_package(
            "https://example.com/registry.json", "com.example.demo", "1.0.0"
        )
