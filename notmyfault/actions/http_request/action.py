import urllib.request
import urllib.error
import json as json_lib


def run(action_info, params):
    method = params.get("method", "GET")
    url = params.get("url", "").strip()
    body = params.get("body", "")
    headers_raw = params.get("headers", "")
    try:
        timeout = max(1, min(float(params.get("timeout_seconds", 30)), 300))
    except (TypeError, ValueError):
        timeout = 30

    if not url:
        print("[Action:http_request] 未指定 URL")
        return

    print(f"[Action:http_request] {method} {url}")

    try:
        req = urllib.request.Request(url, method=method)

        if headers_raw:
            for line in headers_raw.strip().split("\n"):
                line = line.strip()
                if ":" in line:
                    key, val = line.split(":", 1)
                    req.add_header(key.strip(), val.strip())

        data = None
        if method in ("POST", "PUT") and body:
            data = body.encode("utf-8")
            if not any(k.lower() == "content-type" for k in req.headers):
                req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            status = resp.status
            resp_body = resp.read().decode("utf-8", errors="replace")[:500]
            print(f"[Action:http_request] {method} {url} -> {status}")
            if resp_body:
                print(f"[Action:http_request] 响应: {resp_body[:200]}")

    except urllib.error.HTTPError as e:
        print(f"[Action:http_request] HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        print(f"[Action:http_request] 请求失败: {e.reason}")
    except Exception as e:
        print(f"[Action:http_request] 异常: {e}")
