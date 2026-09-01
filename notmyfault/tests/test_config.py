from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest

from notmyfault.config import (
    ConfigValidationError,
    SignedConfigStore,
    _normalize_config,
    normalize_rules,
)
from notmyfault.core.bindings import resolve_value
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


def test_removed_admin_settings_are_discarded_during_normalization():
    config = _normalize_config({
        "settings": {
            "admin_authorization_mode": "engine_start",
            "admin_rule_key_verification": False,
            "custom": "kept",
        },
    })

    assert config["settings"] == {"custom": "kept"}


def test_normalize_rules_updates_pure_and_mixed_legacy_step_templates():
    rules = normalize_rules([{
        "name": "旧模板",
        "event": {"type": "time_schedule", "params": {"time": "08:00"}},
        "actions": [
            {"type": "produce", "params": {}},
            {
                "type": "consume",
                "params": {
                    "pure": "{{ steps.produce_1.result.value }}",
                    "mixed": "prefix {{ steps.produce_1.result.value }}",
                    "literal": "steps.produce_1.result.value",
                    "reference": {
                        "$ref": {
                            "scope": "step",
                            "node": "produce_1",
                            "path": ["value"],
                        }
                    },
                },
            },
        ],
    }])

    producer, consumer = rules[0]["actions"]
    producer_id = producer["binding_id"]
    params = consumer["params"]
    assert params["pure"] == {
        "$ref": {"scope": "step", "node": producer_id, "path": ["value"]}
    }
    assert params["mixed"] == f"prefix {{{{ steps.{producer_id}.result.value }}}}"
    assert params["literal"] == "steps.produce_1.result.value"
    assert params["reference"] == {
        "$ref": {"scope": "step", "node": producer_id, "path": ["value"]}
    }
    context = {"steps": {producer_id: {"result": {"value": "ready"}}}}
    assert resolve_value(params["pure"], context) == "ready"
    assert resolve_value(params["mixed"], context) == "prefix ready"


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


def test_reopened_store_accepts_secure_existing_secret(tmp_path):
    paths = make_paths(tmp_path)
    make_store(paths)

    assert SignedConfigStore(paths).load_verified_config()["settings"] == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX 文件模式检查")
def test_reopened_store_rejects_world_readable_secret(tmp_path):
    paths = make_paths(tmp_path)
    make_store(paths)
    paths.config_secret_file.chmod(0o644)

    with pytest.raises(ConfigValidationError, match="权限过宽"):
        SignedConfigStore(paths).load_verified_config()


def test_loading_does_not_rotate_signed_backups(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    assert store.save_config({"settings": {"value": 1}})
    assert store.save_config({"settings": {"value": 2}})
    backup_path = paths.config_file.with_name(paths.config_file.name + ".bak")
    before = backup_path.read_bytes()

    assert store.load_config()["settings"]["value"] == 2

    assert backup_path.read_bytes() == before


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


def test_signed_malformed_rule_is_rejected_before_runtime(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    secret = paths.config_secret_file.read_bytes()
    malformed = simple_rule()
    malformed["actions"][0]["params"] = ["not", "an", "object"]
    paths.rules_file.write_text(
        json.dumps(
            signed_payload(secret, {"schema_version": 2, "rules": [malformed]}),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="actions\\[0\\]\\.params"):
        store.load_verified_rules()


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


def test_signed_powershell_rule_is_not_blocked_by_command_substrings(tmp_path):
    store = make_store(make_paths(tmp_path))
    rule = simple_rule()
    rule["actions"] = [
        {"type": "run_powershell", "params": {"command": "Remove-Item C:\\data"}}
    ]
    assert store.save_rules([rule])
    assert store.load_verified_rules()[0]["actions"][0]["type"] == "run_powershell"


def test_security_inspection_summarizes_rules_and_detects_tamper(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    rule = simple_rule()
    rule["actions"] = [{
        "type": "shutdown_system",
        "params": {"token": "top-secret-token"},
        "failure_actions": [{
            "type": "notify",
            "params": {"message": "private-failure-message"},
        }],
    }]
    assert store.save_rules([rule])
    status = store.inspect_security({
        "shutdown_system": {"security": {"rule_approval": "admin_key"}},
    })
    assert status["status"] == "ok"
    assert status["summary"]["rule_count"] == 1
    assert status["summary"]["rules"][0]["actions"][0]["high_risk"] is True
    summary_text = json.dumps(status["summary"], ensure_ascii=False)
    assert "top-secret-token" not in summary_text
    assert "private-failure-message" not in summary_text
    assert status["summary"]["rules"][0]["actions"][0]["params"] == {
        "token": "***"
    }
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
    store.approve_current_files({"triggers": {}, "actions": {}}, None)
    assert store.load_verified_config()["approved_value"] == 7


def test_approve_rejects_dynamic_literal_only_parameter(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    raw = json.loads(paths.rules_file.read_text(encoding="utf-8"))
    raw["rules"] = [
        {
            **simple_rule(),
            "actions": [
                {
                    "type": "run_powershell",
                    "params": {
                        "command": {
                            "$ref": {
                                "scope": "trigger",
                                "node": "t_source01",
                                "path": ["command"],
                            }
                        }
                    },
                }
            ],
        }
    ]
    raw["_signature"] = "invalid"
    paths.rules_file.write_text(json.dumps(raw), encoding="utf-8")
    schema = {
        "triggers": {},
        "actions": {
            "run_powershell": {
                "params": [{"name": "command", "type": "textarea"}],
                "security": {
                    "rule_approval": "admin_key",
                    "literal_only_params": ["command"],
                },
            }
        },
    }
    with pytest.raises(ConfigValidationError, match="数据绑定无效"):
        store.approve_current_files(schema, None)
