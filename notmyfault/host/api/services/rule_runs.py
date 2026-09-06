from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List

from notmyfault.config import ConfigValidationError, SignedConfigStore
from notmyfault.core.bindings import BindingResolutionError, _validate_path, iter_legacy_event_payload_paths, iter_references, resolve_reference
from notmyfault.core.data_types import DataTypeError
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.variables import initialize_variables
from notmyfault.core.rules import get_rule_events, validate_rules_structure
from notmyfault.host.api.ports import EngineControlPort
from notmyfault.security.plugin_schema import check_payload_contract


_TEST_ASSERTION_MAX_COUNT = 50
_TEST_ASSERTION_OPERATORS = frozenset(
    {"equals", "contains", "gt", "gte", "lt", "lte", "exists"}
)


@dataclass(frozen=True, slots=True)
class RuleRunServiceError(Exception):
    status_code: int
    body: Dict[str, Any]


class RuleRunService:
    def __init__(
        self,
        engine: EngineControlPort,
        store: SignedConfigStore,
    ) -> None:
        self._engine = engine
        self._store = store

    def run(
        self,
        rule_index: int,
        body: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        engine = self._engine.current_engine
        if engine is None:
            self._fail(409, "引擎尚未就绪")
        try:
            verified_rules = self._store.load_verified_rules()
        except ConfigValidationError as error:
            raise RuleRunServiceError(
                409,
                {"ok": False, "error": "规则未通过完整性校验"},
            ) from error

        body = body or {}
        has_snapshot = "rule" in body
        rule_snapshot = body.get("rule")
        trigger_payloads = (
            body.get("trigger_payloads")
            if isinstance(body.get("trigger_payloads"), dict)
            else {}
        )
        event_payload = (
            body.get("event_payload")
            if isinstance(body.get("event_payload"), dict)
            else None
        )
        step_outputs = body.get("step_outputs", {})
        start_step_id = body.get("start_step_id", "")
        end_step_id = body.get("end_step_id", "")
        test_assertions = body.get("test_assertions", [])
        variable_values = body.get("variable_values")

        candidate_rule = self._select_rule(
            verified_rules,
            rule_index,
            has_snapshot,
            rule_snapshot,
        )
        try:
            registry = TypeRegistry.from_plugins(engine.triggers_meta, engine.actions_meta)
            initialize_variables(candidate_rule, {}, registry, overrides=variable_values)
        except (DataTypeError, BindingResolutionError) as error:
            raise RuleRunServiceError(400, {"ok": False, "code": "invalid_test_payload", "error": str(error)}) from error
        actions = candidate_rule.get("actions", [])
        if not isinstance(actions, list):
            actions = []
        action_ids = [
            action.get("binding_id", "") if isinstance(action, dict) else ""
            for action in actions
        ]
        if not isinstance(start_step_id, str) or not isinstance(end_step_id, str):
            self._fail(400, "局部运行步骤 ID 必须是字符串")
        if start_step_id and start_step_id not in action_ids:
            self._fail(400, "局部运行的起始动作不存在")
        if end_step_id and end_step_id not in action_ids:
            self._fail(400, "局部运行的结束动作不存在")
        start_index = action_ids.index(start_step_id) if start_step_id else 0
        end_index = (
            action_ids.index(end_step_id) if end_step_id else len(actions) - 1
        )
        if actions and start_index > end_index:
            self._fail(400, "局部运行的起始动作不能晚于结束动作")
        selected_actions = actions[start_index : end_index + 1] if actions else []
        selected_ids = set(action_ids[start_index : end_index + 1])
        skipped_upstream_ids = set(action_ids[:start_index])

        if not isinstance(step_outputs, dict):
            self._fail(400, "上游动作结果必须是 JSON 对象")
        invalid_step_output_ids = sorted(
            key
            for key in step_outputs
            if not isinstance(key, str) or key not in skipped_upstream_ids
        )
        if invalid_step_output_ids:
            raise RuleRunServiceError(
                400,
                {
                    "ok": False,
                    "code": "invalid_test_payload",
                    "error": "上游结果只能对应本次跳过的前置动作",
                    "details": invalid_step_output_ids[:10],
                },
            )

        execution_source = {
            "preconditions": candidate_rule.get("preconditions", []),
            "actions": selected_actions,
        }
        references = list(iter_references(execution_source))
        required_upstream_ids = sorted(
            {
                node
                for usage in references
                if usage.reference.get("scope") == "step"
                and isinstance(node := usage.reference.get("node"), str)
                and node in skipped_upstream_ids
                and usage.reference.get("on_missing") not in ("default", "skip")
            }
        )
        missing_upstream_ids = [
            step_id
            for step_id in required_upstream_ids
            if step_id not in step_outputs
        ]
        if missing_upstream_ids:
            raise RuleRunServiceError(
                400,
                {
                    "ok": False,
                    "code": "missing_test_context",
                    "error": "从中间动作继续时需要提供被跳过的上游结果",
                    "required_step_ids": missing_upstream_ids,
                },
            )

        upstream_issues = self._validate_upstream_outputs(
            engine.actions_meta,
            actions,
            references,
            skipped_upstream_ids,
            step_outputs,
            registry,
        )
        if upstream_issues:
            raise RuleRunServiceError(
                400,
                {
                    "ok": False,
                    "code": "invalid_test_payload",
                    "error": "上游动作结果不符合引用字段契约",
                    "details": upstream_issues[:10],
                },
            )

        normalized_assertions = self._normalize_assertions(
            test_assertions,
            selected_ids,
        )
        legacy_event_paths = list(iter_legacy_event_payload_paths(execution_source))
        required_trigger_ids = sorted(
            {
                binding_id
                for usage in references
                if usage.reference.get("scope") == "trigger"
                and usage.reference.get("on_missing") not in ("default", "skip")
                and isinstance(
                    binding_id := usage.reference.get("node"),
                    str,
                )
            }
        )
        missing_trigger_ids = [
            binding_id
            for binding_id in required_trigger_ids
            if not isinstance(trigger_payloads.get(binding_id), dict)
        ]
        needs_event = any(
            usage.reference.get("scope") == "event" and usage.reference.get("on_missing") not in ("default", "skip") for usage in references
        ) or bool(legacy_event_paths)
        if missing_trigger_ids or (needs_event and event_payload is None):
            raise RuleRunServiceError(
                400,
                {
                    "ok": False,
                    "code": "missing_test_context",
                    "error": "测试规则需要提供触发时产生的数据",
                    "required_trigger_ids": missing_trigger_ids,
                    "event_payload_required": needs_event and event_payload is None,
                },
            )

        trigger_issues = self._validate_trigger_payloads(
            engine.triggers_meta,
            candidate_rule,
            references,
            trigger_payloads,
            registry,
        )
        if trigger_issues:
            raise RuleRunServiceError(
                400,
                {
                    "ok": False,
                    "code": "invalid_test_payload",
                    "error": "测试数据不符合触发器输出契约",
                    "details": trigger_issues[:10],
                },
            )

        result = engine.run_manual_rule_snapshot(
            candidate_rule,
            rule_index,
            trigger_payloads=trigger_payloads,
            event_payload=event_payload,
            step_outputs=step_outputs,
            start_step_id=start_step_id,
            end_step_id=end_step_id,
            test_assertions=normalized_assertions,
            **({"variable_values": variable_values} if variable_values is not None else {}),
        )
        ok, message = result[:2]
        run_id = result[2] if len(result) > 2 else ""
        if not ok:
            self._fail(400, message)
        return {
            "ok": True,
            "message": message,
            "run_id": run_id,
            "action_count": len(selected_actions),
        }

    def _select_rule(
        self,
        rules: list[Dict[str, Any]],
        rule_index: int,
        has_snapshot: bool,
        snapshot: Any,
    ) -> Dict[str, Any]:
        if has_snapshot:
            structure_errors = validate_rules_structure([snapshot])
            if structure_errors:
                raise RuleRunServiceError(
                    400,
                    {
                        "ok": False,
                        "error": "规则结构校验失败",
                        "details": structure_errors[:10],
                    },
                )
            if (
                rule_index < 0
                or rule_index >= len(rules)
                or rules[rule_index] != snapshot
            ):
                self._fail(409, "规则保存版本已变化，请刷新后重试")
            return snapshot
        if rule_index < 0 or rule_index >= len(rules):
            self._fail(404, "规则不存在")
        return rules[rule_index]

    def _normalize_assertions(
        self,
        assertions: Any,
        selected_ids: set[str],
    ) -> list[Dict[str, Any]]:
        if not isinstance(assertions, list) or len(assertions) > _TEST_ASSERTION_MAX_COUNT:
            self._fail(400, "测试结果检查必须是数组，且不能超过 50 条")
        normalized_assertions = []
        for index, assertion in enumerate(assertions):
            if not isinstance(assertion, dict):
                self._fail(400, f"检查项 #{index + 1} 必须是对象")
            step_id = assertion.get("step_id")
            path = assertion.get("path", [])
            operator = assertion.get("operator")
            try:
                _validate_path(path, f"test_assertions[{index}]", {})
            except BindingResolutionError:
                self._fail(400, f"检查项 #{index + 1} 的数据路径无效")
            if (
                step_id not in selected_ids
                or operator not in _TEST_ASSERTION_OPERATORS
                or not isinstance(path, list)
            ):
                self._fail(400, f"检查项 #{index + 1} 无效")
            normalized = {"step_id": step_id, "path": path, "operator": operator}
            if operator != "exists":
                if "expected" not in assertion:
                    self._fail(400, f"检查项 #{index + 1} 缺少期望值")
                normalized["expected"] = assertion["expected"]
            normalized_assertions.append(normalized)
        return normalized_assertions

    @staticmethod
    def _validate_upstream_outputs(
        actions_meta: Dict[str, Any],
        actions: list[Any],
        references: list[Any],
        skipped_ids: set[str],
        step_outputs: Dict[str, Any],
        registry=None,
    ) -> List[str]:
        issues: List[str] = []
        actions_by_id = {
            action.get("binding_id"): action
            for action in actions
            if isinstance(action, dict)
            and isinstance(action.get("binding_id"), str)
        }
        for usage in references:
            reference = usage.reference
            step_id = reference.get("node")
            if reference.get("scope") != "step" or step_id not in skipped_ids:
                continue
            path = reference.get("path", [])
            try:
                resolve_reference(reference, {"steps": {key: {"status": "ok", "result": value} for key, value in step_outputs.items()}, "_type_registry": registry}, location=usage.location)
            except BindingResolutionError as error:
                if reference.get("on_missing") not in ("skip", "default"):
                    issues.append(f"{step_id}: 缺少动作输出字段 {path}")
                continue
            if not path or step_id not in step_outputs:
                continue
            source_action = actions_by_id.get(step_id, {})
            source_meta = actions_meta.get(source_action.get("type", ""), {})
            outputs = source_meta.get("outputs", [])
            output = (
                next(
                    (
                        item
                        for item in outputs
                        if isinstance(item, dict) and item.get("name") == path[0]
                    ),
                    None,
                )
                if isinstance(outputs, list)
                else None
            )
            if output is not None:
                supplied_output = step_outputs[step_id]
                root_payload = (
                    {path[0]: supplied_output[path[0]]}
                    if isinstance(supplied_output, dict) and path[0] in supplied_output
                    else {}
                )
                for problem in check_payload_contract(
                    [output],
                    root_payload,
                    registry,
                ):
                    issues.append(f"{step_id}: {problem}")
        return issues

    @staticmethod
    def _validate_trigger_payloads(
        triggers_meta: Dict[str, Any],
        rule: Dict[str, Any],
        references: list[Any],
        trigger_payloads: Dict[str, Dict[str, Any]],
        registry=None,
    ) -> List[str]:
        leaves_by_id = {
            leaf.get("binding_id"): leaf
            for leaf in get_rule_events(rule)
            if isinstance(leaf.get("binding_id"), str)
        }
        unknown_ids = sorted(set(trigger_payloads) - set(leaves_by_id))
        invalid_ids = sorted(
            binding_id
            for binding_id, payload in trigger_payloads.items()
            if not isinstance(payload, dict)
        )
        if unknown_ids or invalid_ids:
            details = []
            if unknown_ids:
                details.append("未知触发器: " + ", ".join(unknown_ids[:5]))
            if invalid_ids:
                details.append("触发数据必须是对象: " + ", ".join(invalid_ids[:5]))
            raise RuleRunServiceError(
                400,
                {
                    "ok": False,
                    "code": "invalid_test_payload",
                    "error": "测试数据包含无效的触发来源",
                    "details": details,
                },
            )
        issues: List[str] = []
        for usage in references:
            reference = usage.reference
            if reference.get("scope") != "trigger":
                continue
            binding_id = reference.get("node")
            if not isinstance(binding_id, str):
                continue
            payload = trigger_payloads.get(binding_id)
            if not isinstance(payload, dict):
                continue
            leaf = leaves_by_id.get(binding_id)
            if leaf is None:
                continue
            trigger_meta = triggers_meta.get(leaf.get("type", ""), {})
            for problem in check_payload_contract(trigger_meta.get("outputs"), payload, registry):
                issues.append(f"{binding_id}: {problem}")
        return issues

    @staticmethod
    def _fail(status_code: int, message: str) -> None:
        raise RuleRunServiceError(
            status_code,
            {"ok": False, "error": message},
        )
