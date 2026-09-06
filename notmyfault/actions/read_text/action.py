from pathlib import Path


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    file_path = str(params.get("file_path", "") or "").strip()
    if not file_path:
        raise ValueError("未指定文件路径")
    encoding = str(params.get("encoding", "utf-8-sig") or "utf-8-sig")
    max_bytes = int(params.get("max_bytes", 1048576))
    if max_bytes < 1 or max_bytes > 104857600:
        raise ValueError("读取字节上限必须在 1 到 104857600 之间")
    path = Path(file_path).expanduser().absolute()
    with path.open("rb") as source:
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"文件超过读取字节上限: {max_bytes}")
    try:
        text = data.decode(encoding)
    except LookupError:
        raise ValueError(f"未知或不适用于文本的编码: {encoding}") from None
    return {
        "file": str(path),
        "text": text,
        "bytes": len(data),
        "characters": len(text),
    }
