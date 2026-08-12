"""本地自然语言规则草稿"""

from pathlib import Path

import pytest

from notmyfault.core.rule_drafting import draft_rule_from_text
from notmyfault.security.plugin_schema import scan_plugins


def _schema():
    return {
        "triggers": {
            "manual": {"name": "手动触发", "params": []},
            "time_schedule": {
                "name": "定时触发",
                "params": [{"name": "time", "label": "触发时间", "type": "time", "default": "22:00"}],
            },
            "usb_insert": {
                "name": "U盘插入监控",
                "params": [{"name": "drive_letter", "label": "目标盘符", "type": "string", "default": "ANY"}],
            },
            "folder_monitor": {
                "name": "文件夹监控",
                "params": [{"name": "folder_path", "label": "文件夹路径", "type": "path", "default": ""}],
            },
        },
        "actions": {
            "notify": {
                "name": "显示通知",
                "params": [
                    {"name": "title", "label": "通知标题", "type": "string", "default": "NotmyFault"},
                    {"name": "message", "label": "通知内容", "type": "textarea", "default": ""},
                ],
            },
            "file_operation": {
                "name": "文件操作",
                "params": [
                    {"name": "operation", "label": "操作类型", "type": "select", "default": "copy"},
                    {"name": "source", "label": "源路径", "type": "path", "default": ""},
                    {"name": "destination", "label": "目标路径", "type": "path", "default": ""},
                ],
            },
            "screenshot": {"name": "截图", "params": []},
        },
    }


def test_local_draft_extracts_time_and_notification_message():
    result = draft_rule_from_text("每天 08:30 提醒我提交月报", _schema())

    assert result["source"] == "local"
    assert result["draft"]["event"] == {
        "type": "time_schedule",
        "params": {"time": "08:30"},
    }
    assert result["draft"]["actions"][0]["type"] == "notify"
    assert result["draft"]["actions"][0]["params"]["message"] == "提交月报"
    assert result["missing"] == []


def test_action_without_start_condition_becomes_manual_draft():
    result = draft_rule_from_text("帮我截个图", _schema())

    assert result["draft"]["event"]["type"] == "manual"
    assert result["draft"]["actions"][0]["type"] == "screenshot"
    assert result["assumptions"] == ["没有识别到开始条件，先按手动运行起草"]


def test_partial_description_keeps_missing_work_visible():
    result = draft_rule_from_text("文件夹有新文件时通知我", _schema())

    assert result["draft"]["event"]["type"] == "folder_monitor"
    assert result["draft"]["actions"][0]["type"] == "notify"
    assert any("文件夹路径" in item for item in result["missing"])
    assert any("通知内容" in item for item in result["missing"])


def test_unknown_description_does_not_invent_a_rule():
    result = draft_rule_from_text("把它弄好", _schema())

    assert result["draft"] is None
    assert result["missing"] == ["什么时候开始", "开始以后做什么"]
    assert result["ready"] is False


def test_description_length_is_bounded():
    result = draft_rule_from_text("x" * 2001, _schema())

    assert result["ok"] is False
    assert result["code"] == "description_too_long"


def _real_schema():
    package_root = str(Path(__file__).parents[1])
    return {
        "triggers": scan_plugins(
            package_root, "triggers", "trigger.json"
        ),
        "actions": scan_plugins(
            package_root, "actions", "action.json"
        ),
    }


@pytest.mark.parametrize(
    ("description", "trigger_type", "action_types"),
    [
        ("连上 WiFi 后打开蓝牙", "wifi_network", ["bluetooth_toggle"]),
        ("电脑解锁时打开程序", "session_lock", ["launch_program"]),
        ("CPU超过 80% 时通知我", "system_resource", ["notify"]),
        (
            "每周一截图并写入日志",
            "cron_schedule",
            ["screenshot", "append_text"],
        ),
        (
            "U盘插入后备份文件并通知我完成了",
            "usb_insert",
            ["file_operation", "notify"],
        ),
    ],
)
def test_local_matcher_covers_installed_plugins(
    description, trigger_type, action_types
):
    result = draft_rule_from_text(description, _real_schema())

    assert result["draft"]["event"]["type"] == trigger_type
    assert [
        action["type"] for action in result["draft"]["actions"]
    ] == action_types
