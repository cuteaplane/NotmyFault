import ast
import json
import threading
import unittest
from pathlib import Path
from typing import cast

from notmyfault.bindings import resolve_value
from notmyfault.engine import AutomationEngine
from notmyfault.plugin_schema import check_payload_contract, validate_plugin_meta
from notmyfault.rules import ConditionRuntime
from notmyfault.trigger_supervisor import TriggerSupervisor
from notmyfault.workflow import build_context


TRIGGERS_DIR = Path(__file__).resolve().parent / "notmyfault" / "triggers"


class TriggerV2Tests(unittest.TestCase):
    def test_supervisor_starts_one_v2_instance_per_config(self):
        started = []
        started_event = threading.Event()
        stopped = threading.Event()

        def run_trigger(instance_id, trigger_id, _func, _meta, config, stop_event):
            started.append((instance_id, trigger_id, config))
            if len(started) == 2:
                started_event.set()
            stop_event.wait(1)
            stopped.set()

        supervisor = TriggerSupervisor()
        count = supervisor.start(
            {"source": [{"name": "first"}, {"name": "second"}]},
            {"source": object()},
            {"source": {"trigger_api": "event-v2"}},
            run_trigger,
        )

        self.assertEqual(count, 2)
        self.assertTrue(started_event.wait(1))
        self.assertEqual({item[0] for item in started}, {"source:1", "source:2"})
        self.assertEqual({item[2]["name"] for item in started}, {"first", "second"})
        self.assertTrue(supervisor.stop())
        self.assertTrue(stopped.is_set())

    def test_engine_binds_v2_payload_to_plugin_trigger_id(self):
        events = []

        class EngineStub:
            emit_event = staticmethod(
                lambda trigger_id, payload, instance=None: events.append(
                    (trigger_id, payload, instance)
                )
            )

        def trigger(_meta, config, emit_event, _stop_event):
            emit_event({"value": config["value"]})

        AutomationEngine._run_trigger(
            cast(AutomationEngine, EngineStub()), "source:1", "source", trigger,
            {"trigger_api": "event-v2"}, {"value": 7}, threading.Event(),
        )

        self.assertEqual(len(events), 1)
        trigger_id, payload, instance = events[0]
        self.assertEqual(trigger_id, "source")
        self.assertEqual(payload, {"value": 7})
        self.assertEqual(instance, {"config": {"value": 7}})

    def test_v2_manifest_is_valid(self):
        valid, errors = validate_plugin_meta({
            "id": "source", "name": "Source", "description": "test",
            "enabled": True, "version_code": 1, "version": "1.0",
            "package_name": "com.example.source", "trigger_api": "event-v2",
        }, "trigger")

        self.assertTrue(valid, errors)

    def test_all_builtin_triggers_declare_the_v2_entrypoint(self):
        for manifest_path in TRIGGERS_DIR.glob("*/trigger.json"):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest.get("trigger_api"), "event-v2", manifest_path)
            module = ast.parse(manifest_path.with_name("trigger.py").read_text(encoding="utf-8"))
            run = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "run")
            self.assertEqual(
                [argument.arg for argument in run.args.args],
                ["meta", "config", "emit_event", "shutdown_event"],
                manifest_path,
            )


class V2ContractTests(unittest.TestCase):
    OUTPUTS = [
        {"name": "text", "label": "内容", "type": "string", "sensitive": True},
        {"name": "count", "label": "数量", "type": "number"},
        {"name": "note", "label": "备注", "type": "string", "required": False},
    ]

    def test_payload_matching_outputs_passes(self):
        self.assertEqual(check_payload_contract(self.OUTPUTS, {
            "text": "hello", "count": 3,
        }), [])

    def test_missing_required_output_is_rejected(self):
        problems = check_payload_contract(self.OUTPUTS, {"text": "hello"})
        self.assertTrue(any("count" in problem for problem in problems))

    def test_unknown_field_is_rejected(self):
        problems = check_payload_contract(self.OUTPUTS, {
            "text": "hello", "count": 3, "mystery": True,
        })
        self.assertTrue(any("mystery" in problem for problem in problems))

    def test_wrong_simple_type_is_rejected(self):
        problems = check_payload_contract(self.OUTPUTS, {
            "text": 5, "count": 3,
        })
        self.assertTrue(any("text" in problem for problem in problems))
        self.assertTrue(any("string" in problem for problem in problems))

    def test_optional_missing_is_allowed(self):
        self.assertEqual(check_payload_contract(self.OUTPUTS, {
            "text": "hello", "count": 3,
        }), [])

    def test_engine_blocks_contract_violating_payload(self):
        events = []
        blocked = []

        class EngineStub:
            def emit_event(self, trigger_id, payload, instance=None):
                events.append((trigger_id, payload, instance))

            def _safe_on_event(self, event_type, payload):
                blocked.append((event_type, payload))

        def trigger(_meta, config, emit_event, _stop_event):
            emit_event({"count": 1})
            emit_event({"text": "ok", "count": 1})

        AutomationEngine._run_trigger(
            cast(AutomationEngine, EngineStub()), "source:1", "source", trigger,
            {"trigger_api": "event-v2", "outputs": self.OUTPUTS},
            {"value": 7}, threading.Event(),
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1], {"text": "ok", "count": 1})
        self.assertTrue(any(event_type == "trigger_payload_invalid" for event_type, _ in blocked))


