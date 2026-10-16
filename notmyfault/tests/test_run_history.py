"""运行记录持久化、汇总和脱敏"""

import json

from notmyfault.core.run_history import RunHistory


def test_async_history_keeps_queries_current_and_flushes_batches(tmp_path, monkeypatch):
    import threading

    history = RunHistory(str(tmp_path / "runs.jsonl"))
    writing = threading.Event()
    release = threading.Event()
    original = history._write_batch
    def slow_write(packets):
        writing.set()
        assert release.wait(5)
        original(packets)
    monkeypatch.setattr(history, "_write_batch", slow_write)
    try:
        for index in range(1100):
            history.record_async({"v": 1, "type": "run.triggered", "ts": index, "data": {"run_id": f"run_{index}"}})
        assert writing.wait(5)
        assert len(history.list_runs(1000)) == 1000
        assert history.get_run("run_1")["run_id"] == "run_1"
    finally:
        release.set()
    assert history.flush()
    saved = RunHistory(history.path)
    assert saved.get_run("run_1") is not None
    assert len(saved.list_runs(1000)) == 1000


def packet(name, timestamp, **data):
    return {"v": 1, "type": name, "ts": timestamp, "data": data}


def test_async_history_flush_reports_write_failure(tmp_path, monkeypatch):
    history = RunHistory(str(tmp_path / "runs.jsonl"))

    def fail(packets):
        raise OSError("磁盘已满")

    monkeypatch.setattr(history, "_write_batch", fail)
    history.record_async(packet("run.triggered", 1, run_id="run_fail"))

    assert history.flush() is False


def test_waiting_group_resumes_and_keeps_the_stable_branch_and_parent(tmp_path):
    path = str(tmp_path / "runs.jsonl")
    history = RunHistory(path)
    common = {"run_id": "run_group001", "step_id": "a_wait0001", "action_type": "event_group", "group_path": ["a_group001"]}
    history.record_async(packet("run.triggered", 10, run_id="run_group001"))
    history.record_async(packet("run.step.started", 11, **common))
    history.record_async(packet("run.step.waiting", 12, **common, reason="等待退出事件"))
    waiting = history.get_run("run_group001")
    assert waiting["status"] == "running"
    assert waiting["steps"][0]["status"] == "waiting"
    assert waiting["steps"][0]["finished_at"] is None
    assert waiting["steps"][0]["reason"] == "等待退出事件"
    history.record_async(packet("run.step.resumed", 20, **common))
    resumed = history.get_run("run_group001")["steps"][0]
    assert resumed["started_at"] == 11
    assert resumed["status"] == "running"
    assert resumed["reason"] is None
    history.record_async(packet("run.step.succeeded", 21, **common, branch="b_recover01"))
    history.record_async(packet("run.succeeded", 22, run_id="run_group001"))
    history.flush()
    restored = RunHistory(path).get_run("run_group001")
    assert restored["status"] == "succeeded"
    assert len(restored["steps"]) == 1
    assert restored["steps"][0]["branch"] == "b_recover01"
    assert restored["steps"][0]["group_path"] == ["a_group001"]


def test_history_keeps_only_condition_results_and_drops_operands_and_invalid_trace(tmp_path):
    history = RunHistory(str(tmp_path / 'runs.jsonl'))
    history.record_async(packet('run.triggered', 1, run_id='run_trace'))
    history.record_async(packet('run.step.failed', 2, run_id='run_trace', step_id='a_group001', action_type='event_group', evaluated_branches=[
        {'branch_id': 'b_first01', 'result': False, 'left': '不能保存', 'predicates': [
            {'node_id': 'a_first01', 'op': 'eq', 'result': False, 'left': '不能保存'},
            {'node_id': '不能保存', 'path': 'condition', 'op': {}, 'result': True}]},
        {'branch_id': 'b_second1', 'error': {'code': 'missing_binding_value', 'message': '不能保存'}, 'predicates': []},
        {'branch_id': 'b_third01', 'result': '不能保存'}]))
    history.flush()
    restored = RunHistory(history.path).get_run('run_trace')['steps'][0]['evaluated_branches']
    assert restored == [
        {'branch_id': 'b_first01', 'result': False, 'predicates': [{'node_id': 'a_first01', 'op': 'eq', 'result': False}]},
        {'branch_id': 'b_second1', 'error': {'code': 'missing_binding_value'}, 'predicates': []}]
    assert '不能保存' not in (tmp_path / 'runs.jsonl').read_text()


