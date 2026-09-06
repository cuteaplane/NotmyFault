from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from notmyfault.config import (
    ConfigValidationError,
    SignedConfigStore,
    ensure_rule_binding_ids,
    ensure_rule_id,
    validate_rules_safety,
)
from notmyfault.core.rules import (
    get_rule_events,
    iter_action_nodes,
    validate_rule_bindings,
    validate_rules,
    validate_rules_structure,
)
from notmyfault.security.rule_approval import (
    AdminRuleApprovalError,
    require_admin_rule_approval,
)


@dataclass(frozen=True, slots=True)
class RuleServiceError(Exception):
    status_code: int
    body: Dict[str, Any]


class RuleService:
    def __init__(
        self,
        store: SignedConfigStore,
        plugin_schema: Callable[[], Dict[str, Any]],
    ) -> None:
        self._store = store
        self._plugin_schema = plugin_schema

    def list_rules(self) -> Dict[str, Any]:
        try:
            rules = self._store.load_verified_rules(for_editing=True)
        except ConfigValidationError:
            rules = []
        return {"rules": rules}

    def validate_draft(self, rule: Any) -> Dict[str, Any]:
        issues: List[Dict[str, Any]] = []

        def add(
            severity: str,
            code: str,
            message: str,
            location: str = "",
        ) -> None:
            item = {"severity": severity, "code": code, "message": message}
            if location:
                item["location"] = location
            issues.append(item)

        structure_errors = validate_rules_structure([rule])
        if structure_errors:
            for message in structure_errors[:20]:
                add("error", "invalid_structure", message)
        else:
            normalized = ensure_rule_binding_ids(rule)
            schema = self._plugin_schema()
            _valid, _total, _plugin_errors, plugin_warnings = validate_rules(
                [normalized],
                schema["triggers"],
                schema["actions"],
            )
            for _rule_name, message in plugin_warnings:
                add("warning", "plugin_parameter", message)

            for event in get_rule_events(normalized):
                plugin = schema["triggers"].get(event.get("type", ""))
                self._add_plugin_availability_issue(
                    add,
                    plugin,
                    event.get("type", ""),
                    "触发器",
                    "event",
                )

            for action_item, location in iter_action_nodes(normalized.get("actions", [])):
                if action_item.get("type") in ("if", "set_variable"):
                    continue
                item_label = f"动作 {location}"
                plugin = schema["actions"].get(
                    action_item.get("type", "")
                )
                if plugin is None:
                    add(
                        "error",
                        "plugin_reference",
                        f"{item_label}引用了未加载的动作: "
                        f"{action_item.get('type', '')}",
                        location,
                    )
                elif plugin.get("platform_compatible") is False:
                    add(
                        "error",
                        "platform_incompatible",
                        f"{item_label}“{plugin.get('name') or action_item.get('type')}”"
                        "不支持当前系统",
                        location,
                    )
                elif plugin.get("availability") == "unavailable":
                    reasons = "；".join(
                        plugin.get("unavailable_reasons") or []
                    )
                    add(
                        "error",
                        "capability_incompatible",
                        f"{item_label}“{plugin.get('name') or action_item.get('type')}”"
                        f"当前系统缺少能力（{reasons}）",
                        location,
                    )
                elif (
                    action_item.get("timeout_seconds") is not None
                    and plugin.get("cancellation_api") != "runtime-v1"
                ):
                    add(
                        "error",
                        "timeout_not_supported",
                        f"{item_label}不支持安全取消，不能设置运行超时",
                        location,
                    )
                elif (
                    int(action_item.get("retry", 0) or 0) > 0
                    and plugin.get("idempotent") is not True
                ):
                    add(
                        "warning",
                        "retry_may_repeat",
                        f"{item_label}“{plugin.get('name') or action_item.get('type')}”"
                        "没有声明可安全重复执行，重试可能重复产生结果",
                        location,
                    )

            for issue in validate_rule_bindings(
                normalized,
                schema["triggers"],
                schema["actions"],
            ):
                add(
                    "error",
                    issue.get("code", "invalid_binding"),
                    issue.get("message", "规则数据绑定无效"),
                    issue.get("location", ""),
                )

            safety_warnings, safety_errors = validate_rules_safety([normalized])
            for message in safety_warnings:
                add("warning", "safety_warning", message)
            for message in safety_errors:
                add("error", "unsafe_action", message)

        unique_issues: list[Dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for issue in issues:
            key = (issue["severity"], issue["message"], issue.get("location", ""))
            if key not in seen:
                seen.add(key)
                unique_issues.append(issue)
        error_count = sum(item["severity"] == "error" for item in unique_issues)
        warning_count = sum(
            item["severity"] == "warning" for item in unique_issues
        )
        return {
            "ok": True,
            "valid": error_count == 0,
            "issues": unique_issues,
            "summary": {"errors": error_count, "warnings": warning_count},
        }

    def approve(self, rules: Any, admin_key_password: Any) -> Dict[str, Any]:
        if not isinstance(rules, list):
            raise RuleServiceError(
                400,
                {"ok": False, "error": "rules 必须是列表"},
            )
        try:
            require_admin_rule_approval(
                [],
                rules,
                self._plugin_schema(),
                admin_key_password,
            )
        except AdminRuleApprovalError as error:
            raise self._approval_error(error) from error
        return {"ok": True}

    def save(
        self,
        rules: Any,
        admin_key_password: Any,
    ) -> Dict[str, Any]:
        structure_errors = validate_rules_structure(rules)
        if structure_errors:
            raise RuleServiceError(
                400,
                {
                    "ok": False,
                    "error": "规则结构校验失败",
                    "details": structure_errors[:10],
                },
            )
        if not isinstance(rules, list):
            raise RuleServiceError(
                400,
                {"ok": False, "error": "rules 必须是列表"},
            )
        seen_rule_ids: set[str] = set()
        normalized_rules = [
            ensure_rule_binding_ids(ensure_rule_id(rule, seen_rule_ids))
            for rule in rules
        ]
        structure_errors = validate_rules_structure(normalized_rules)
        if structure_errors:
            raise RuleServiceError(
                400,
                {
                    "ok": False,
                    "error": "规则节点标识无效",
                    "details": structure_errors[:10],
                },
            )

        schema = self._plugin_schema()
        binding_issues = []
        for index, rule in enumerate(normalized_rules):
            for issue in validate_rule_bindings(
                rule,
                schema["triggers"],
                schema["actions"],
            ):
                binding_issues.append(
                    {
                        "rule": rule.get("name", f"规则 #{index + 1}"),
                        **issue,
                    }
                )
        if binding_issues:
            raise RuleServiceError(
                400,
                {
                    "ok": False,
                    "error": "规则数据绑定无效",
                    "details": binding_issues[:20],
                },
            )

        _warnings, errors = validate_rules_safety(normalized_rules)
        if errors:
            raise RuleServiceError(
                400,
                {
                    "ok": False,
                    "error": "规则安全校验失败",
                    "details": errors[:10],
                },
            )

        previous_rules = []
        if os.path.exists(self._store.rules_path):
            try:
                previous_rules = self._store.load_verified_rules(for_editing=True)
            except ConfigValidationError as error:
                raise RuleServiceError(
                    409,
                    {"ok": False, "error": "现有规则未通过完整性校验"},
                ) from error
        try:
            require_admin_rule_approval(
                previous_rules,
                normalized_rules,
                schema,
                admin_key_password,
            )
        except AdminRuleApprovalError as error:
            raise self._approval_error(error) from error

        if not self._store.save_rules(normalized_rules):
            raise RuleServiceError(
                500,
                {"ok": False, "error": "写入规则文件失败"},
            )
        return {"ok": True, "rules": normalized_rules}

    def _load_config(self) -> Dict[str, Any]:
        try:
            return self._store.load_verified_config()
        except ConfigValidationError:
            return {"rules": []}

    @staticmethod
    def _approval_error(error: AdminRuleApprovalError) -> RuleServiceError:
        return RuleServiceError(
            409,
            {
                "ok": False,
                "code": error.code,
                "error": str(error),
                "plugins": error.plugins,
            },
        )

    @staticmethod
    def _add_plugin_availability_issue(
        add: Callable[[str, str, str, str], None],
        plugin: Dict[str, Any] | None,
        plugin_id: str,
        label: str,
        location: str,
    ) -> None:
        if plugin is None:
            add(
                "error",
                "plugin_reference",
                f"引用了未加载的{label}: {plugin_id}",
                location,
            )
        elif plugin.get("platform_compatible") is False:
            add(
                "error",
                "platform_incompatible",
                f"{label}“{plugin.get('name') or plugin_id}”不支持当前系统",
                location,
            )
        elif plugin.get("availability") == "unavailable":
            reasons = "；".join(plugin.get("unavailable_reasons") or [])
            add(
                "error",
                "capability_incompatible",
                f"{label}“{plugin.get('name') or plugin_id}”"
                f"当前系统缺少能力（{reasons}）",
                location,
            )
