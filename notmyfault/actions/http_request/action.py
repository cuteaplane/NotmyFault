"""HTTP 请求动作，仅接受 http 和 https URL"""

import urllib.request
import urllib.error
from urllib.parse import urlparse

_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD")
_MAX_RESPONSE_BYTES = 1024 * 1024


def _redact_url(url: str) -> str:
    """查询参数可能包含 token 等凭据，日志只保留 URL 路径"""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    return parsed._replace(query="***").geturl()


def run(action_info, params):
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
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"仅允许 http/https URL，收到: {scheme or '(空)'}://"
        )

    print(f"[Action:http_request] {method} {_redact_url(url)}")

    req = urllib.request.Request(url, method=method)

    if headers_raw:
        for line in str(headers_raw).strip().split("\n"):
            line = line.strip()
            if ":" in line:
                key, val = line.split(":", 1)
                req.add_header(key.strip(), val.strip())

    data = None
    if method in ("POST", "PUT", "PATCH") and body:
        data = str(body).encode("utf-8")
        if not any(k.lower() == "content-type" for k in req.headers):
            req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            status = resp.status
            resp_body = resp.read(_MAX_RESPONSE_BYTES + 1).decode(
                "utf-8", errors="replace"
            )
            print(f"[Action:http_request] {method} {_redact_url(url)} -> {status}")
            truncated = len(resp_body) > _MAX_RESPONSE_BYTES
            if resp_body:
                print(f"[Action:http_request] 响应: {resp_body[:200]}")
            return {
                "status": status,
                "body": resp_body[:_MAX_RESPONSE_BYTES],
                "truncated": truncated,
            }
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"请求失败: {e.reason}") from e
    except (ValueError, OSError) as e:
        raise RuntimeError(f"请求异常: {e}") from e
