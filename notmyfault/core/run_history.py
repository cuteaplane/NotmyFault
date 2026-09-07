"""把引擎事件整理成可查询的规则运行记录"""

from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from collections import deque
from typing import Any, Dict, Iterable

from notmyfault.core import run_lifecycle as lifecycle


_RUN_EVENT_TYPES = frozenset(
    {
        "rule_triggered",
        "workflow_deferred",
        "workflow_failed",
        "workflow_completed",
        "action_executed",
        "action_skipped",
        "action_cancelled",
        "action_timed_out",
        "test_assertions_completed",
        "error",
        "run_queued",
        "run_dropped",
        "run_replaced",
    }
)


def _duration_ms(started_at: Any, finished_at: Any) -> int | None:
    try:
        duration = (float(finished_at) - float(started_at)) * 1000
        return max(0, round(duration)) if math.isfinite(duration) else None
    except (OverflowError, TypeError, ValueError):
        return None


def _safe_count(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if not math.isfinite(number) or not number.is_integer():
        return default
    parsed = int(number)
    return parsed if 0 <= parsed <= 1_000_000 else default


def _safe_summary(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        label = item.get("label")
        display = item.get("display")
        if not all(isinstance(part, str) for part in (name, label, display)):
            continue
        result.append({
            "name": name[:80],
            "label": label[:80],
            "type": str(item.get("type", "any"))[:30],
            "display": display[:120],
            "redacted": item.get("redacted") is True,
        })
    return result


def _safe_event(packet: Dict[str, Any]) -> Dict[str, Any] | None:
    event_type = packet.get("type")
    data = packet.get("data")
    if event_type not in _RUN_EVENT_TYPES or not isinstance(data, dict):
        return None
    # 比当前版本新的事件格式读不懂，整条丢弃，等引擎升级后再解释
    schema = packet.get("schema")
    if isinstance(schema, int) and schema > lifecycle.RUN_EVENT_SCHEMA_VERSION:
        return None
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None

    kept = {
        key: copy.deepcopy(data[key])
        for key in (
            "run_id",
            "rule_id",
            "rule_name",
            "event_type",
            "action_count",
            "precondition_count",
            "start_step_id",
            "end_step_id",
            "assertion_count",
            "assertions_passed",
            "assertions_total",
            "failure_kind",
            "action_type",
            "step_id",
            "status",
            "reason",
            "retry_after_seconds",
            "duration_ms",
            "attempt",
        )
        if key in data
    }
    error = data.get("error")
    if isinstance(error, dict):
        kept["error"] = {
            key: str(error[key])[:240]
            for key in ("code", "location", "message")
            if key in error
        }
    elif error is not None:
        kept["error"] = {
            "code": "run_error",
            "message": "运行失败",
        }
    if event_type in {
        "action_executed", "action_cancelled", "action_timed_out", "error"
    }:
        kept["input_summary"] = _safe_summary(data.get("input_summary"))
        kept["output_summary"] = _safe_summary(data.get("output_summary"))
    if event_type == "test_assertions_completed":
        kept["passed"] = _safe_count(data.get("passed"))
        kept["total"] = _safe_count(data.get("total"))
        raw_results = data.get("results", [])
        if not isinstance(raw_results, list):
            raw_results = []
        kept["results"] = [
            {
                key: copy.deepcopy(result[key])
                for key in ("step_id", "path", "operator", "passed", "message")
                if key in result
            }
            for result in raw_results[:50]
            if isinstance(result, dict)
        ]
    saved = {
        "type": event_type,
        "data": kept,
        "ts": packet.get("ts"),
    }
    if packet.get("schema") is not None:
        saved["schema"] = packet.get("schema")
    return saved


def _new_run(data: Dict[str, Any], timestamp: Any) -> Dict[str, Any]:
    return {
        "run_id": data.get("run_id", ""),
        "rule_id": data.get("rule_id", ""),
        "rule_name": data.get("rule_name", "未命名规则"),
        "event_type": data.get("event_type", ""),
        "status": "running",
        "started_at": timestamp,
        "finished_at": None,
        "duration_ms": None,
        "action_count": _safe_count(data.get("action_count")),
        "precondition_count": _safe_count(data.get("precondition_count")),
        "start_step_id": data.get("start_step_id", ""),
        "end_step_id": data.get("end_step_id", ""),
        "assertions_passed": 0,
        "assertions_total": _safe_count(data.get("assertion_count")),
        "assertion_results": [],
        "failure_kind": "",
        "steps": [],
        "error": None,
        "deferred_reason": None,
        "retry_after_seconds": None,
        "replayable": False,
    }


def _find_step(run: Dict[str, Any], step_id: str) -> Dict[str, Any] | None:
    for step in run["steps"]:
        if step.get("step_id") == step_id:
            return step
    return None


def _apply_event(run: Dict[str, Any], packet: Dict[str, Any]) -> None:
    event_type = packet["type"]
    data = packet["data"]
    timestamp = packet.get("ts")
    # 到终点的 run 不再吃任何事件。重复 complete / cancel / resume 在写入侧
    # 被 _finish_run 挡掉，迟到的在这里跳过
    if lifecycle.is_terminal(run["status"]):
        return
    if event_type == "rule_triggered":
        run.update(
            rule_id=data.get("rule_id", run["rule_id"]),
            rule_name=data.get("rule_name", run["rule_name"]),
            event_type=data.get("event_type", run["event_type"]),
            action_count=_safe_count(
                data.get("action_count"), run["action_count"]
            ),
            precondition_count=_safe_count(
                data.get("precondition_count"), run["precondition_count"]
            ),
            start_step_id=data.get("start_step_id", run["start_step_id"]),
            end_step_id=data.get("end_step_id", run["end_step_id"]),
            assertions_total=_safe_count(
                data.get("assertion_count"), run["assertions_total"]
            ),
        )
        return

    if event_type == "test_assertions_completed":
        run["assertions_passed"] = _safe_count(data.get("passed"))
        run["assertions_total"] = _safe_count(data.get("total"))
        run["assertion_results"] = data.get("results", [])
        return

    if event_type in {
        "action_executed",
        "action_skipped",
        "action_cancelled",
        "action_timed_out",
        "error",
        "workflow_failed",
    }:
        step_id = str(data.get("step_id") or "")
        if step_id:
            step = _find_step(run, step_id)
            if step is None:
                step = {
                    "step_id": step_id,
                    "action_type": data.get("action_type", ""),
                    "status": "running",
                    "finished_at": timestamp,
                    "duration_ms": data.get("duration_ms"),
                    "attempt": data.get("attempt"),
                    "reason": None,
                    "error": None,
                }
                run["steps"].append(step)
            if event_type == "action_executed":
                step["status"] = "succeeded"
            elif event_type == "action_skipped":
                step["status"] = "skipped"
                step["reason"] = data.get("reason")
            elif event_type == "action_cancelled":
                step["status"] = "cancelled"
                step["reason"] = data.get("error") or "动作已取消"
            elif event_type == "action_timed_out":
                step["status"] = "timed_out"
                step["error"] = data.get("error")
            else:
                step["status"] = "failed"
                step["error"] = data.get("error")
            step["finished_at"] = timestamp
            step["duration_ms"] = data.get("duration_ms", step.get("duration_ms"))
            step["attempt"] = data.get("attempt", step.get("attempt"))
            if "input_summary" in data:
                step["input_summary"] = data["input_summary"]
            if "output_summary" in data:
                step["output_summary"] = data["output_summary"]

    new_status = lifecycle.status_after(event_type, data, run["status"])
    if event_type == "workflow_deferred":
        run["status"] = new_status or run["status"]
        run["deferred_reason"] = data.get("reason")
        run["retry_after_seconds"] = data.get("retry_after_seconds")
    elif event_type == "run_queued":
        run["status"] = new_status or run["status"]
        run["queued_reason"] = data.get("reason")
    elif event_type in ("run_dropped", "run_replaced"):
        run["status"] = new_status or run["status"]
        run["drop_reason"] = data.get("reason")
        run["finished_at"] = timestamp
        run["duration_ms"] = _duration_ms(run["started_at"], timestamp)
    elif event_type == "workflow_failed":
        run["status"] = new_status or run["status"]
        run["error"] = data.get("error")
        run["finished_at"] = timestamp
        run["duration_ms"] = _duration_ms(run["started_at"], timestamp)
    elif event_type == "workflow_completed":
        run["status"] = new_status or run["status"]
        run["assertions_passed"] = _safe_count(
            data.get("assertions_passed"), run["assertions_passed"]
        )
        run["assertions_total"] = _safe_count(
            data.get("assertions_total"), run["assertions_total"]
        )
        run["failure_kind"] = data.get("failure_kind", "")
        run["finished_at"] = timestamp
        run["duration_ms"] = data.get(
            "duration_ms", _duration_ms(run["started_at"], timestamp)
        )
    elif new_status == "running":
        # deferred 的 run 收到动作事件说明重试已经跑起来了
        run["status"] = "running"


def build_runs(events: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    runs: Dict[str, Dict[str, Any]] = {}
    order: list[str] = []
    for packet in events:
        data = packet.get("data")
        if not isinstance(data, dict):
            continue
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        run = runs.get(run_id)
        if run is None:
            run = _new_run(data, packet.get("ts"))
            runs[run_id] = run
            order.append(run_id)
        _apply_event(run, packet)
    return [runs[run_id] for run_id in reversed(order)]


class RunHistory:
    """用 JSONL 保存脱敏后的运行事件并按需汇总"""

    def __init__(self, path: str, max_events: int = 5000) -> None:
        self.path = path
        self.max_events = max(100, int(max_events))
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._writer_done = threading.Condition(self._lock)
        self._events = deque(self._read_events_unlocked()[-self.max_events:], maxlen=self.max_events)
        self._pending: deque[str] = deque(maxlen=self.max_events)
        self._writer: threading.Thread | None = None
        self._write_error: OSError | None = None
        self._event_count = self._count_events()

    def _count_events(self) -> int:
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                return sum(1 for line in file if line.strip())
        except OSError:
            return 0

    def _read_events_unlocked(self) -> list[Dict[str, Any]]:
        events = []
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                for line in file:
                    try:
                        packet = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(packet, dict):
                        events.append(packet)
        except OSError:
            pass
        return events

    def _compact_unlocked(self) -> None:
        events = self._read_events_unlocked()[-self.max_events :]
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8", newline="\n") as file:
            for packet in events:
                file.write(json.dumps(packet, ensure_ascii=False, separators=(",", ":")))
                file.write("\n")
        os.replace(temp_path, self.path)
        self._event_count = len(events)

    def _prepare_packet(self, packet: Dict[str, Any]) -> Dict[str, Any] | None:
        if isinstance(packet, dict) and "schema" not in packet:
            packet = {**packet, "schema": lifecycle.RUN_EVENT_SCHEMA_VERSION}
        return _safe_event(packet)

    def _write_batch(self, packets: list[Dict[str, Any] | str]) -> None:
        with self._file_lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as file:
                for packet in packets:
                    file.write(packet if isinstance(packet, str) else json.dumps(packet, ensure_ascii=False, separators=(",", ":")))
                    file.write("\n")
            self._event_count += len(packets)
            if self._event_count > self.max_events + 500:
                self._compact_unlocked()

    def record(self, packet: Dict[str, Any]) -> None:
        safe_packet = self._prepare_packet(packet)
        if safe_packet is None:
            return
        self._write_batch([safe_packet])
        with self._lock:
            self._events.append(safe_packet)

    def record_async(self, packet: Dict[str, Any]) -> None:
        safe_packet = self._prepare_packet(packet)
        if safe_packet is None:
            return
        encoded = json.dumps(safe_packet, ensure_ascii=False, separators=(",", ":"))
        with self._writer_done:
            self._events.append(safe_packet)
            self._pending.append(encoded)
            if self._writer is None:
                self._writer = threading.Thread(target=self._write_pending, name="RunHistory", daemon=True)
                self._writer.start()
            self._writer_done.notify_all()

    def _write_pending(self) -> None:
        while True:
            with self._writer_done:
                if not self._pending:
                    self._writer_done.wait(timeout=0.25)
                    if not self._pending:
                        self._writer = None
                        self._writer_done.notify_all()
                        return
                batch = [self._pending.popleft() for _ in range(min(128, len(self._pending)))]
            try:
                self._write_batch(batch)
            except OSError as error:
                from notmyfault.core.logging import engine_error

                with self._lock:
                    self._write_error = error
                engine_error("run_history_write_failed", error=str(error))

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._writer_done:
            while self._writer is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._writer_done.wait(remaining)
            return self._write_error is None

    def list_runs(self, limit: int = 100) -> list[Dict[str, Any]]:
        with self._lock:
            events = list(self._events)
        return build_runs(events)[:max(1, min(int(limit), 1000))]

    def get_run(self, run_id: str) -> Dict[str, Any] | None:
        with self._lock:
            events = [packet for packet in self._events if isinstance(packet.get("data"), dict) and packet["data"].get("run_id") == run_id]
        runs = build_runs(events)
        return runs[0] if runs else None
