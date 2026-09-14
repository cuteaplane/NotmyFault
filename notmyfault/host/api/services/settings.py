from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Callable, Dict, Literal

from notmyfault.config import ConfigValidationError, SignedConfigStore
from notmyfault.security.rule_approval import AdminRuleApprovalError
from notmyfault.security.plugin_checks import inspect_plugin_tree, inspect_signature
from notmyfault.security.security import (
    SecurityMode, detect_security_mode, verify_core_integrity,
)
from notmyfault.security.signing import verify_file


@dataclass(slots=True)
class SettingsServiceError(Exception):
    kind: Literal["invalid", "integrity", "write", "approval"]
    message: str
    code: str | None = None

    def __str__(self) -> str:
        return self.message


class SettingsService:
    def __init__(
        self,
        store: SignedConfigStore,
        plugin_schema: Callable[[], Dict[str, Any]] | None = None,
    ) -> None:
        self._store = store
        self._plugin_schema = plugin_schema or (
            lambda: {"triggers": {}, "actions": {}}
        )

    def security_status(self) -> Dict[str, Any]:
        result = self.security_summary()
        mode = detect_security_mode()
        paths = self._store.paths
        frozen = getattr(sys, "frozen", False)
        build_root = Path(sys._MEIPASS) if frozen else paths.project_root
        issues = []
        if not verify_file(str(build_root / "build.json")):
            issues.append({"path": "build.json", "reason": "构建信息缺失或签名无效"})
        checked = 0
        for kind, filename in (("actions", "action.json"), ("triggers", "trigger.json")):
            for metadata in sorted((paths.package_root / kind).glob(f"*/{filename}")):
                checked += 1
                tree = inspect_plugin_tree(str(metadata.parent))
                if tree is None or inspect_signature(metadata.parent, "builtin", tree).kind == "none":
                    issues.append({
                        "path": metadata.parent.relative_to(paths.package_root).as_posix(),
                        "reason": "内置插件签名缺失或无效",
                    })
        core_checked = mode == SecurityMode.STRICT and not frozen
        if core_checked:
            valid, files = verify_core_integrity()
            if not valid:
                issues.extend({"path": path, "reason": "核心文件完整性校验失败"} for path in files)
        result["installation"] = {
            "status": "invalid" if issues else "ok",
            "issues": issues,
            "checked_plugins": checked,
            "core_checked": core_checked,
        }
        result["security_mode"] = mode.value
        result["config_dir"] = str(paths.config_dir)
        return result

    def security_summary(self) -> Dict[str, Any]:
        status = self._store.inspect_files()
        rules = status.pop("rules", [])
        if status.get("status") == "unreadable":
            return status
        from notmyfault.security.plugin_schema import requires_admin_rule_approval

        action_schema = self._plugin_schema().get("actions", {})

        def summarize_params(params: Any) -> Dict[str, Any]:
            if not isinstance(params, dict):
                return {}
            return {
                str(key): "***" if value not in (None, "") else ""
                for key, value in params.items()
            }

        def summarize_item(item: Any) -> Dict[str, Any]:
            if not isinstance(item, dict):
                return {"type": "?", "high_risk": False, "params": {}}
            action_type = item.get("type", "?")
            summary = {
                "type": action_type,
                "high_risk": requires_admin_rule_approval(
                    action_schema.get(action_type, {})
                ),
                "params": summarize_params(item.get("params")),
            }
            child_fields = ("then", "else", "failure_actions") if action_type == "if" else ("failure_actions",)
            for field in child_fields:
                children = item.get(field, [])
                if isinstance(children, list) and (children or field in ("then", "else")):
                    summary[field] = [summarize_item(child) for child in children]
                    summary["high_risk"] |= any(child["high_risk"] for child in summary[field])
            return summary

        status["summary"] = {
            "rule_count": len(rules),
            "rules": [
                {
                    "name": (
                        rule.get("name", f"规则 #{index + 1}")
                        if isinstance(rule, dict)
                        else f"规则 #{index + 1}"
                    ),
                    "preconditions": (
                        [
                            summarize_item(item)
                            for item in rule.get("preconditions", [])
                        ]
                        if isinstance(rule, dict)
                        and isinstance(rule.get("preconditions", []), list)
                        else []
                    ),
                    "actions": (
                        [summarize_item(action) for action in rule.get("actions", [])]
                        if isinstance(rule, dict)
                        and isinstance(rule.get("actions", []), list)
                        else []
                    ),
                }
                for index, rule in enumerate(rules)
            ],
        }
        return status

    def _approve_files(self, admin_key_password: str | None) -> None:
        normalized_config, normalized_rules = self._store.load_unsigned_files()
        schema = self._plugin_schema()
        if normalized_rules is not None:
            from notmyfault.core.rules import (
                validate_rule_bindings,
                validate_rules_structure,
            )

            structure_errors = validate_rules_structure(normalized_rules)
            if structure_errors:
                raise ConfigValidationError(
                    "规则包含结构无效的规则，拒绝重新签名: "
                    + "; ".join(structure_errors[:3])
                )
            binding_errors = []
            for index, rule in enumerate(normalized_rules):
                for issue in validate_rule_bindings(
                    rule,
                    schema.get("triggers", {}),
                    schema.get("actions", {}),
                ):
                    binding_errors.append(
                        f"规则 #{index + 1} {issue.get('message', '数据绑定无效')}"
                    )
            if binding_errors:
                raise ConfigValidationError(
                    "规则数据绑定无效，拒绝重新签名: "
                    + "; ".join(binding_errors[:3])
                )

            from notmyfault.security.rule_approval import (
                require_admin_rule_approval,
            )

            require_admin_rule_approval(
                [],
                normalized_rules,
                schema,
                admin_key_password,
            )

        if not self._store.save_config(normalized_config):
            raise ConfigValidationError("重新签名失败")
        if normalized_rules is not None and not self._store.save_rules(normalized_rules):
            raise ConfigValidationError("规则重新签名失败")

    def approve_security(self, admin_key_password: Any = None) -> Dict[str, Any]:
        try:
            self._approve_files(
                admin_key_password if isinstance(admin_key_password, str) else None,
            )
        except AdminRuleApprovalError as error:
            raise SettingsServiceError(
                "approval",
                str(error),
                code=error.code,
            ) from error
        except ConfigValidationError as error:
            raise SettingsServiceError(
                "invalid",
                str(error),
            ) from error
        return {"ok": True, "message": "设置与规则已重新签名"}
