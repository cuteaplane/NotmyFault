def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    text = params.get("text", "")
    if not isinstance(text, str):
        raise ValueError("文本必须是字符串")
    operation = params.get("operation", "trim")
    if operation in ("split", "join"):
        delimiter = params.get("delimiter", ",")
        if not isinstance(delimiter, str) or operation == "split" and not delimiter:
            raise ValueError("分隔符必须是文本，拆分时不能为空")
        if operation == "split":
            parts = text.split(delimiter)
        else:
            parts = params.get("parts", [])
            if not isinstance(parts, list) or any(not isinstance(item, str) for item in parts):
                raise ValueError("连接内容必须是文本数组")
            text = delimiter.join(parts)
        return {"text": text, "lines": parts, "length": len(text), "line_count": len(parts)}
    if operation == "trim":
        text = text.strip()
    elif operation == "lower":
        text = text.lower()
    elif operation == "upper":
        text = text.upper()
    elif operation == "title":
        text = text.title()
    elif operation == "casefold":
        text = text.casefold()
    elif operation == "replace":
        find = params.get("find", "")
        replacement = params.get("replacement", "")
        if not isinstance(find, str) or not find:
            raise ValueError("查找文本必须是非空字符串")
        if not isinstance(replacement, str):
            raise ValueError("替换文本必须是字符串")
        text = text.replace(find, replacement)
    elif operation == "split_lines":
        lines = text.splitlines()
        if params.get("trim_lines", False):
            lines = [line.strip() for line in lines]
        if params.get("skip_empty_lines", False):
            lines = [line for line in lines if line]
        text = "\n".join(lines)
        return {
            "text": text,
            "lines": lines,
            "length": len(text),
            "line_count": len(lines),
        }
    else:
        raise ValueError("未知的文本处理操作")
    lines = text.splitlines()
    return {
        "text": text,
        "lines": lines,
        "length": len(text),
        "line_count": len(lines),
    }
