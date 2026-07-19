"""引擎诊断数据：线程安全的计数器、问题列表与错误上报通道。

设计目标：
- 错误不石沉大海：提供 record_error / record_trigger_crash 统一上报，
  snapshot() 暴露最近错误，供 Dashboard / API 观测。
- 写入经锁保护，snapshot() 返回深拷贝快照，消除读取与写入竞态。
"""
import copy
import threading
from typing import Any, Dict


class Diagnostics:
    """线程安全的诊断数据容器。"""

    _MAX_ERRORS = 50

    def __init__(self) -> None:
        # 诊断是黑匣子，不是无限垃圾桶；错误列表下面会自动截断。
        self._data: Dict[str, Any] = {
            "plugin_errors": [],      # [(store, plugin_id, reason), ...]
            "rule_issues": [],        # [(rule_name, issue), ...]
            "action_ok": 0,
            "action_fail": 0,
            "hot_reload_errors": 0,
            "trigger_crashes": 0,     # 触发器线程未捕获异常计数
            "errors": [],             # [(category, detail), ...] 最近 _MAX_ERRORS 条
        }
        self._lock = threading.RLock()

    # -- 兼容入口：engine._diag / engine._diag_lock 仍指向内部对象 --
    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # -- 写入（均加锁） --
    def record_plugin_error(self, store: str, plugin_id: str, reason: str) -> None:
        with self._lock:
            self._data["plugin_errors"].append((store, plugin_id, reason))

    def reset_rule_issues(self) -> None:
        with self._lock:
            self._data["rule_issues"] = []

    def add_rule_issue(self, rule_name: str, issue: str) -> None:
        with self._lock:
            self._data["rule_issues"].append((rule_name, issue))

    def inc_action_ok(self) -> None:
        with self._lock:
            self._data["action_ok"] += 1

    def inc_action_fail(self) -> None:
        with self._lock:
            self._data["action_fail"] += 1

    def inc_hot_reload_error(self) -> None:
        with self._lock:
            self._data["hot_reload_errors"] += 1

    def inc_trigger_crash(self) -> None:
        with self._lock:
            self._data["trigger_crashes"] += 1

    def record_error(self, category: str, detail: str) -> None:
        """通用错误上报：写入环形日志（仅保留最近 _MAX_ERRORS 条）。"""
        with self._lock:
            self._data["errors"].append((category, detail))
            if len(self._data["errors"]) > self._MAX_ERRORS:
                del self._data["errors"][: len(self._data["errors"]) - self._MAX_ERRORS]

    def record_trigger_crash(self, trigger_id: str, error: str) -> None:
        """触发器线程崩溃：计数 + 记入错误日志。

        计数与错误记录在同一把锁内完成，避免 snapshot() 在两次加锁之间观察到
        “崩了计数已加但错误日志还没写”的不一致快照。
        """
        with self._lock:
            self._data["trigger_crashes"] += 1
            self._data["errors"].append(("trigger_crash", f"{trigger_id}: {error}"))
            if len(self._data["errors"]) > self._MAX_ERRORS:
                del self._data["errors"][: len(self._data["errors"]) - self._MAX_ERRORS]

    # -- 读取：返回不可变快照 --
    def snapshot(self) -> Dict[str, Any]:
        # 给 Dashboard 的必须是副本，不然它一边看我们一边写，容易看出幻觉。
        with self._lock:
            return copy.deepcopy(self._data)
