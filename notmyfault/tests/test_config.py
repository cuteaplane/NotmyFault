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
from notmyfault.host.api.services.settings import SettingsService, SettingsServiceError
from notmyfault.tests.api_support import make_paths, make_store
from notmyfault.tests.canvas_fixtures import trigger_rule, set_rule_steps


def simple_rule(name="提醒"):
    return set_rule_steps(trigger_rule("r_config001", name, "time_schedule", {"time": "08:00"}), [
        {"type": "notify", "params": {"title": name}},
    ])


def signed_payload(secret: bytes, value: dict) -> dict:
    payload = dict(value)
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    payload["_signature"] = hmac.new(
        secret, content.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return payload


@pytest.mark.parametrize("field", ["admin_authorization_mode", "admin_rule_key_verification"])
def test_removed_admin_settings_are_rejected_without_modifying_input(field):
    config = {"settings": {field: False, "custom": "kept"}}
    with pytest.raises(ConfigValidationError, match=field):
        _normalize_config(config)
    assert config == {"settings": {field: False, "custom": "kept"}}


@pytest.mark.parametrize("obsolete", ["event", "condition", "actions", "preconditions"])
def test_normalize_rules_rejects_removed_rule_model(obsolete):
    rule = simple_rule()
    rule[obsolete] = {"value": "{{ steps.produce_1.result.value }}"}
    with pytest.raises(ValueError, match="旧条件模型已移除"):
        normalize_rules([rule])


def test_round_trip_uses_the_existing_files_and_json_shape(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    assert store.save_config({"custom": "value"})
    rule = simple_rule()
    rule["workflow"]["nodes"][0].update(occurrence="absent", duration_seconds=10)
    assert store.save_rules([rule])
    assert store.config_path == str(paths.config_dir / "config.json")
    assert store.rules_path == str(paths.config_dir / "rules.json")
    config_on_disk = json.loads(paths.config_file.read_text(encoding="utf-8"))
    rules_on_disk = json.loads(paths.rules_file.read_text(encoding="utf-8"))
    assert len(config_on_disk["_signature"]) == 64
    assert set(rules_on_disk) == {"schema_version", "rules", "_signature", "value_encoding"}
    assert rules_on_disk["schema_version"] == "2.1"
    assert rules_on_disk["value_encoding"] == "typed-v1"
    assert store.load_verified_config()["custom"] == "value"
    restored = store.load_verified_rules()[0]
    assert normalize_rules([restored]) == [restored]
    assert "event" not in restored and "trigger" not in restored
    assert restored["name"] == "提醒"
    assert restored["workflow"]["nodes"][0]["occurrence"] == "absent"
    assert restored["workflow"]["nodes"][0]["duration_seconds"] == 10
    assert restored["workflow"]["nodes"][1]["binding_id"].startswith("a_")
    restored["preconditions"] = [{"type": "document_quiescent", "params": {}}]
    assert not store.save_rules([restored])
    assert store.load_verified_rules()[0] == normalize_rules([rule])[0]


def test_reopened_store_accepts_secure_existing_secret(tmp_path, monkeypatch):
    if os.name == "nt":
        monkeypatch.setenv("USERDOMAIN", "")
        monkeypatch.setenv("USERNAME", "notmyfault-not-the-current-user")
    paths = make_paths(tmp_path)
    make_store(paths)

    assert SignedConfigStore(paths).load_verified_config()["settings"] == {}


def test_reopened_store_rejects_world_readable_secret(tmp_path):
    paths = make_paths(tmp_path)
    make_store(paths)
    if os.name == "nt":
        import subprocess

        try:
            result = subprocess.run(
                ["icacls", str(paths.config_secret_file), "/grant:r", "*S-1-1-0:R"],
                capture_output=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            pytest.skip(f"当前环境不能设置测试文件 ACL: {error}")
        if result.returncode:
            pytest.skip("当前账户不能为测试文件添加 Everyone 读取权限")
    else:
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
@pytest.mark.parametrize("bad_signature", [None, 123, ["bad"], "错误签名"])
def test_tampering_is_rejected(target, bad_signature, tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    path = paths.config_file if target == "config" else paths.rules_file
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["tampered"] = True
    if bad_signature is not None:
        raw["_signature"] = bad_signature
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
    malformed["workflow"]["nodes"][1]["params"] = ["not", "an", "object"]
    paths.rules_file.write_text(
        json.dumps(
            signed_payload(secret, {"schema_version": "2.1", "value_encoding": "typed-v1", "rules": [malformed]}),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="params"):
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


@pytest.mark.parametrize("backup_valid", [True, False])
def test_rules_parse_failure_recovers_signed_backup(tmp_path, backup_valid):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    assert store.save_rules([simple_rule("旧")])
    assert store.save_rules([simple_rule("新")])
    paths.rules_file.write_text("{broken", encoding="utf-8")
    if not backup_valid:
        backup = paths.rules_file.with_suffix(".json.bak")
        backup.write_text("{bad-backup", encoding="utf-8")
        with pytest.raises(ConfigValidationError):
            store.load_rules()
        assert paths.rules_file.read_text(encoding="utf-8") == "{broken"
        assert backup.read_text(encoding="utf-8") == "{bad-backup"
        return
    recovered = store.load_rules()
    assert [rule["name"] for rule in recovered] == ["旧"]


def test_legacy_rules_in_signed_config_are_rejected_without_migration(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    secret = paths.config_secret_file.read_bytes()
    legacy = {"disabled_plugins": {"triggers": [], "actions": []}, "rules": [simple_rule("旧规则")]}
    paths.config_file.write_text(json.dumps(signed_payload(secret, legacy), ensure_ascii=False), encoding="utf-8")
    original = paths.rules_file.read_bytes()
    with pytest.raises(ConfigValidationError, match="旧配置中的规则模型已移除"):
        store.load_config()
    assert paths.rules_file.read_bytes() == original
    assert not (paths.config_dir / "config.json.premigration.bak").exists()


def test_signed_powershell_rule_is_not_blocked_by_command_substrings(tmp_path):
    store = make_store(make_paths(tmp_path))
    rule = simple_rule()
    set_rule_steps(rule, [{"type": "run_powershell", "params": {"command": "Remove-Item C:\\data"}}])
    assert store.save_rules([rule])
    assert store.load_verified_rules()[0]["workflow"]["nodes"][1]["type"] == "run_powershell"


def test_security_inspection_summarizes_rules_and_detects_tamper(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    rule = set_rule_steps(simple_rule(), [{
        "type": "shutdown_system", "params": {"token": "top-secret-token"},
        "failure_actions": [{"type": "notify", "params": {"message": "private-failure-message"}}],
    }])
    graph = rule["workflow"]
    source = next(node for node in graph['nodes'] if node['type'] == 'trigger')
    restricted = next(node for node in graph['nodes'] if node['type'] == 'shutdown_system')
    group = {"binding_id": "a_choices001", "type": "event_group", "group_type": "if", "branches": [
        {"id": "b_initial001"}, {"id": "b_alternate001"}, {"id": "b_fallback001", "else": True},
    ]}
    condition = {"binding_id": "a_compare001", "type": "condition", "parent": group["binding_id"],
                 "predicate": {"op": "eq", "left": "private-condition", "right": "private-condition"}}
    branch = {"binding_id": "a_branch001", "type": "notify", "parent": group["binding_id"],
              "params": {"message": "private-branch-message"}}
    handler = {'binding_id': 'a_recovery001', 'type': 'action_group', 'parent': group['binding_id']}
    failure = {'binding_id': 'a_failure001', 'type': 'shutdown_system', 'parent': handler['binding_id'], 'params': {}}
    alternate = {"binding_id": "a_alternate001", "type": "shutdown_system", "parent": group["binding_id"],
                 "params": {"reason": "private-elif-message"}}
    fallback = {"binding_id": "a_fallback001", "type": "shutdown_system", "parent": group["binding_id"], "params": {}}
    graph["nodes"] += [group, condition, branch, alternate, fallback, handler, failure]
    graph["edges"] += [
        {"source": restricted["binding_id"], "port": "next", "target": group["binding_id"], "channel": "control"},
        {"source": condition["binding_id"], "port": "result", "target": group["binding_id"], "target_port": "b_initial001", "channel": "condition"},
        {"source": condition["binding_id"], "port": "result", "target": group["binding_id"], "target_port": "b_alternate001", "channel": "condition"},
        {"source": group["binding_id"], "port": "b_initial001", "target": branch["binding_id"], "channel": "control"},
        {"source": group["binding_id"], "port": "b_alternate001", "target": alternate["binding_id"], "channel": "control"},
        {"source": group["binding_id"], "port": "b_fallback001", "target": fallback["binding_id"], "channel": "control"},
        {'source': branch['binding_id'], 'port': 'failure', 'target': handler['binding_id'], 'channel': 'control'},
        {'source': handler['binding_id'], 'port': 'entry', 'target': failure['binding_id'], 'channel': 'control'},
    ]
    assert store.save_rules([rule])
    status = SettingsService(store, lambda: {"actions": {
        "shutdown_system": {"security": {"rule_approval": "admin_key"}},
    }}).security_summary()
    assert status["status"] == "ok"
    assert status["summary"]["rule_count"] == 1
    assert status["summary"]["rules"][0]["actions"][1]["high_risk"] is True
    summary_text = json.dumps(status["summary"], ensure_ascii=False)
    assert "top-secret-token" not in summary_text
    assert "private-failure-message" not in summary_text
    assert "private-branch-message" not in summary_text
    assert "private-elif-message" not in summary_text
    assert "private-condition" not in summary_text
    assert status["summary"]["rules"][0]["actions"][1]["params"] == {
        "token": "***"
    }
    summaries = status["summary"]["rules"][0]["actions"]
    by_id = {item['binding_id']: item for item in summaries}
    branch = by_id['a_branch001']
    assert branch['params'] == {'message': '***'}
    assert branch['high_risk'] is False
    assert by_id['a_failure001']['type'] == 'shutdown_system'
    assert by_id['a_failure001']['high_risk'] is True
    assert by_id['a_failure001']['parent'] == 'a_recovery001'
    assert by_id['a_alternate001']['high_risk'] is True
    assert by_id['a_alternate001']['params'] == {'reason': '***'}
    assert by_id['a_fallback001']['high_risk'] is True
    raw = json.loads(paths.rules_file.read_text(encoding="utf-8"))
    raw["rules"][0]["name"] = "篡改"
    paths.rules_file.write_text(json.dumps(raw), encoding="utf-8")
    assert SettingsService(store).security_summary()["status"] == "tampered"


def test_approve_current_files_resigns_reviewed_content(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    raw = json.loads(paths.config_file.read_text(encoding="utf-8"))
    raw["approved_value"] = 7
    raw["_signature"] = "invalid"
    paths.config_file.write_text(json.dumps(raw), encoding="utf-8")
    SettingsService(store).approve_security()
    assert store.load_verified_config()["approved_value"] == 7


def test_approve_rejects_dynamic_literal_only_parameter(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    raw = json.loads(paths.rules_file.read_text(encoding="utf-8"))
    raw["rules"] = [set_rule_steps(simple_rule(), [{
        "type": "run_powershell", "params": {"command": {"$ref": {
            "scope": "trigger", "node": "t_source01", "path": ["command"],
        }}},
    }])]
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
    with pytest.raises(SettingsServiceError, match="数据绑定无效"):
        SettingsService(store, lambda: schema).approve_security()


@pytest.mark.parametrize("encoding", [None, "json", "typed-v2", True, 1, {}, []])
@pytest.mark.parametrize("reader", ["verified", "unsigned", "backup"])
def test_rule_encoding_required_on_all_read_paths(tmp_path, encoding, reader):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    payload = {"schema_version": "2.1", "rules": [simple_rule()]}
    if encoding is not None:
        payload["value_encoding"] = encoding
    target = paths.rules_file.with_suffix(".json.bak") if reader == "backup" else paths.rules_file
    target.write_text(json.dumps(signed_payload(paths.config_secret_file.read_bytes(), payload)), encoding="utf-8")
    if reader == "backup":
        paths.rules_file.write_text("{broken", encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(ConfigValidationError):
        {"verified": store.load_verified_rules, "unsigned": store.load_unsigned_files, "backup": store.load_rules}[reader]()
    assert target.read_bytes() == before
    if reader == "backup":
        assert paths.rules_file.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("schema", [None, 2, "2", "3", "2.0"])
@pytest.mark.parametrize("reader", ["verified", "unsigned", "backup"])
def test_rule_format_is_not_converted_by_any_read_path(tmp_path, schema, reader):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    payload = {"schema_version": schema, "value_encoding": "typed-v1", "rules": [simple_rule()]}
    target = paths.rules_file.with_suffix(".json.bak") if reader == "backup" else paths.rules_file
    target.write_text(json.dumps(signed_payload(paths.config_secret_file.read_bytes(), payload)), encoding="utf-8")
    if reader == "backup":
        paths.rules_file.write_text("{broken", encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(ConfigValidationError):
        {"verified": store.load_verified_rules, "unsigned": store.load_unsigned_files, "backup": store.load_rules}[reader]()
    assert target.read_bytes() == before


def test_config_does_not_require_or_decode_rule_value_encoding(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    literal = {"$nmf_value": {"type": "int", "data": "9007199254740993"}}
    config = {"settings": {}, "custom": literal}
    paths.config_file.write_text(json.dumps(signed_payload(paths.config_secret_file.read_bytes(), config)), encoding="utf-8")
    from notmyfault.core.value_codec import decode_value
    def decode_rules_only(value):
        assert isinstance(value, list), "配置不应执行规则值解码"
        return decode_value(value)
    monkeypatch.setattr("notmyfault.config.decode_value", decode_rules_only)
    assert store.load_verified_config()["custom"] == literal
    assert store.load_unsigned_files()[0]["custom"] == literal
    paths.config_file.with_suffix(".json.bak").write_bytes(paths.config_file.read_bytes())
    paths.config_file.write_text("{broken", encoding="utf-8")
    assert store.load_config()["custom"] == literal


@pytest.mark.parametrize("reader", ["verified", "backup"])
def test_rule_signature_checked_before_decoding(tmp_path, monkeypatch, reader):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    payload = {"schema_version": "2.1", "value_encoding": "typed-v1", "rules": [], "_signature": "invalid"}
    target = paths.rules_file.with_suffix(".json.bak") if reader == "backup" else paths.rules_file
    target.write_text(json.dumps(payload), encoding="utf-8")
    if reader == "backup":
        paths.rules_file.write_text("{broken", encoding="utf-8")
    def unexpected_decode(_value):
        raise AssertionError("无效签名的数据不应解码")
    monkeypatch.setattr("notmyfault.config.decode_value", unexpected_decode)
    with pytest.raises(ConfigValidationError):
        (store.load_verified_rules if reader == "verified" else store.load_rules)()


def test_valid_signed_rule_decodes_exact_values_after_verification(tmp_path):
    store = make_store(make_paths(tmp_path))
    rule = simple_rule()
    value = 9007199254740993123456789
    rule["constants"] = [{"id": "c_exact0001", "name": "精确整数", "value_type": "int", "value": value}]
    assert store.save_rules([rule])
    disk = json.loads(store.paths.rules_file.read_text(encoding="utf-8"))
    assert disk["rules"][0]["constants"][0]["value"] == {"$nmf_value": {"type": "int", "data": str(value)}}
    assert store.load_verified_rules()[0]["constants"][0]["value"] == value


@pytest.mark.parametrize("field", ["admin_authorization_mode", "admin_rule_key_verification"])
@pytest.mark.parametrize("reader", ["verified", "unsigned", "backup"])
def test_removed_admin_settings_rejected_for_signed_unsigned_and_backup_files(tmp_path, field, reader):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    config = {"settings": {field: False}}
    target = paths.config_file.with_suffix(".json.bak") if reader == "backup" else paths.config_file
    target.write_text(json.dumps(signed_payload(paths.config_secret_file.read_bytes(), config)), encoding="utf-8")
    if reader == "backup":
        paths.config_file.write_text("{broken", encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(ConfigValidationError):
        {"verified": store.load_verified_config, "unsigned": store.load_unsigned_files, "backup": store.load_config}[reader]()
    assert target.read_bytes() == before


@pytest.mark.parametrize("reader", ["verified", "unsigned", "backup"])
def test_missing_rules_array_rejected_in_all_read_paths(tmp_path, reader):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    payload = {"schema_version": "2.1", "value_encoding": "typed-v1"}
    target = paths.rules_file.with_suffix(".json.bak") if reader == "backup" else paths.rules_file
    target.write_text(json.dumps(signed_payload(paths.config_secret_file.read_bytes(), payload)), encoding="utf-8")
    if reader == "backup":
        paths.rules_file.write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        {"verified": store.load_verified_rules, "unsigned": store.load_unsigned_files, "backup": store.load_rules}[reader]()


def test_approval_does_not_rewrite_missing_rule_encoding(tmp_path):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    payload = {"schema_version": "2.1", "rules": [simple_rule()], "_signature": "invalid"}
    paths.rules_file.write_text(json.dumps(payload), encoding="utf-8")
    before = paths.rules_file.read_bytes()
    with pytest.raises(SettingsServiceError, match="规则数据编码版本不受支持"):
        SettingsService(store).approve_security()
    assert paths.rules_file.read_bytes() == before
    assert not paths.rules_file.with_suffix(".json.bak").exists()


@pytest.mark.parametrize("invalid", ["condition", "actions", "failure_actions", "unknown"])
@pytest.mark.parametrize("reader", ["verified", "unsigned", "backup"])
def test_rejected_rule_fields_are_not_converted_on_read_or_recovery(tmp_path, invalid, reader):
    paths = make_paths(tmp_path)
    store = make_store(paths)
    rule = simple_rule()
    if invalid == "condition":
        rule["condition"] = {"op": "any", "children": []}
    elif invalid == "actions":
        rule["actions"] = [{"type": "notify", "params": {}}]
    elif invalid == "failure_actions":
        rule["workflow"]["nodes"][1]["failure_actions"] = [{"type": "notify", "params": {}}]
    else:
        rule["unknown"] = "unsupported"
    payload = {"schema_version": "2.1", "value_encoding": "typed-v1", "rules": [rule]}
    target = paths.rules_file.with_suffix(".json.bak") if reader == "backup" else paths.rules_file
    target.write_text(json.dumps(signed_payload(paths.config_secret_file.read_bytes(), payload)), encoding="utf-8")
    if reader == "backup":
        paths.rules_file.write_text("{broken", encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(ConfigValidationError):
        {"verified": store.load_verified_rules, "unsigned": store.load_unsigned_files, "backup": store.load_rules}[reader]()
    assert target.read_bytes() == before
    if reader == "backup":
        assert paths.rules_file.read_text(encoding="utf-8") == "{broken"
