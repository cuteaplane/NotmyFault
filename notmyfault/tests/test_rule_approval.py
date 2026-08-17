from copy import deepcopy

import pytest

from notmyfault.security import rule_approval
from notmyfault.security.security import SecurityMode


SCHEMA = {
    "triggers": {"plain_trigger": {"permissions": []}},
    "actions": {
        "plain_action": {"permissions": []},
        "admin_action": {"permissions": ["admin"]},
    },
}


def admin_rule():
    return {
        "rule_id": "r_admin001",
        "name": "管理员规则",
        "event": {"type": "plain_trigger", "params": {}},
        "actions": [{"type": "admin_action", "params": {"state": "on"}}],
    }


def plain_rule():
    return {
        "rule_id": "r_plain001",
        "name": "普通规则",
        "event": {"type": "plain_trigger", "params": {}},
        "actions": [{"type": "plain_action", "params": {}}],
    }


def high_risk_rule():
    return {
        "rule_id": "r_risk001",
        "name": "高危规则",
        "event": {"type": "plain_trigger", "params": {}},
        "actions": [{"type": "run_powershell", "params": {"command": "Get-Service"}}],
    }


def test_changed_admin_plugins_ignores_unrelated_rule_edits():
    previous = [admin_rule(), plain_rule()]
    changed_plain = deepcopy(previous)
    changed_plain[1]["name"] = "改名后的普通规则"

    assert rule_approval.changed_admin_plugins(previous, changed_plain, SCHEMA) == []

    changed_admin = deepcopy(previous)
    changed_admin[0]["actions"][0]["params"]["state"] = "off"
    assert rule_approval.changed_admin_plugins(
        previous, changed_admin, SCHEMA,
    ) == ["admin_action"]


def test_changed_admin_plugins_finds_failure_action():
    rule = plain_rule()
    rule["actions"][0]["failure_actions"] = [
        {"type": "admin_action", "params": {}},
    ]

    assert rule_approval.changed_admin_plugins([], [rule], SCHEMA) == ["admin_action"]


def test_changed_high_risk_actions_finds_run_powershell():
    assert rule_approval.changed_high_risk_actions(
        [], [high_risk_rule()],
    ) == ["run_powershell"]


def test_changed_high_risk_actions_finds_failure_action():
    rule = plain_rule()
    rule["actions"][0]["failure_actions"] = [
        {"type": "kill_process", "params": {}},
    ]
    assert rule_approval.changed_high_risk_actions([], [rule]) == ["kill_process"]


def test_normal_mode_does_not_request_private_key(monkeypatch):
    monkeypatch.setattr(
        rule_approval, "detect_security_mode", lambda: SecurityMode.NORMAL,
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


def test_key_verification_off_skips_strict_mode_check(monkeypatch):
    monkeypatch.setattr(
        rule_approval, "detect_security_mode", lambda: SecurityMode.STRICT,
    )
    monkeypatch.setattr(
        rule_approval, "key_status", lambda: {"exists": True, "encrypted": True},
    )

    assert rule_approval.require_admin_rule_approval(
        [], [admin_rule()], SCHEMA, None, key_verification=False,
    ) == []


def test_key_verification_off_skips_high_risk_check(monkeypatch):
    monkeypatch.setattr(
        rule_approval, "detect_security_mode", lambda: SecurityMode.STRICT,
    )

    assert rule_approval.require_admin_rule_approval(
        [], [high_risk_rule()], SCHEMA, None, key_verification=False,
    ) == []


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
    monkeypatch.setattr(
        rule_approval, "detect_security_mode", lambda: SecurityMode.STRICT,
    )
    monkeypatch.setattr(
        rule_approval, "key_status", lambda: {"exists": True, "encrypted": True},
    )

    with pytest.raises(rule_approval.AdminRuleApprovalError) as caught:
        rule_approval.verify_admin_key_password(
            password, ["admin_action"],
        )

    assert caught.value.code == expected_code
    assert caught.value.plugins == ["admin_action"]
