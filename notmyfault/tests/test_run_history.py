"""运行记录持久化、汇总和脱敏"""

import json

from notmyfault.core.run_history import RunHistory


def packet(name, timestamp, **data):
    return {"type": name, "ts": timestamp, "data": data}


def test_run_history_builds_completed_run_with_steps(tmp_path):
    history = RunHistory(str(tmp_path / "runs.jsonl"))
    history.record(
        packet(
            "rule_triggered",
            100.0,
            run_id="run_1",
            rule_id="r_rule001",
            rule_name="测试规则",
            event_type="manual",
            action_count=2,
            event_payload={"secret": "不能落盘"},
        )
    )
    history.record(
        packet(
            "action_executed",
            100.2,
            run_id="run_1",
            rule_id="r_rule001",
            rule_name="测试规则",
            step_id="a_first",
            action_type="notify",
            duration_ms=180,
            attempt=1,
            params={"token": "不能落盘"},
            result={"token": "不能落盘"},
            input_summary=[{
                "name": "message",
                "label": "消息",
                "type": "string",
                "display": "文本 · 4 字符",
                "redacted": False,
                "raw": "不能落盘",
            }],
            output_summary=[{
                "name": "token",
                "label": "令牌",
                "type": "string",
                "display": "敏感值已隐藏",
                "redacted": True,
            }],
        )
    )
    history.record(
        packet(
            "action_skipped",
            100.3,
            run_id="run_1",
            rule_id="r_rule001",
            rule_name="测试规则",
            step_id="a_second",
            action_type="open_url",
            duration_ms=0,
            reason="数据来源未参与本次运行",
        )
    )
    history.record(
        packet(
            "workflow_completed",
            100.5,
            run_id="run_1",
            rule_id="r_rule001",
            rule_name="测试规则",
            status="succeeded",
        )
    )

    run = history.get_run("run_1")

    assert run["status"] == "succeeded"
    assert run["duration_ms"] == 500
    assert [step["status"] for step in run["steps"]] == ["succeeded", "skipped"]
    assert run["steps"][0]["duration_ms"] == 180
    assert run["steps"][0]["input_summary"][0]["display"] == "文本 · 4 字符"
    assert run["steps"][0]["output_summary"][0]["redacted"] is True
    assert run["replayable"] is False
    stored = (tmp_path / "runs.jsonl").read_text(encoding="utf-8")
    assert "不能落盘" not in stored
    assert "event_payload" not in stored
    assert "params" not in stored
    assert "result" not in stored


def test_run_history_recovers_from_broken_lines(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text(
        "not-json\n"
        + json.dumps(
            packet(
                "rule_triggered",
                10.0,
                run_id="run_ok",
                rule_name="幸存记录",
                event_type="hotkey",
            ),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    runs = RunHistory(str(path)).list_runs()

    assert len(runs) == 1
    assert runs[0]["run_id"] == "run_ok"
    assert runs[0]["status"] == "running"


def test_run_history_keeps_partial_scope_and_safe_assertion_summary(tmp_path):
    path = tmp_path / "runs.jsonl"
    history = RunHistory(str(path))
    history.record(packet(
        "rule_triggered",
        10.0,
        run_id="run_partial",
        rule_name="局部运行",
        event_type="manual",
        action_count=1,
        start_step_id="a_second001",
        end_step_id="",
        assertion_count=1,
    ))
    history.record(packet(
        "test_assertions_completed",
        10.2,
        run_id="run_partial",
        rule_name="局部运行",
        passed=0,
        total=1,
        results=[{
            "step_id": "a_second001",
            "path": ["token"],
            "operator": "equals",
            "passed": False,
            "message": "动作结果不符合预期",
            "actual": "不能落盘",
            "expected": "也不能落盘",
        }],
    ))
    history.record(packet(
        "workflow_completed",
        10.3,
        run_id="run_partial",
        rule_name="局部运行",
        status="failed",
        assertions_passed=0,
        assertions_total=1,
        failure_kind="assertion",
    ))

    run = history.get_run("run_partial")
    stored = path.read_text(encoding="utf-8")

    assert run["start_step_id"] == "a_second001"
    assert run["action_count"] == 1
    assert run["assertions_total"] == 1
    assert run["failure_kind"] == "assertion"
    assert run["assertion_results"][0]["message"] == "动作结果不符合预期"
    assert "不能落盘" not in stored


def test_run_history_marks_binding_failure_terminal(tmp_path):
    history = RunHistory(str(tmp_path / "runs.jsonl"))
    history.record(
        packet(
            "rule_triggered",
            50.0,
            run_id="run_failed",
            rule_name="失败规则",
            event_type="usb_insert",
        )
    )
    history.record(
        packet(
            "workflow_failed",
            50.25,
            run_id="run_failed",
            rule_name="失败规则",
            action_type="open_url",
            step_id="a_failed001",
            error={
                "code": "missing_reference",
                "location": "actions.a_one.params.url",
                "message": "找不到来源",
                "private": "不保存",
            },
        )
    )

    run = history.get_run("run_failed")

    assert run["status"] == "failed"
    assert run["duration_ms"] == 250
    assert run["steps"] == [
        {
            "step_id": "a_failed001",
            "action_type": "open_url",
            "status": "failed",
            "finished_at": 50.25,
            "duration_ms": None,
            "attempt": None,
            "reason": None,
            "error": {
                "code": "missing_reference",
                "location": "actions.a_one.params.url",
                "message": "找不到来源",
            },
        }
    ]
    assert run["error"] == {
        "code": "missing_reference",
        "location": "actions.a_one.params.url",
        "message": "找不到来源",
    }


def test_run_history_distinguishes_cancelled_and_timed_out_steps(tmp_path):
    history = RunHistory(str(tmp_path / "runs.jsonl"))
    history.record(packet(
        "rule_triggered",
        80.0,
        run_id="run_cancelled",
        rule_name="可停止规则",
        event_type="manual",
        action_count=2,
    ))
    history.record(packet(
        "action_timed_out",
        80.2,
        run_id="run_cancelled",
        rule_name="可停止规则",
        action_type="waitable",
        step_id="a_timeout001",
        error="动作运行超时",
        duration_ms=200,
    ))
    history.record(packet(
        "action_cancelled",
        80.3,
        run_id="run_cancelled",
        rule_name="可停止规则",
        action_type="waitable",
        step_id="a_cancel001",
        error="动作已取消",
        duration_ms=100,
    ))
    history.record(packet(
        "workflow_completed",
        80.3,
        run_id="run_cancelled",
        rule_name="可停止规则",
        status="cancelled",
        failure_kind="cancelled",
    ))

    run = history.get_run("run_cancelled")

    assert run["status"] == "cancelled"
    assert run["failure_kind"] == "cancelled"
    assert [step["status"] for step in run["steps"]] == [
        "timed_out",
        "cancelled",
    ]
