from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from notmyfault.config import ConfigValidationError, SignedConfigStore
from notmyfault.tests.api_support import make_paths, make_store


def simple_rule(name="提醒"):
    return {
        "name": name,
        "event": {"type": "time_schedule", "params": {"time": "08:00"}},
        "actions": [{"type": "notify", "params": {"title": name}}],
    }


def signed_payload(secret: bytes, value: dict) -> dict:
    payload = dict(value)
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    payload["_signature"] = hmac.new(
        secret, content.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return payload


def test_round_trip_uses_the_existing_files_and_json_shape(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    assert store.save_config({"custom": "value"})
    assert store.save_rules([simple_rule()])
    assert store.config_path == str(paths.config_dir / "config.json")
    assert store.rules_path == str(paths.config_dir / "rules.json")
    config_on_disk = json.loads(paths.config_file.read_text(encoding="utf-8"))
    rules_on_disk = json.loads(paths.rules_file.read_text(encoding="utf-8"))
    assert len(config_on_disk["_signature"]) == 64
    assert set(rules_on_disk) == {"schema_version", "rules", "_signature"}
    assert store.load_verified_config()["custom"] == "value"
    assert store.load_verified_rules()[0]["name"] == "提醒"


def test_signature_algorithm_stays_hmac_sha256_over_sorted_json(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    assert store.save_config({"z": 1, "a": "值"})
    raw = json.loads(paths.config_file.read_text(encoding="utf-8"))
    signature = raw.pop("_signature")
    secret = paths.config_secret_file.read_bytes()
    content = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    expected = hmac.new(
        secret, content.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(signature, expected)


@pytest.mark.parametrize("target", ["config", "rules"])
def test_tampering_is_rejected(target, tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    path = paths.config_file if target == "config" else paths.rules_file
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["tampered"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    loader = (
        store.load_verified_config
        if target == "config"
        else store.load_verified_rules
    )
    with pytest.raises(ConfigValidationError, match="签名校验失败"):
        loader()


def test_config_parse_failure_recovers_signed_backup(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    assert store.save_config({"generation": 1})
    assert store.save_config({"generation": 2})
    paths.config_file.write_text("{broken", encoding="utf-8")
    recovered = store.load_config()
    assert recovered["generation"] == 1
    assert store.load_verified_config()["generation"] == 1


def test_rules_parse_failure_recovers_signed_backup(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    assert store.save_rules([simple_rule("旧")])
    assert store.save_rules([simple_rule("新")])
    paths.rules_file.write_text("{broken", encoding="utf-8")
    recovered = store.load_rules()
    assert [rule["name"] for rule in recovered] == ["旧"]


def test_legacy_rules_are_moved_without_changing_physical_paths(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    secret = paths.config_secret_file.read_bytes()
    legacy = {
        "disabled_plugins": {"triggers": [], "actions": []},
        "rules": [simple_rule("旧规则")],
    }
    paths.config_file.write_text(
        json.dumps(signed_payload(secret, legacy), ensure_ascii=False),
        encoding="utf-8",
    )
    paths.rules_file.unlink()
    loaded = store.load_config()
    assert "rules" not in loaded
    assert [rule["name"] for rule in store.load_rules()] == ["旧规则"]
    assert (paths.config_dir / "config.json.premigration.bak").is_file()


def test_dangerous_rule_is_rejected_at_verified_load(tmp_path):
    store = make_store(make_paths(tmp_path))
    rule = simple_rule()
    rule["actions"] = [
        {"type": "run_powershell", "params": {"command": "Remove-Item C:\\data"}}
    ]
    assert store.save_rules([rule])
    with pytest.raises(ConfigValidationError, match="规则安全校验失败"):
        store.load_verified_rules()


def test_security_inspection_summarizes_rules_and_detects_tamper(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    rule = simple_rule()
    rule["actions"] = [{"type": "shutdown_system", "params": {}}]
    assert store.save_rules([rule])
    status = store.inspect_security()
    assert status["status"] == "ok"
    assert status["summary"]["rule_count"] == 1
    assert status["summary"]["rules"][0]["actions"][0]["high_risk"] is True
    raw = json.loads(paths.rules_file.read_text(encoding="utf-8"))
    raw["rules"][0]["name"] = "篡改"
    paths.rules_file.write_text(json.dumps(raw), encoding="utf-8")
    assert store.inspect_security()["status"] == "tampered"


def test_approve_current_files_resigns_reviewed_content(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    raw = json.loads(paths.config_file.read_text(encoding="utf-8"))
    raw["approved_value"] = 7
    raw["_signature"] = "invalid"
    paths.config_file.write_text(json.dumps(raw), encoding="utf-8")
    store.approve_current_files()
    assert store.load_verified_config()["approved_value"] == 7


def test_approve_rejects_unsafe_rules_before_resigning(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    raw = json.loads(paths.rules_file.read_text(encoding="utf-8"))
    raw["rules"] = [
        {
            **simple_rule(),
            "actions": [
                {
                    "type": "run_powershell",
                    "params": {"command": "Invoke-Expression bad"},
                }
            ],
        }
    ]
    raw["_signature"] = "invalid"
    paths.rules_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="规则安全校验失败"):
        store.approve_current_files()
