"""
引擎结构化日志模块
------------------
每个引擎 session 写独立日志文件: logs/engine-YYYYMMDD-HHMMSS-ffffff-PID.log
自动保留最近 7 个，旧文件自动删除

格式约定：
    正常:  [TIMESTAMP] [INFO] 纯文本消息
    警告:  [TIMESTAMP] [WARN] 纯文本或简短结构化消息
    错误:  [TIMESTAMP] [ERROR] {"event": "...", ...JSON...}

用法:
    from notmyfault.core.logging import engine_info, engine_warn, engine_error
    engine_info("装载Trigger: 进程状态扫描器 (process_state) v1")
    engine_error("plugin_load_failed", plugin="test", type="Trigger", reason="schema 校验失败")
"""

import glob
import json
import os
import re
import sys
import threading
from collections import deque
from datetime import datetime
from typing import Any, Dict


# 多线程写日志时共用同一把锁
_log_lock = threading.Lock()
_current_log_path: str | None = None


def init_session_log(log_dir: str) -> str:
    """创建当前会话日志并清理旧文件，返回新文件路径"""
    global _current_log_path

    os.makedirs(log_dir, exist_ok=True)

    with _log_lock:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        base = f"engine-{timestamp}-{os.getpid()}"
        for suffix in range(1000):
            filename = f"{base}{f'-{suffix}' if suffix else ''}.log"
            candidate = os.path.join(log_dir, filename)
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                os.close(descriptor)
                _current_log_path = candidate
                break
            except FileExistsError:
                continue
        else:
            raise OSError("无法创建唯一的会话日志文件")
        _rotate_logs(log_dir)
        return _current_log_path


def _rotate_logs(log_dir: str, keep: int = 7) -> None:
    """只保留最近 keep 个日志文件"""
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
    """返回最新的日志文件路径，没有则返回 None"""
    files = sorted(
        glob.glob(os.path.join(log_dir, "engine-*.log")),
        key=os.path.getmtime,
        reverse=True,
    )
    return files[0] if files else None


def list_logs(log_dir: str) -> list[dict]:
    """按最新修改时间列出日志文件"""
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


def _emit(level: str, payload: str) -> None:
    """在引擎重定向的 stdout 上加锁写入日志"""
    with _log_lock:
        print(f"[{level}] {payload}", file=sys.stdout, flush=True)


def engine_info(msg: str) -> None:
    _emit("INFO", msg)


def engine_warn(msg: str) -> None:
    """写入可继续运行的警告日志"""
    _emit("WARN", msg)


def engine_error(event: str, **data: Any) -> None:
    """写入供 Dashboard 解析的 JSON 错误日志"""
    payload = json.dumps({"event": event, **data}, ensure_ascii=False, default=str)
    _emit("ERROR", payload)


def parse_log_line(line: str) -> Dict[str, Any] | None:
    """解析宿主写入的时间戳、级别和正文。"""
    match = re.fullmatch(
        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[(INFO|WARN|ERROR)\] (.*)",
        line.rstrip("\r\n"),
    )
    if match is None:
        return None
    ts, level, payload = match.groups()
    data = None
    text = payload
    if level == "ERROR":
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            data = decoded
            event = data.get("event", "?")
            plugin = data.get("plugin", data.get("rule", ""))
            reason = data.get("reason", "")
            if plugin and reason:
                text = f"[{event}] {plugin}: {reason}"
            elif reason:
                text = f"[{event}] {reason}"
    return {"ts": ts, "level": level, "text": text, "data": data}


def read_log_entries(log_path: str, lines: int = 500) -> list[Dict[str, Any]]:
    """读取日志末尾指定行数并返回解析后的条目"""
    entries: list[Dict[str, Any]] = []
    try:
        # deque 边读边丢尾部，日志再大也不会全部堆进内存
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = deque(f, maxlen=lines)
        for line in raw_lines:
            entry = parse_log_line(line)
            if entry is not None:
                entries.append(entry)
    except OSError:
        pass
    return entries


def build_diagnostics(
    entries: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """从解析后的日志条目构建诊断摘要"""

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
