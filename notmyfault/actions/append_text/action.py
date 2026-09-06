"""追加文本动作：把内容写到本地文件末尾并保留已有内容
支持给每行加时间标记，编码使用 Python 文本编码名称
"""

from datetime import datetime


def run(action_info, params):
    # execution_api=context-v1 入口通过 run_with_context() 执行，run() 是旧协议误调用时的占位入口
    raise RuntimeError(
        "append_text 需要上下文执行（execution_api=context-v1），请通过引擎规则调用"
    )


def run_with_context(action_info, params, context):
    file_path = str(params.get("file_path", "") or "").strip()
    if not file_path:
        raise ValueError("未指定日志文件路径")
    text = str(params.get("text", "") or "")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("没有可写入的内容")

    encoding = str(params.get("encoding", "utf-8") or "utf-8")
    b"".decode(encoding)

    add_timestamp = bool(params.get("add_timestamp", True))
    timestamp = (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S") if add_timestamp else None
    )
    payload = []
    for line in lines:
        # 行首是方括号时视为已有时间标记，直接写入
        if timestamp and not line.lstrip().startswith("["):
            payload.append(f"[{timestamp}] {line}")
        else:
            payload.append(line)

    with open(file_path, "a", encoding=encoding, newline="") as fh:
        fh.write("\n".join(payload) + "\n")

    print(f"[Action:append_text] 已追加 {len(payload)} 行 -> {file_path}")
    return {
        "file": file_path,
        "lines": len(payload),
        "written": "\n".join(payload),
    }
