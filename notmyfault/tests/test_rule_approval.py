from copy import deepcopy

import pytest

from notmyfault.security import rule_approval
from notmyfault.security.security import SecurityMode
from notmyfault.tests.canvas_fixtures import native_rule


SCHEMA = {
    "triggers": {"plain_trigger": {"permissions": []}},
    "actions": {
        "plain_action": {"permissions": []},
        "admin_action": {"permissions": ["admin"]},
        "run_powershell": {
            "permissions": ["external_binary"],
            "security": {"rule_approval": "admin_key"},
        },
        "kill_process": {
            "permissions": ["process"],
            "security": {"rule_approval": "admin_key"},
        },
    },
}


def admin_rule():
    return native_rule({"type": "plain_trigger", "params": {}}, [{"type": "admin_action", "params": {"state": "on"}}], rule_id="r_admin001", name="管理员规则")


def plain_rule():
    return native_rule({"type": "plain_trigger", "params": {}}, [{"type": "plain_action", "params": {}}], rule_id="r_plain001", name="普通规则")


def high_risk_rule():
    return native_rule({"type": "plain_trigger", "params": {}}, [{"type": "run_powershell", "params": {"command": "Get-Service"}}], rule_id="r_risk001", name="高危规则")


def attach_recovery(rule, plugin_id):
    graph = rule["workflow"]
    graph["nodes"] += [
        {"binding_id": "a_recovery001", "type": "action_group"},
        {"binding_id": "a_handler001", "type": plugin_id, "parent": "a_recovery001", "params": {}},
    ]
    graph["edges"] += [
        {"source": graph["nodes"][1]["binding_id"], "port": "failure", "target": "a_recovery001", "channel": "control"},
        {"source": "a_recovery001", "port": "entry", "target": "a_handler001", "channel": "control"},
    ]


def test_changed_admin_plugins_ignores_unrelated_rule_edits():
    previous = [admin_rule(), plain_rule()]
    changed_plain = deepcopy(previous)
    changed_plain[1]["name"] = "改名后的普通规则"

    assert rule_approval.changed_admin_plugins(previous, changed_plain, SCHEMA) == []

    changed_admin = deepcopy(previous)
    changed_admin[0]["workflow"]["nodes"][1]["params"]["state"] = "off"
    assert rule_approval.changed_admin_plugins(
        previous, changed_admin, SCHEMA,
    ) == ["admin_action"]


def test_changed_admin_plugins_finds_failure_action():
    rule = plain_rule()
    attach_recovery(rule, "admin_action")

    assert rule_approval.changed_admin_plugins([], [rule], SCHEMA) == ["admin_action"]


def test_admin_approval_includes_nested_elif_actions():
    rule = plain_rule()
    graph = rule["workflow"]
    outer = {"binding_id": "a_outer001", "type": "event_group", "group_type": "if", "branches": [
        {"id": "b_first001"}, {"id": "b_elif001"}, {"id": "b_else001", "else": True},
    ]}
    inner = {"binding_id": "a_inner001", "type": "event_group", "group_type": "if", "parent": outer["binding_id"], "branches": [
        {"id": "b_first002"}, {"id": "b_elif002"}, {"id": "b_else002", "else": True},
    ]}
    graph["nodes"] += [outer, inner,
        {"type": "admin_action", "binding_id": "a_restricted001", "parent": inner["binding_id"], "params": {}},
        {"type": "kill_process", "binding_id": "a_restricted002", "parent": inner["binding_id"], "params": {}},
    ]
    graph["edges"] += [
        {"source": outer["binding_id"], "port": "b_elif001", "target": inner["binding_id"], "channel": "control"},
        {"source": inner["binding_id"], "port": "b_elif002", "target": "a_restricted001", "channel": "control"},
        {"source": "a_restricted001", "port": "next", "target": "a_restricted002", "channel": "control"},
    ]
    assert rule_approval.changed_admin_plugins([], [rule], SCHEMA) == ["admin_action", "kill_process"]


def test_changed_admin_plugins_finds_manifest_restricted_action():
    assert rule_approval.changed_admin_plugins(
        [], [high_risk_rule()], SCHEMA,
    ) == ["run_powershell"]


def test_changed_admin_plugins_finds_restricted_failure_action():
    rule = plain_rule()
    attach_recovery(rule, "kill_process")
    assert rule_approval.changed_admin_plugins([], [rule], SCHEMA) == ["kill_process"]


def test_permissive_mode_does_not_request_private_key(monkeypatch):
    monkeypatch.setattr(
        rule_approval, "detect_security_mode", lambda: SecurityMode.PERMISSIVE,
    )

    assert rule_approval.require_admin_rule_approval(
        [], [admin_rule()], SCHEMA, None,
    ) == []


