"""动作运行摘要的脱敏和大小边界"""

import json
from pathlib import Path

from notmyfault.core.run_summary import summarize_fields


def test_default_summary_only_describes_value_shape():
    summary = summarize_fields(
        {
            "text": "TOP_SECRET",
            "items": [1, 2, 3],
            "meta": {"token": "HIDDEN"},
        },
        [
            {"name": "text", "label": "文本", "type": "string"},
            {"name": "items", "label": "列表", "type": "array"},
            {"name": "meta", "label": "对象", "type": "object"},
        ],
    )

    assert [item["display"] for item in summary] == [
        "文本 · 10 字符",
        "列表 · 3 项",
        "对象 · 1 个字段",
    ]
    assert "TOP_SECRET" not in repr(summary)
    assert "HIDDEN" not in repr(summary)


def test_value_policy_is_explicit_and_sensitive_always_wins():
    summary = summarize_fields(
        {"count": 12, "ready": True, "token": "TOP_SECRET", "hidden": "x"},
        [
            {"name": "count", "label": "数量", "type": "number", "summary": "value"},
            {"name": "ready", "label": "就绪", "type": "bool", "summary": "value"},
            {
                "name": "token",
                "label": "令牌",
                "type": "string",
                "summary": "value",
                "sensitive": True,
            },
            {"name": "hidden", "label": "隐藏", "type": "string", "summary": "hidden"},
        ],
    )

    assert [item["display"] for item in summary] == ["12", "是", "敏感值已隐藏"]
    assert summary[-1]["redacted"] is True
    assert "TOP_SECRET" not in repr(summary)
    assert all(item["name"] != "hidden" for item in summary)


def test_summary_ignores_undeclared_fields_and_limits_field_count():
    values = {f"field_{index}": index for index in range(12)}
    values["undeclared"] = "secret"
    definitions = [
        {
            "name": f"field_{index}",
            "label": f"字段 {index}",
            "type": "number",
            "summary": "value",
        }
        for index in range(12)
    ]

    summary = summarize_fields(values, definitions)

    assert len(summary) == 8
    assert "undeclared" not in repr(summary)
    assert "secret" not in repr(summary)


def test_single_declared_output_can_summarize_scalar_result():
    summary = summarize_fields(
        "done",
        [{"name": "result", "label": "结果", "type": "string"}],
    )

    assert summary == [{
        "name": "result",
        "label": "结果",
        "type": "string",
        "display": "文本 · 4 字符",
        "redacted": False,
    }]


def test_uia_read_text_never_enters_run_summary():
    action_path = (
        Path(__file__).parents[1]
        / "actions"
        / "uia_read_text"
        / "action.json"
    )
    outputs = json.loads(action_path.read_text(encoding="utf-8"))["outputs"]

    summary = summarize_fields(
        {"text": "月度报告", "display": {"control": "文件名"}},
        outputs,
    )

    assert all(item["name"] != "text" for item in summary)
    assert "月度报告" not in repr(summary)
