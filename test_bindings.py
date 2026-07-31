import unittest
from typing import cast

from notmyfault.bindings import (
    iter_legacy_event_payload_paths,
    references_available,
    resolve_value,
)
from notmyfault.rules import validate_rule_bindings
from notmyfault.workflow_executor import WorkflowExecutor


class LegacyBindingTests(unittest.TestCase):
    def test_legacy_event_payload_paths_include_nested_and_full_payload(self):
        rule = {
            "actions": [{
                "params": {
                    "message": "文件：{{ event.payload.file.name }}",
                    "payload": "{{ event.payload }}",
                },
            }],
        }

        self.assertEqual(
            list(iter_legacy_event_payload_paths(rule)),
            [("file", "name"), ()],
        )

    def test_legacy_event_payload_resolves_with_manual_context(self):
        value = resolve_value(
            "文件：{{ event.payload.file.name }}",
            {"event": {"payload": {"file": {"name": "report.txt"}}}},
        )

        self.assertEqual(value, "文件：report.txt")


class StructuredBindingTests(unittest.TestCase):
    def test_conditional_trigger_source_is_valid_for_action(self):
        rule = {
            "condition": {
                "op": "any",
                "children": [
                    {"binding_id": "t_source1", "type": "source", "params": {}},
                    {"binding_id": "t_source2", "type": "source", "params": {}},
                ],
            },
            "actions": [{
                "binding_id": "a_target1",
                "type": "target",
                "params": {
                    "path": {
                        "$ref": {
                            "scope": "trigger",
                            "node": "t_source1",
                            "path": ["file_path"],
                        },
                    },
                },
            }],
        }
        issues = validate_rule_bindings(
            rule,
            {"source": {"outputs": [{
                "name": "file_path", "label": "文件", "type": "string",
            }]}},
            {"target": {"params": [{
                "name": "path", "label": "路径", "type": "path",
            }]}},
        )

        self.assertEqual(issues, [])

    def test_reference_availability_only_checks_source_participation(self):
        params = {
            "path": {
                "$ref": {
                    "scope": "trigger",
                    "node": "t_source1",
                    "path": ["file_path"],
                },
            },
        }

        self.assertFalse(references_available(params, {"triggers": {}, "steps": {}}))
        self.assertTrue(references_available(
            params,
            {"triggers": {"t_source1": {"payload": {}}}, "steps": {}},
        ))

    def test_action_with_unavailable_source_is_skipped(self):
        events = []

        class ExecutorStub:
            _on_event = staticmethod(lambda name, payload: events.append((name, payload)))
            _run_action = staticmethod(
                lambda action, rule_name, context: self.fail("动作不应执行")
            )

        context = {"triggers": {}, "steps": {}}
        action = {
            "binding_id": "a_reply01",
            "type": "reply",
            "params": {
                "message": {
                    "$ref": {
                        "scope": "trigger",
                        "node": "t_source1",
                        "path": ["message"],
                    },
                },
            },
        }

        executor = cast(WorkflowExecutor, ExecutorStub())
        WorkflowExecutor.execute_actions(executor, [action], "测试", context)

        self.assertEqual(context["steps"]["a_reply01"]["status"], "skipped")
        self.assertEqual(events[0][0], "action_skipped")

    def test_downstream_action_is_skipped_after_source_step_is_skipped(self):
        calls = []

        class ExecutorStub:
            _on_event = staticmethod(lambda name, payload: None)
            _run_action = staticmethod(
                lambda action, rule_name, context: calls.append(action["type"])
            )

        context = {"triggers": {}, "steps": {}}
        actions = [
            {
                "binding_id": "a_download",
                "type": "download",
                "params": {"url": {"$ref": {
                    "scope": "trigger", "node": "t_xianyu", "path": ["url"],
                }}},
            },
            {
                "binding_id": "a_reply01",
                "type": "reply",
                "params": {"path": {"$ref": {
                    "scope": "step", "node": "a_download", "path": ["file_path"],
                }}},
            },
        ]

        executor = cast(WorkflowExecutor, ExecutorStub())
        WorkflowExecutor.execute_actions(executor, actions, "测试", context)

        self.assertEqual(calls, [])
        self.assertEqual(context["steps"]["a_download"]["status"], "skipped")
        self.assertEqual(context["steps"]["a_reply01"]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
