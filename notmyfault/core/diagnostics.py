"""引擎诊断数据容器用锁保护计数和错误列表，并向 Dashboard 与 API 提供快照"""
import copy
import threading
from typing import Any, Dict


class Diagnostics:
    """线程安全的诊断数据容器"""

    _MAX_ERRORS = 50
    _MAX_PLUGIN_ERRORS = 200
    _MAX_RULE_ISSUES = 500

    def __init__(self) -> None:
        # 错误列表超过上限时删除最早的记录
        self._data: Dict[str, Any] = {
            "plugin_errors": [],      # 保存存储名、插件 ID 和原因
            "rule_issues": [],        # 保存规则名和问题
            "action_ok": 0,
            "action_fail": 0,
            "hot_reload_errors": 0,
            "trigger_crashes": 0,     # 触发器线程未捕获异常计数
            "trigger_crash_details": [],  # 保存最近 20 条触发器 ID 和错误
            "errors": [],             # 保存最近 _MAX_ERRORS 条分类和详情
        }
        self._lock = threading.RLock()

    # 旧代码仍通过 engine._diag 和 engine._diag_lock 访问这两个属性
    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def record_plugin_error(self, store: str, plugin_id: str, reason: str) -> None:
        with self._lock:
            self._data["plugin_errors"].append((store, plugin_id, reason))
            if len(self._data["plugin_errors"]) > self._MAX_PLUGIN_ERRORS:
                del self._data["plugin_errors"][: len(self._data["plugin_errors"]) - self._MAX_PLUGIN_ERRORS]

    def reset_rule_issues(self) -> None:
        with self._lock:
            self._data["rule_issues"] = []

    def add_rule_issue(self, rule_name: str, issue: str) -> None:
        with self._lock:
            self._data["rule_issues"].append((rule_name, issue))
            if len(self._data["rule_issues"]) > self._MAX_RULE_ISSUES:
                del self._data["rule_issues"][: len(self._data["rule_issues"]) - self._MAX_RULE_ISSUES]

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
        """记录错误并保留最近 _MAX_ERRORS 条"""
        with self._lock:
            self._data["errors"].append((category, detail))
            if len(self._data["errors"]) > self._MAX_ERRORS:
                del self._data["errors"][: len(self._data["errors"]) - self._MAX_ERRORS]

    def record_trigger_crash(self, trigger_id: str, error: str) -> None:
        """记录触发器崩溃并在同一把锁内更新计数和详情"""
        with self._lock:
            self._data["trigger_crashes"] += 1
            self._data["errors"].append(("trigger_crash", f"{trigger_id}: {error}"))
            if len(self._data["errors"]) > self._MAX_ERRORS:
                del self._data["errors"][: len(self._data["errors"]) - self._MAX_ERRORS]
            self._data["trigger_crash_details"].append({
                "trigger_id": trigger_id,
                "error": error[:300],
            })
            if len(self._data["trigger_crash_details"]) > 20:
                del self._data["trigger_crash_details"][:-20]

    def snapshot(self) -> Dict[str, Any]:
        # snapshot() 返回副本，Dashboard 读取与写入线程分开
        with self._lock:
            return copy.deepcopy(self._data)
