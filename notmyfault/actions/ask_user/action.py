"""向用户弹出桌面消息框或输入框
走系统原生对话框（Windows MessageBox / Linux zenity），不经过 Dashboard
"""

from __future__ import annotations

from typing import Any

from notmyfault.platform.platform_support import ask_text_dialog, show_message_dialog

_MODES = frozenset({"message", "input"})


def run(action_info, params):
    mode = str(params.get("mode", "message") or "message").strip().lower()
    if mode not in _MODES:
        raise ValueError(f"未知模式: {mode!r}（可选: message/input）")

    title = params.get("title", "NotmyFault")
    message = params.get("message", "")
    if not isinstance(title, str):
        title = str(title)
    if not isinstance(message, str):
        message = str(message)
    title = title[:120]
    message = message[:2000]

    result: dict[str, Any] = {"mode": mode, "title": title}

    if mode == "message":
        shown = show_message_dialog(title, message)
        result["ok"] = bool(shown)
        result["confirmed"] = bool(shown)
        result["text"] = ""
        result["error"] = (
            "" if shown else "无法显示消息框（当前环境没有可用的桌面对话框）"
        )
        print(f"[Action:ask_user] 消息框: ok={result['ok']}")
        return result

    default = params.get("default", "")
    if not isinstance(default, str):
        default = str(default)
    default = default[:500]
    answer = ask_text_dialog(title, message, default)
    available = bool(answer.get("available"))
    confirmed = bool(answer.get("confirmed"))
    text = str(answer.get("text") or "") if confirmed else ""
    result["ok"] = available
    result["confirmed"] = confirmed
    result["text"] = text
    result["error"] = (
        "" if available else "无法显示输入框（当前环境没有可用的桌面对话框）"
    )
    print(
        f"[Action:ask_user] 输入框: available={available} confirmed={confirmed} "
        f"text_len={len(text)}"
    )
    return result
