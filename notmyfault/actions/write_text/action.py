from pathlib import Path


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    file_path = str(params.get("file_path", "") or "").strip()
    if not file_path:
        raise ValueError("未指定文件路径")
    text = params.get("text", "")
    if not isinstance(text, str):
        raise ValueError("写入内容必须是文本")
    encoding = str(params.get("encoding", "utf-8") or "utf-8")
    try:
        data = text.encode(encoding)
    except LookupError:
        raise ValueError(f"未知或不适用于文本的编码: {encoding}") from None
    path = Path(file_path).expanduser().absolute()
    if params.get("create_parents", False):
        path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if params.get("overwrite", False) else "xb"
    with path.open(mode) as target:
        target.write(data)
    return {
        "file": str(path),
        "bytes": len(data),
        "characters": len(text),
    }
