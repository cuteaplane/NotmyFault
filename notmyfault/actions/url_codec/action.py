import json
import math
from urllib.parse import parse_qs, quote, quote_plus, unquote, unquote_plus, urlencode, urlsplit, urlunsplit


def _query_value(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("查询参数数字必须是有限值")
        return str(value)
    raise ValueError("查询参数仅支持文本、数字、布尔值、null 或这些值的数组")


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    operation = params.get("operation", "encode")
    value = params.get("value", "")
    result = {"text": "", "scheme": "", "hostname": "", "port": None, "path": "", "query": {}, "fragment": ""}
    if operation == "compose":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError("组成 URL 时输入必须是对象或其 JSON 文本") from None
        if not isinstance(value, dict):
            raise ValueError("组成 URL 时输入必须是对象")
        scheme, host = value.get("scheme", ""), value.get("hostname", "")
        path, fragment = value.get("path", ""), value.get("fragment", "")
        if any(not isinstance(item, str) for item in (scheme, host, path, fragment)):
            raise ValueError("协议、主机、路径和片段必须是文本")
        port = value.get("port")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535):
            raise ValueError("端口必须是 0 到 65535 的整数或 null")
        if any(character in host for character in '/?#@') or any(character.isspace() for character in host):
            raise ValueError("主机名包含无效字符")
        query = value.get("query", {})
        query_text = run_with_context(action_info, {"operation": "query_encode", "value": query}, context)["text"]
        authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if port is not None:
            if not authority:
                raise ValueError("指定端口时必须提供主机名")
            authority += f":{port}"
        assembled = urlunsplit((scheme, authority, path, query_text, fragment))
        return run_with_context(action_info, {"operation": "parse", "value": assembled}, context)
    if operation == "query_encode":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON 格式错误，第 {exc.lineno} 行第 {exc.colno} 列") from None
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise ValueError("生成查询参数时输入必须是对象或其 JSON 文本")
        query = {
            key: [_query_value(item) for item in item_value] if isinstance(item_value, list) else _query_value(item_value)
            for key, item_value in value.items()
        }
        result["text"] = urlencode(query, doseq=True)
        result["query"] = parse_qs(result["text"], keep_blank_values=True)
        return result
    if not isinstance(value, str):
        raise ValueError("URL 编解码和解析的输入必须是字符串")
    form_mode = bool(params.get("form_mode", False))
    if operation == "encode":
        safe = params.get("safe", "")
        if not isinstance(safe, str):
            raise ValueError("保留字符必须是字符串")
        result["text"] = (quote_plus if form_mode else quote)(value, safe=safe)
    elif operation == "decode":
        try:
            result["text"] = (unquote_plus if form_mode else unquote)(value, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ValueError("URL 编码内容不是有效的 UTF-8 文本") from None
    elif operation == "parse":
        try:
            parsed = urlsplit(value)
            result.update({
                "text": value,
                "scheme": parsed.scheme,
                "hostname": parsed.hostname or "",
                "port": parsed.port,
                "path": parsed.path,
                "query": parse_qs(parsed.query, keep_blank_values=True, encoding="utf-8", errors="strict"),
                "fragment": parsed.fragment,
            })
        except (ValueError, UnicodeDecodeError):
            raise ValueError("URL 格式、端口或查询参数编码无效") from None
    else:
        raise ValueError("未知的 URL 操作")
    return result