def test_strict_mode_requests_private_key_password(monkeypatch):
    monkeypatch.setattr(
        rule_approval, "detect_security_mode", lambda: SecurityMode.STRICT,
    )
    monkeypatch.setattr(
        rule_approval, "key_status", lambda: {"exists": True, "encrypted": True},
    )

    with pytest.raises(rule_approval.AdminRuleApprovalError) as caught:
        rule_approval.require_admin_rule_approval(
            [], [admin_rule()], SCHEMA, None,
        )

    assert caught.value.code == "admin_key_required"
    assert caught.value.plugins == ["admin_action"]


def test_private_key_read_error_does_not_expose_system_details(monkeypatch):
    def fail_key_status():
        raise OSError("secret-key-path")

    monkeypatch.setattr(rule_approval, "key_status", fail_key_status)

    with pytest.raises(rule_approval.AdminRuleApprovalError) as caught:
        rule_approval.verify_admin_key_password("password", ["admin_action"])

    assert caught.value.code == "admin_key_unreadable"
    assert str(caught.value) == "无法读取签名私钥"
    assert "secret-key-path" not in str(caught.value)


def test_strict_mode_requires_password_for_high_risk_action(monkeypatch):
    monkeypatch.setattr(
        rule_approval, "detect_security_mode", lambda: SecurityMode.STRICT,
    )
    monkeypatch.setattr(
        rule_approval, "key_status", lambda: {"exists": True, "encrypted": True},
    )

    with pytest.raises(rule_approval.AdminRuleApprovalError) as caught:
        rule_approval.require_admin_rule_approval(
            [], [high_risk_rule()], SCHEMA, None,
        )

    assert caught.value.code == "admin_key_required"
    assert caught.value.plugins == ["run_powershell"]


def test_strict_mode_accepts_matching_encrypted_private_key(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        BestAvailableEncryption,
        Encoding,
        PrivateFormat,
        PublicFormat,
    )
    from notmyfault.security import signing_keys

    key = Ed25519PrivateKey.generate()
    path = tmp_path / "signing_private_key.pem"
    path.write_bytes(key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        BestAvailableEncryption(b"correct-password"),
    ))
    public_key = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setattr(
        rule_approval, "detect_security_mode", lambda: SecurityMode.STRICT,
    )
    monkeypatch.setattr(rule_approval, "PRIVATE_KEY_FILE", path)
    monkeypatch.setattr(
        rule_approval, "key_status", lambda: {"exists": True, "encrypted": True},
    )
    monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [public_key])

    assert rule_approval.require_admin_rule_approval(
        [], [admin_rule()], SCHEMA, "correct-password",
    ) == ["admin_action"]

    with pytest.raises(rule_approval.AdminRuleApprovalError) as caught:
        rule_approval.require_admin_rule_approval(
            [], [admin_rule()], SCHEMA, "wrong-password",
        )
    assert caught.value.code == "admin_key_invalid"


def test_verify_admin_key_password_accepts_matching_encrypted_private_key(
    tmp_path, monkeypatch,
):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        BestAvailableEncryption,
        Encoding,
        PrivateFormat,
        PublicFormat,
    )
    from notmyfault.security import signing_keys

    key = Ed25519PrivateKey.generate()
    path = tmp_path / "signing_private_key.pem"
    path.write_bytes(key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        BestAvailableEncryption(b"correct-password"),
    ))
    public_key = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setattr(
        rule_approval, "detect_security_mode", lambda: SecurityMode.STRICT,
    )
    monkeypatch.setattr(rule_approval, "PRIVATE_KEY_FILE", path)
    monkeypatch.setattr(
        rule_approval, "key_status", lambda: {"exists": True, "encrypted": True},
    )
    monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [public_key])

    assert rule_approval.verify_admin_key_password(
        "correct-password", ["admin_action"],
    ) is None


@pytest.mark.parametrize(
    ("password", "expected_code"),
    [(None, "admin_key_required"), ("wrong-password", "admin_key_invalid")],
)
def test_verify_admin_key_password_reuses_existing_approval_errors(
    password, expected_code, monkeypatch,
):
    def reject_password(*args, **kwargs):
        raise ValueError("bad password")

    monkeypatch.setattr(
        rule_approval, "detect_security_mode", lambda: SecurityMode.STRICT,
    )
    monkeypatch.setattr(
        rule_approval, "key_status", lambda: {"exists": True, "encrypted": True},
    )
    monkeypatch.setattr(rule_approval, "load_private_key", reject_password)

    with pytest.raises(rule_approval.AdminRuleApprovalError) as caught:
        rule_approval.verify_admin_key_password(
            password, ["admin_action"],
        )

    assert caught.value.code == expected_code
    assert caught.value.plugins == ["admin_action"]
