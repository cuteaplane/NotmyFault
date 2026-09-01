"""严格模式下管理员规则的私钥确认。"""

from typing import Any, Dict, List

from notmyfault.core.rules import get_rule_admin_plugins
from notmyfault.security.plugin_schema import requires_admin_rule_approval
from notmyfault.security.security import SecurityMode, detect_security_mode
from notmyfault.security.signing import (
    PRIVATE_KEY_FILE,
    key_status,
    load_private_key,
)


class AdminRuleApprovalError(ValueError):
    def __init__(self, code: str, message: str, plugins: List[str]) -> None:
        super().__init__(message)
        self.code = code
        self.plugins = plugins


def _rule_approval_plugins(
    rule: Dict[str, Any],
    schema: Dict[str, Dict[str, Dict[str, Any]]],
) -> List[str]:
    triggers_meta = schema.get("triggers", {})
    actions_meta = schema.get("actions", {})
    required = set(get_rule_admin_plugins(rule, triggers_meta, actions_meta))
    for field in ("preconditions", "actions"):
        items = rule.get(field, [])
        if not isinstance(items, list):
            continue
        pending = list(items)
        while pending:
            item = pending.pop()
            if not isinstance(item, dict):
                continue
            plugin_id = item.get("type")
            if requires_admin_rule_approval(actions_meta.get(plugin_id, {})):
                required.add(plugin_id)
            failure_actions = item.get("failure_actions", [])
            if isinstance(failure_actions, list):
                pending.extend(failure_actions)
    return sorted(required)


def changed_admin_plugins(
    previous_rules: List[Dict[str, Any]],
    next_rules: List[Dict[str, Any]],
    schema: Dict[str, Dict[str, Dict[str, Any]]],
) -> List[str]:
    """返回新增或内容有变化的管理员规则所引用的插件。"""
    previous_by_id = {
        rule.get("rule_id"): rule
        for rule in previous_rules
        if isinstance(rule, dict) and isinstance(rule.get("rule_id"), str)
    }
    required = set()
    for rule in next_rules:
        if not isinstance(rule, dict):
            continue
        plugins = _rule_approval_plugins(rule, schema)
        if not plugins:
            continue
        previous = previous_by_id.get(rule.get("rule_id"))
        if previous != rule:
            required.update(plugins)
    return sorted(required)


def verify_admin_key_password(password: str | None, required: List[str]) -> None:
    """验证加密签名私钥，并使用管理员审批的错误契约报告失败。"""
    try:
        status = key_status()
    except OSError as error:
        raise AdminRuleApprovalError(
            "admin_key_unreadable",
            "无法读取签名私钥",
            required,
        ) from error
    if not status["exists"]:
        raise AdminRuleApprovalError(
            "admin_key_missing",
            "严格模式缺少签名私钥，无法保存需要管理员权限的规则",
            required,
        )
    if not status["encrypted"]:
        raise AdminRuleApprovalError(
            "admin_key_unencrypted",
            "严格模式要求使用密码加密的签名私钥",
            required,
        )
    if not isinstance(password, str) or not password:
        raise AdminRuleApprovalError(
            "admin_key_required",
            "这条规则可能调用管理员权限或执行高危命令，请输入签名私钥密码",
            required,
        )

    try:
        private_key = load_private_key(PRIVATE_KEY_FILE, password=password)
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from notmyfault.security.signing_keys import get_public_keys

        public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        trusted = public_key in get_public_keys()
    except Exception:
        trusted = False
    if not trusted:
        raise AdminRuleApprovalError(
            "admin_key_invalid",
            "私钥密码错误，或私钥与当前 NotmyFault 签名公钥不匹配",
            required,
        )


def require_admin_rule_approval(
    previous_rules: List[Dict[str, Any]],
    next_rules: List[Dict[str, Any]],
    schema: Dict[str, Dict[str, Dict[str, Any]]],
    password: str | None,
) -> List[str]:
    """检查受限插件的规则改动，成功时返回涉及的插件。"""
    if detect_security_mode() != SecurityMode.STRICT:
        return []
    required = changed_admin_plugins(previous_rules, next_rules, schema)
    if not required:
        return []

    verify_admin_key_password(password, required)
    return required