class V2MatchingTests(unittest.TestCase):
    RULE = {
        "name": "双配置规则",
        "condition": {
            "op": "any",
            "children": [
                {"type": "source", "binding_id": "t_first01", "params": {"name": "first"}},
                {"type": "source", "binding_id": "t_second1", "params": {"name": "second"}},
            ],
        },
        "actions": [{"type": "sink", "binding_id": "a_sink0001", "params": {}}],
    }

    def _rule(self):
        return json.loads(json.dumps(self.RULE))

    def test_v2_config_matching_hits_own_leaf(self):
        runtime = ConditionRuntime()
        rule = self._rule()

        hit = runtime.match(
            "k", rule, "source", {}, instance={"config": {"name": "first"}}
        )
        self.assertTrue(hit)
        last = runtime.last_match("k")
        self.assertEqual(last[0]["binding_id"], "t_first01")

        hit = runtime.match(
            "k", rule, "source", {}, instance={"config": {"name": "second"}}
        )
        self.assertTrue(hit)
        last = runtime.last_match("k")
        self.assertEqual(last[0]["binding_id"], "t_second1")

    def test_v2_config_mismatch_does_not_hit(self):
        runtime = ConditionRuntime()
        rule = self._rule()
        self.assertFalse(runtime.match(
            "k", rule, "source", {}, instance={"config": {"name": "other"}}
        ))
        self.assertFalse(runtime.match(
            "k", rule, "other_type", {}, instance={"config": {"name": "first"}}
        ))

    def test_v1_payload_filtering_still_works(self):
        runtime = ConditionRuntime()
        rule = {
            "name": "v1 规则",
            "condition": {
                "op": "any",
                "children": [
                    {"type": "legacy", "binding_id": "t_legacy01", "params": {"kind": "a"}},
                ],
            },
            "actions": [{"type": "sink", "binding_id": "a_sink0001", "params": {}}],
        }
        self.assertTrue(runtime.match("k", rule, "legacy", {"kind": "a"}))
        self.assertFalse(runtime.match("k", rule, "legacy", {"kind": "b"}))

    def test_v2_and_v1_semantics_coexist_in_one_rule(self):
        runtime = ConditionRuntime()
        rule = {
            "name": "混合规则",
            "condition": {
                "op": "all",
                "children": [
                    {"type": "source", "binding_id": "t_v2id0001", "params": {"name": "first"}},
                    {"type": "legacy", "binding_id": "t_v1id0001", "params": {"kind": "a"}},
                ],
            },
            "actions": [{"type": "sink", "binding_id": "a_sink0001", "params": {}}],
        }
        # v2 事件（带 instance）只喂 v2 叶子，all 组合缺 v1 侧 → 未成立
        self.assertFalse(runtime.match(
            "k", rule, "source", {}, instance={"config": {"name": "first"}}
        ))
        # v1 事件（无 instance）按 payload 过滤命中 v1 叶子，组合成立
        self.assertTrue(runtime.match("k", rule, "legacy", {"kind": "a"}))
        last = runtime.last_match("k")
        self.assertEqual({item["binding_id"] for item in last}, {"t_v2id0001", "t_v1id0001"})


class TriggerConfigBindingTests(unittest.TestCase):
    def test_context_snapshots_trigger_config(self):
        context = build_context("测试", "source", {"x": 1}, [
            {
                "binding_id": "t_first01",
                "event": {"type": "source", "params": {"name": "first"}},
                "payload": {"x": 1},
            },
        ])
        self.assertEqual(context["triggers"]["t_first01"]["config"], {"name": "first"})

    def test_trigger_config_reference_resolves(self):
        context = build_context("测试", "source", {"x": 1}, [
            {
                "binding_id": "t_first01",
                "event": {"type": "source", "params": {"name": "first"}},
                "payload": {"x": 1},
            },
        ])
        value = resolve_value(
            {"$ref": {"scope": "trigger_config", "node": "t_first01", "path": ["name"]}},
            context,
            location="$",
        )
        self.assertEqual(value, "first")

    def test_trigger_config_missing_config_reports_error(self):
        context = build_context("测试", "source", {"x": 1}, [])
        with self.assertRaises(Exception):
            resolve_value(
                {"$ref": {"scope": "trigger_config", "node": "t_absent1", "path": ["name"]}},
                context,
                location="$",
            )


if __name__ == "__main__":
    unittest.main()