def test_run_history_builds_completed_run_with_steps(tmp_path):
    history = RunHistory(str(tmp_path / "runs.jsonl"))
    history.record_async(
        packet(
            "run.triggered",
            100.0,
            run_id="run_1",
            rule_id="r_rule001",
            rule_name="测试规则",
            event_type="manual",
            action_count=2,
            precondition_count=1,
            event_payload={"secret": "不能落盘"},
        )
    )
    history.record_async(
        packet(
            "run.step.succeeded",
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
            branch="不能落盘",
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
    history.record_async(
        packet(
            "run.step.skipped",
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
    history.record_async(
        packet(
            "run.succeeded",
            100.5,
            run_id="run_1",
            rule_id="r_rule001",
            rule_name="测试规则",
            status="succeeded",
        )
    )
    history.flush()

    run = history.get_run("run_1")

    assert run["status"] == "succeeded"
    assert run["duration_ms"] == 500
    assert "precondition_count" not in run
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
        "not-json\n{\"type\":\"rule_triggered\",\"data\":null}\n"
        + json.dumps(
            packet(
                "run.triggered",
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

    history = RunHistory(str(path))
    runs = history.list_runs()

    assert len(runs) == 1
    assert runs[0]["run_id"] == "run_ok"
    assert runs[0]["status"] == "queued"
    assert history.get_run("run_ok") == runs[0]


def test_async_history_compacts_broken_lines_and_keeps_latest(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text(
        "".join("not-json\n" for _ in range(300))
        + json.dumps(packet("run.triggered", 1, run_id="run_old")) + "\n",
        encoding="utf-8",
    )
    history = RunHistory(str(path), max_events=100)

    for start in range(0, 300, 100):
        for index in range(start, start + 100):
            history.record_async({
                "v": 1, "type": "run.triggered", "ts": index, "data": {"run_id": f"run_{index}"},
            })
        assert history.flush()

    assert "not-json" not in path.read_text(encoding="utf-8")
    runs = RunHistory(str(path)).list_runs(1000)
    assert len(runs) == 100
    assert runs[0]["run_id"] == "run_299"


def test_run_history_rejects_malformed_counts_and_timestamps(tmp_path):
    history = RunHistory(str(tmp_path / "runs.jsonl"))
    history.record_async(packet(
        "run.triggered",
        float("inf"),
        run_id="run_malformed",
        action_count={"bad": 1},
        precondition_count="NaN",
        assertion_count="1000001",
    ))
    history.record_async(packet(
        "run.succeeded",
        float("-inf"),
        run_id="run_malformed",
        status="succeeded",
        assertions_passed=[],
        assertions_total="not-a-number",
    ))

    run = history.get_run("run_malformed")

    assert run["action_count"] == 0
    assert "precondition_count" not in run
    assert run["assertions_passed"] == 0
    assert run["assertions_total"] == 0
    assert run["duration_ms"] is None


def test_run_history_keeps_partial_scope_and_safe_assertion_summary(tmp_path):
    path = tmp_path / "runs.jsonl"
    history = RunHistory(str(path))
    history.record_async(packet(
        "run.triggered",
        10.0,
        run_id="run_partial",
        rule_name="局部运行",
        event_type="manual",
        action_count=1,
        start_step_id="a_second001",
        end_step_id="",
        assertion_count=1,
    ))
    history.record_async(packet(
        "run.assertions.completed",
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
    history.record_async(packet(
        "run.succeeded",
        10.3,
        run_id="run_partial",
        rule_name="局部运行",
        status="failed",
        assertions_passed=0,
        assertions_total=1,
        failure_kind="assertion",
    ))
    history.flush()

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
    history.record_async(
        packet(
            "run.triggered",
            50.0,
            run_id="run_failed",
            rule_name="失败规则",
            event_type="usb_insert",
        )
    )
    history.record_async(
        packet(
            "run.step.failed",
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

    history.record_async(packet("run.failed", 50.25, run_id="run_failed", error={"code": "missing_reference", "location": "actions.a_one.params.url", "message": "找不到来源"}))

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
            "input_summary": [],
            "output_summary": [],
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
    history.record_async(packet(
        "run.triggered",
        80.0,
        run_id="run_cancelled",
        rule_name="可停止规则",
        event_type="manual",
        action_count=2,
    ))
    history.record_async(packet(
        "run.step.timed_out",
        80.2,
        run_id="run_cancelled",
        rule_name="可停止规则",
        action_type="waitable",
        step_id="a_timeout001",
        error={"code": "action_timeout", "message": "动作运行超时"},
        duration_ms=200,
    ))
    history.record_async(packet(
        "run.step.cancelled",
        80.3,
        run_id="run_cancelled",
        rule_name="可停止规则",
        action_type="waitable",
        step_id="a_cancel001",
        error={"code": "action_cancelled", "message": "动作已取消"},
        duration_ms=100,
    ))
    history.record_async(packet(
        "run.cancelled",
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
