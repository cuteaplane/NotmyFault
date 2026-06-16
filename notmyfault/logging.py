"""
引擎结构化日志模块
------------------
每个引擎 session 写独立日志文件: logs/engine-YYYYMMDD-HHMMSS.log
自动保留最近 7 个，旧文件自动删除。

格式约定：
    正常:  [TIMESTAMP] [INFO] 纯文本消息
    警告:  [TIMESTAMP] [WARN] 纯文本或简短结构化消息
    错误:  [TIMESTAMP] [ERROR] {"event": "...", ...JSON...}

用法:
    from notmyfault.logging import engine_info, engine_warn, engine_error
    engine_info("装载Trigger: 进程状态扫描器 (process_state) v1")
    engine_error("plugin_load_failed", plugin="test", type="Trigger", reason="schema 校验失败")
"""

import glob
import json
import os
import sys
import threading
from datetime import datetime
from typing import Any, Dict


# 保护多线程写 log
_log_lock = threading.Lock()
_current_log_path: str | None = None


def init_session_log(log_dir: str) -> str:
    """创建当前 session 的日志文件，清理旧文件。

    Args:
        log_dir: 日志目录路径（如 %APPDATA%/NotmyFault/logs）

    Returns:
        新创建的日志文件路径
    """
    global _current_log_path

    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"engine-{timestamp}.log"
    _current_log_path = os.path.join(log_dir, filename)

    # 清理旧日志，只保留最近 7 个
    _rotate_logs(log_dir)

    return _current_log_path


def _rotate_logs(log_dir: str, keep: int = 7) -> None:
    """只保留最近 keep 个日志文件。"""
    files = sorted(
        glob.glob(os.path.join(log_dir, "engine-*.log")),
        key=os.path.getmtime,
        reverse=True,
    )
    for old_file in files[keep:]:
        try:
            os.remove(old_file)
        except OSError:
            pass


def get_latest_log(log_dir: str) -> str | None:
    """返回最新的日志文件路径，没有则返回 None。"""
    files = sorted(
        glob.glob(os.path.join(log_dir, "engine-*.log")),
        key=os.path.getmtime,
        reverse=True,
    )
    return files[0] if files else None


def list_logs(log_dir: str) -> list[dict]:
    """列出所有日志文件（最新在前）。"""
    files = sorted(
        glob.glob(os.path.join(log_dir, "engine-*.log")),
        key=os.path.getmtime,
        reverse=True,
    )
    result = []
    for f in files:
        try:
            mtime = os.path.getmtime(f)
            size = os.path.getsize(f)
            with open(f, "r", encoding="utf-8", errors="replace") as fp:
                line_count = sum(1 for _ in fp)
        except OSError:
            line_count, size, mtime = 0, 0, 0
        result.append({
            "name": os.path.basename(f),
            "path": f,
            "size": size,
            "lines": line_count,
            "mtime": mtime,
        })
    return result


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def _emit(level: str, payload: str) -> None:
    """线程安全地写入 stdout（被引擎重定向到日志文件）。"""
    with _log_lock:
        print(f"[{level}] {payload}", file=sys.stdout, flush=True)


def engine_info(msg: str) -> None:
    """普通信息日志。"""
    _emit("INFO", msg)


def engine_warn(msg: str) -> None:
    """警告日志 — 需要关注但引擎能继续运行。"""
    _emit("WARN", msg)


def engine_error(event: str, **data: Any) -> None:
    """结构化错误日志 — JSON 格式，供 Dashboard 解析诊断。"""
    payload = json.dumps({"event": event, **data}, ensure_ascii=False, default=str)
    _emit("ERROR", payload)


# ---------------------------------------------------------------------------
# 日志解析（供 Dashboard bridge 使用）
# ---------------------------------------------------------------------------

def parse_log_line(line: str) -> Dict[str, Any] | None:
    """解析一行日志，返回结构化条目或 None。

    兼容两种格式:
        新: [2026-06-17 12:34:56] [LEVEL] payload...
        旧: [2026-06-17 12:34:56] payload...          → 视为 INFO
    """
    line = line.strip()
    if not line:
        return None

    if not line.startswith("["):
        return None

    ts_end = line.find("] ")
    if ts_end < 0:
        return None
    ts = line[1:ts_end]

    rest = line[ts_end + 2:]

    level = "INFO"
    payload = rest
    if rest.startswith("[") and "] " in rest:
        bracket_end = rest.find("] ")
        candidate_level = rest[1:bracket_end]
        if candidate_level in ("INFO", "WARN", "ERROR"):
            level = candidate_level
            payload = rest[bracket_end + 2:]

    data = None
    text = payload
    if level == "ERROR":
        try:
            data = json.loads(payload)
            event = data.get("event", "?")
            plugin = data.get("plugin", data.get("rule", ""))
            reason = data.get("reason", "")
            if plugin and reason:
                text = f"[{event}] {plugin}: {reason}"
            elif reason:
                text = f"[{event}] {reason}"
        except json.JSONDecodeError:
            pass

    return {"ts": ts, "level": level, "text": text, "data": data}


def read_log_entries(log_path: str, lines: int = 500) -> list[Dict[str, Any]]:
    """读取日志文件末尾 N 行，返回解析后的条目列表。"""
    entries: list[Dict[str, Any]] = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()
        for line in raw_lines[-lines:]:
            entry = parse_log_line(line)
            if entry is not None:
                entries.append(entry)
    except FileNotFoundError:
        pass
    return entries


def build_diagnostics(
    entries: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """从解析后的日志条目构建诊断摘要。"""

    diag: Dict[str, Any] = {
        "error_count": 0,
        "warn_count": 0,
        "plugin_errors": [],
        "rule_issues": [],
        "action_fails": 0,
        "hot_reload_errors": 0,
        "last_errors": [],
        "last_warns": [],
    }

    for entry in entries:
        level = entry.get("level", "")
        data = entry.get("data")

        if level == "ERROR":
            diag["error_count"] += 1
            diag["last_errors"].append(entry["text"])
            if data:
                event = data.get("event", "")
                if event == "plugin_load_failed":
                    diag["plugin_errors"].append({
                        "plugin": data.get("plugin", "?"),
                        "type": data.get("type", "?"),
                        "reason": data.get("reason", "?"),
                    })
                elif event == "rule_issue":
                    diag["rule_issues"].append({
                        "rule": data.get("rule", "?"),
                        "issue": data.get("issue", "?"),
                    })
                elif event == "action_failed":
                    diag["action_fails"] += 1
                elif event == "hot_reload_error":
                    diag["hot_reload_errors"] += 1

        elif level == "WARN":
            diag["warn_count"] += 1
            diag["last_warns"].append(entry["text"])

    diag["last_errors"] = diag["last_errors"][-10:]
    diag["last_warns"] = diag["last_warns"][-5:]

    return diag
