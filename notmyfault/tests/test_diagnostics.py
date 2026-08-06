"""Diagnostics 容器的计数、错误截断与快照行为"""

import threading

from notmyfault.core.diagnostics import Diagnostics


class TestDiagnosticsInit:
    def test_initial_counters_are_zero(self):
        diag = Diagnostics()
        assert diag.data["action_ok"] == 0
        assert diag.data["action_fail"] == 0
        assert diag.data["hot_reload_errors"] == 0
        assert diag.data["trigger_crashes"] == 0

    def test_initial_lists_are_empty(self):
        diag = Diagnostics()
        assert diag.data["plugin_errors"] == []
        assert diag.data["rule_issues"] == []
        assert diag.data["trigger_crash_details"] == []
        assert diag.data["errors"] == []

    def test_data_property_returns_dict(self):
        diag = Diagnostics()
        assert isinstance(diag.data, dict)

    def test_lock_property_is_rlock(self):
        diag = Diagnostics()
        # RLock 可重入：同一线程连续两次获取不阻塞
        with diag.lock:
            with diag.lock:
                pass


class TestActionCounters:
    def test_inc_action_ok(self):
        diag = Diagnostics()
        diag.inc_action_ok()
        diag.inc_action_ok()
        assert diag.data["action_ok"] == 2

    def test_inc_action_fail(self):
        diag = Diagnostics()
        diag.inc_action_fail()
        assert diag.data["action_fail"] == 1

    def test_counters_independent(self):
        diag = Diagnostics()
        diag.inc_action_ok()
        diag.inc_action_fail()
        assert diag.data["action_ok"] == 1
        assert diag.data["action_fail"] == 1


class TestHotReloadAndTriggerCounters:
    def test_inc_hot_reload_error(self):
        diag = Diagnostics()
        diag.inc_hot_reload_error()
        diag.inc_hot_reload_error()
        assert diag.data["hot_reload_errors"] == 2

    def test_inc_trigger_crash_without_detail(self):
        diag = Diagnostics()
        diag.inc_trigger_crash()
        assert diag.data["trigger_crashes"] == 1
        assert diag.data["trigger_crash_details"] == []


class TestMaxErrorsTruncation:
    def test_record_error_within_limit(self):
        diag = Diagnostics()
        for i in range(10):
            diag.record_error("test", f"error {i}")
        assert len(diag.data["errors"]) == 10

    def test_record_error_exceeding_limit_truncates(self):
        diag = Diagnostics()
        for i in range(Diagnostics._MAX_ERRORS + 20):
            diag.record_error("test", f"error {i}")
        errors = diag.data["errors"]
        assert len(errors) == Diagnostics._MAX_ERRORS
        # 保留最近记录，最早的被丢弃
        assert errors[0] == ("test", "error 20")
        assert errors[-1] == ("test", f"error {Diagnostics._MAX_ERRORS + 19}")

    def test_trigger_crash_also_appends_to_errors(self):
        diag = Diagnostics()
        diag.record_trigger_crash("hotkey", "boom")
        assert diag.data["trigger_crashes"] == 1
        assert diag.data["errors"] == [("trigger_crash", "hotkey: boom")]

    def test_trigger_crash_details_capped_at_20(self):
        diag = Diagnostics()
        for i in range(25):
            diag.record_trigger_crash(f"t{i}", "err")
        details = diag.data["trigger_crash_details"]
        assert len(details) == 20
        assert details[0]["trigger_id"] == "t5"
        assert details[-1]["trigger_id"] == "t24"


class TestRecordPluginError:
    def test_single_error_recorded(self):
        diag = Diagnostics()
        diag.record_plugin_error("Trigger", "hotkey", "schema 校验失败")
        assert diag.data["plugin_errors"] == [
            ("Trigger", "hotkey", "schema 校验失败")
        ]

    def test_multiple_errors_accumulate(self):
        diag = Diagnostics()
        diag.record_plugin_error("Trigger", "a", "r1")
        diag.record_plugin_error("Action", "b", "r2")
        assert len(diag.data["plugin_errors"]) == 2

    def test_concurrent_record_plugin_error(self):
        diag = Diagnostics()
        threads = [
            threading.Thread(
                target=lambda i=i: diag.record_plugin_error("Trigger", f"p{i}", "x")
            )
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(diag.data["plugin_errors"]) == 20


class TestRuleIssues:
    def test_add_rule_issue_appends(self):
        diag = Diagnostics()
        diag.add_rule_issue("规则1", "动作不存在")
        assert diag.data["rule_issues"] == [("规则1", "动作不存在")]

    def test_reset_rule_issues_clears_all(self):
        diag = Diagnostics()
        diag.add_rule_issue("规则1", "问题")
        diag.reset_rule_issues()
        assert diag.data["rule_issues"] == []

    def test_add_after_reset_works(self):
        diag = Diagnostics()
        diag.add_rule_issue("a", "x")
        diag.reset_rule_issues()
        diag.add_rule_issue("b", "y")
        assert diag.data["rule_issues"] == [("b", "y")]


class TestSnapshot:
    def test_snapshot_reflects_current_state(self):
        diag = Diagnostics()
        diag.inc_action_ok()
        diag.record_plugin_error("Action", "x", "y")
        snap = diag.snapshot()
        assert snap["action_ok"] == 1
        assert snap["plugin_errors"] == [("Action", "x", "y")]

    def test_snapshot_is_deep_copy(self):
        diag = Diagnostics()
        diag.record_plugin_error("Action", "x", "y")
        snap = diag.snapshot()
        snap["plugin_errors"].append(("Trigger", "fake", "fake"))
        snap["action_ok"] = 999
        assert len(diag.data["plugin_errors"]) == 1
        assert diag.data["action_ok"] == 0
