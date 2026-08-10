"""EngineAPI：配置读写、引擎端点、规则端点、事件流与插件扫描别名"""

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import notmyfault.config as config_mod
from notmyfault.config import ensure_rule_binding_ids
from notmyfault.host import api_server
from notmyfault.host.api_server import EngineAPI


class FakeRunner:
    def __init__(self):
        self.engine_running = False
        self.engine_state = "stopped"
        self.current_engine = None
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.start_result = True

    def start_engine(self):
        self.start_calls += 1
        if self.start_result:
            self.engine_running = True
            self.engine_state = "running"
        return self.start_result

    def stop_engine(self):
        self.stop_calls += 1
        self.engine_running = False
        self.engine_state = "stopped"
        return True

    def request_process_shutdown(self, force_after=10):
        self.shutdown_calls += 1


class FakeEngine:
    def __init__(self):
        self.triggers_meta = {}
        self.calls = []

    def run_manual_rule_snapshot(self, rule, index, **kwargs):
        self.calls.append({"rule": rule, "index": index, "kwargs": kwargs})
        return True, "已执行"


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr(api_server, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(api_server, "API_TOKEN_FILE", str(tmp_path / ".api_token"))
    runner = FakeRunner()
    api = EngineAPI(runner)
    api._get_user_plugins_dir = lambda: str(tmp_path / "user_plugins")
    client = TestClient(api.app)
    headers = {"Authorization": f"Bearer {api_server.API_TOKEN}"}
    return SimpleNamespace(
        api=api, runner=runner, client=client, headers=headers, tmp_path=tmp_path
    )


def make_rule(condition, actions, name="测试规则"):
    return {"name": name, "condition": condition, "actions": actions}


def simple_rule():
    return {
        "name": "通知规则",
        "event": {"type": "usb_insert", "params": {}},
        "actions": [{"type": "open_url", "params": {"url": "http://example.com"}}],
    }


def bound_rule():
    # 动作参数绑定触发器输出，手动运行前必须提供对应 payload
    return {
        "name": "绑定规则",
        "event": {"type": "usb_insert", "params": {}, "binding_id": "t_usb001"},
        "actions": [
            {
                "type": "open_url",
                "params": {
                    "url": {
                        "$ref": {
                            "scope": "trigger",
                            "node": "t_usb001",
                            "path": ["drive_letter"],
                        }
                    }
                },
            }
        ],
    }


def typed_binding_rule(param_name):
    # 先由配置迁移器分配节点 id，再构造指向该触发器的 $ref
    rule = make_rule(
        {"type": "battery_level", "params": {}},
        [
            {
                "type": "shutdown_system",
                "params": {
                    param_name: {
                        "$ref": {
                            "scope": "trigger",
                            "node": "",
                            "path": ["battery_percent"],
                        }
                    }
                },
            }
        ],
    )
    normalized = ensure_rule_binding_ids(rule)
    leaf = normalized["condition"]
    ref = normalized["actions"][0]["params"][param_name]["$ref"]
    ref["node"] = leaf["binding_id"]
    return normalized


def write_plugin_dir(base, ptype, folder, meta, json_name):
    plugin_dir = base / ptype / folder
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / json_name).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    return plugin_dir


def make_meta(**overrides):
    meta = {
        "id": "demo",
        "name": "演示插件",
        "description": "演示用插件",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": "com.test.demo",
    }
    meta.update(overrides)
    return meta


class TestConfigHelpers:
    def test_load_config_file_missing(self, api_env):
        assert api_env.api._load_config() == {"rules": []}

    def test_load_config_corrupt(self, api_env):
        with open(api_server.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("{ not valid json")
        assert api_env.api._load_config() == {"rules": []}

    def test_save_config_success(self, api_env):
        assert api_env.api._save_config({"rules": []}) is True
        assert os.path.exists(api_server.CONFIG_FILE)

    def test_save_and_load_roundtrip(self, api_env):
        config = {"rules": [{"name": "r"}], "custom_key": 123}
        assert api_env.api._save_config(config) is True
        loaded = api_env.api._load_config()
        assert loaded["rules"] == [{"name": "r"}]
        assert loaded["custom_key"] == 123
        assert "_signature" not in loaded


class TestEngineEndpoints:
    def test_start_engine(self, api_env):
        response = api_env.client.post("/api/engine/start", headers=api_env.headers)
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["running"] is True
        assert api_env.runner.start_calls == 1

    def test_start_already_running(self, api_env):
        api_env.runner.engine_running = True
        api_env.runner.engine_state = "running"
        response = api_env.client.post("/api/engine/start", headers=api_env.headers)
        assert response.status_code == 200
        assert response.json()["message"] == "already_running"
        assert api_env.runner.start_calls == 0

    def test_start_rejected_while_previous_engine_is_stopping(self, api_env):
        api_env.runner.engine_state = "stopping"
        api_env.runner.start_result = False
        response = api_env.client.post("/api/engine/start", headers=api_env.headers)
        assert response.status_code == 409
        assert response.json()["message"] == "engine_stopping"

    def test_stop_engine(self, api_env):
        response = api_env.client.post("/api/engine/stop", headers=api_env.headers)
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["stopped"] is True
        assert api_env.runner.stop_calls == 1

    def test_shutdown(self, api_env):
        response = api_env.client.post("/api/engine/shutdown", headers=api_env.headers)
        assert response.status_code == 200
        assert response.json() == {"ok": True, "message": "shutting_down"}
        assert api_env.runner.shutdown_calls == 1

    def test_status_not_running(self, api_env):
        response = api_env.client.get("/api/engine/status", headers=api_env.headers)
        assert response.status_code == 200
        body = response.json()
        assert body["running"] is False
        assert body["api_alive"] is True
        assert body["rules_count"] == 0

    def test_status_counts_nested_condition_trigger_types(self, api_env):
        rule = make_rule(
            {
                "op": "all",
                "children": [
                    {"type": "usb_insert", "params": {}},
                    {
                        "op": "any",
                        "children": [
                            {"type": "hotkey", "params": {}},
                            {"type": "usb_insert", "params": {}},
                        ],
                    },
                ],
            },
            [
                {"type": "open_url", "params": {"url": "http://example.com"}},
                {"type": "open_url", "params": {"url": "http://example.org"}},
            ],
        )
        assert api_env.api._save_config({"rules": [rule]}) is True
        body = api_env.client.get(
            "/api/engine/status", headers=api_env.headers
        ).json()
        assert body["rules_count"] == 1
        assert body["triggers_count"] == 2
        assert body["actions_count"] == 1

    def test_unauthenticated_read_is_rejected(self, api_env):
        response = api_env.client.get("/api/rules")
        assert response.status_code == 403

    def test_invalid_disk_token_is_repaired_for_next_request(self, api_env):
        stale = "0" * 64
        api_server._secure_write_token(api_server.API_TOKEN_FILE, stale)
        headers = {"Authorization": f"Bearer {stale}"}
        assert api_env.client.get("/api/rules", headers=headers).status_code == 403
        # 拒绝请求的同时把磁盘 token 修复为当前实例使用的值
        with open(api_server.API_TOKEN_FILE, encoding="utf-8") as f:
            assert f.read().strip() == api_server.API_TOKEN
        ok = api_env.client.get("/api/rules", headers=api_env.headers)
        assert ok.status_code == 200

    def test_dashboard_fallback_port_cors_preflight_is_allowed(self, api_env):
        origin = "http://localhost:19210"
        response = api_env.client.options(
            "/api/engine/status",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin


class TestDiagnosticsEndpoint:
    def test_diagnostics_no_engine_ref(self, api_env):
        response = api_env.client.get(
            "/api/engine/diagnostics", headers=api_env.headers
        )
        assert response.status_code == 200
        assert response.json() == {
            "uptime_seconds": 0,
            "plugins": {},
            "rules": {},
            "actions": {},
        }

    def test_diagnostics_with_engine(self, api_env):
        snapshot = {
            "uptime_seconds": 12.5,
            "plugins": {"loaded": 3},
            "rules": {"active": 1},
            "actions": {"success": 2},
        }
        api_env.runner.current_engine = SimpleNamespace(
            get_diagnostics=lambda: snapshot
        )
        response = api_env.client.get(
            "/api/engine/diagnostics", headers=api_env.headers
        )
        assert response.json() == snapshot


class TestEventStream:
    def _deliver_one(self, api, setup):
        async def scenario():
            queue = setup()
            api._subscribers.append(queue)
            api._loop = asyncio.get_running_loop()
            api.push_event("engine_started", {"rule": "r1"})
            await asyncio.sleep(0)
            return queue

        return asyncio.run(scenario())

    def test_push_event_to_subscriber(self, api_env):
        queue = self._deliver_one(
            api_env.api, lambda: asyncio.Queue(maxsize=200)
        )
        packet = queue.get_nowait()
        assert packet["type"] == "engine_started"
        assert packet["data"] == {"rule": "r1"}

    def test_event_has_timestamp(self, api_env):
        before = time.time()
        queue = self._deliver_one(
            api_env.api, lambda: asyncio.Queue(maxsize=200)
        )
        packet = queue.get_nowait()
        assert before <= packet["ts"] <= time.time()

    def test_push_event_multiple_subscribers(self, api_env):
        api = api_env.api

        async def scenario():
            q1 = asyncio.Queue(maxsize=200)
            q2 = asyncio.Queue(maxsize=200)
            api._subscribers.extend([q1, q2])
            api._loop = asyncio.get_running_loop()
            api.push_event("engine_stopped", {})
            await asyncio.sleep(0)
            return q1, q2

        q1, q2 = asyncio.run(scenario())
        assert q1.get_nowait()["type"] == "engine_stopped"
        assert q2.get_nowait()["type"] == "engine_stopped"

    def test_push_event_full_queue_removed(self, api_env):
        api = api_env.api

        async def scenario():
            queue = asyncio.Queue(maxsize=1)
            queue.put_nowait({"filler": True})
            api._subscribers.append(queue)
            api._loop = asyncio.get_running_loop()
            api.push_event("engine_started", {})
            await asyncio.sleep(0)

        asyncio.run(scenario())
        assert api._subscribers == []


class TestGetPluginsSchema:
    def test_returns_expected_structure(self, api_env):
        schema = api_env.api._get_plugins_schema()
        assert set(schema) == {"triggers", "actions"}
        assert "usb_insert" in schema["triggers"]
        assert "open_url" in schema["actions"]
        for meta in schema["actions"].values():
            assert meta["id"]


class TestLogsEndpoint:
    def test_logs_no_files(self, api_env):
        response = api_env.client.get("/api/engine/logs", headers=api_env.headers)
        assert response.status_code == 200
        assert response.json() == {"lines": [], "total": 0}

    def test_logs_with_file(self, api_env):
        log_dir = api_env.tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "engine-test.log").write_text(
            "line1\nline2\nline3\n", encoding="utf-8"
        )
        response = api_env.client.get(
            "/api/engine/logs", params={"lines": 2}, headers=api_env.headers
        )
        assert response.json() == {
            "lines": ["line2", "line3"],
            "total": 3,
        }


class TestPluginsEndpoint:
    def test_plugins_schema(self, api_env):
        response = api_env.client.get("/api/plugins", headers=api_env.headers)
        assert response.status_code == 200
        schema = response.json()
        assert set(schema) == {"triggers", "actions"}
        assert schema["triggers"], "内置触发器不应为空"
        assert schema["actions"], "内置动作不应为空"


class TestRulesEndpoints:
    def test_get_rules_empty(self, api_env):
        response = api_env.client.get("/api/rules", headers=api_env.headers)
        assert response.status_code == 200
        assert response.json() == {"rules": []}

    def test_get_rules_with_data(self, api_env):
        rules = [simple_rule()]
        assert api_env.api._save_config({"rules": rules}) is True
        response = api_env.client.get("/api/rules", headers=api_env.headers)
        assert response.status_code == 200
        got = response.json()["rules"]
        assert len(got) == 1
        assert got[0]["name"] == "通知规则"
        assert got[0]["event"]["type"] == "usb_insert"

    def test_validate_rule_draft_reports_valid_rule_with_schema_warning(self, api_env):
        response = api_env.client.post(
            "/api/rules/validate",
            json={"rule": simple_rule()},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["valid"] is True
        assert body["summary"] == {"errors": 0, "warnings": 1}
        assert body["issues"][0]["code"] == "plugin_parameter"

    def test_validate_rule_draft_reports_plugin_and_binding_errors(self, api_env):
        missing_plugin = simple_rule()
        missing_plugin["actions"][0]["type"] = "missing_action"
        plugin_response = api_env.client.post(
            "/api/rules/validate",
            json={"rule": missing_plugin},
            headers=api_env.headers,
        )
        binding_response = api_env.client.post(
            "/api/rules/validate",
            json={"rule": typed_binding_rule("force")},
            headers=api_env.headers,
        )

        assert plugin_response.status_code == 200
        assert plugin_response.json()["valid"] is False
        assert any(
            issue["code"] == "plugin_reference"
            for issue in plugin_response.json()["issues"]
        )
        assert binding_response.status_code == 200
        assert binding_response.json()["valid"] is False
        assert any(
            issue["code"] == "binding_type_mismatch"
            for issue in binding_response.json()["issues"]
        )

    def test_validate_rule_draft_warns_when_retry_may_repeat_action(self, api_env):
        rule = simple_rule()
        rule["actions"][0]["retry"] = 2

        response = api_env.client.post(
            "/api/rules/validate",
            json={"rule": rule},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert response.json()["valid"] is True
        warning = next(
            issue
            for issue in response.json()["issues"]
            if issue["code"] == "retry_may_repeat"
        )
        assert warning["location"] == "actions[0]"
        assert "重试可能重复产生结果" in warning["message"]

    def test_validate_rule_draft_reports_unsafe_command_without_writing(self, api_env):
        rule = simple_rule()
        rule["actions"] = [{
            "type": "run_powershell",
            "params": {"command": "powershell -EncodedCommand QUFB"},
        }]

        response = api_env.client.post(
            "/api/rules/validate",
            json={"rule": rule},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert response.json()["valid"] is False
        assert any(
            issue["code"] == "unsafe_action"
            for issue in response.json()["issues"]
        )
        assert not os.path.exists(api_server.RULES_FILE)

    def test_put_rules_invalid_json(self, api_env):
        response = api_env.client.put(
            "/api/rules",
            content="not json",
            headers={**api_env.headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["ok"] is False

    def test_put_rules_rejects_non_object_body(self, api_env):
        response = api_env.client.put(
            "/api/rules", json=[1, 2, 3], headers=api_env.headers
        )
        assert response.status_code == 400
        assert "JSON 对象" in response.json()["error"]

    def test_put_rules_success(self, api_env):
        response = api_env.client.put(
            "/api/rules", json={"rules": [simple_rule()]}, headers=api_env.headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        saved = api_env.api._load_config()["rules"]
        assert len(saved) == 1
        assert saved[0]["name"] == "通知规则"

    def test_rules_roundtrip(self, api_env):
        put = api_env.client.put(
            "/api/rules", json={"rules": [simple_rule()]}, headers=api_env.headers
        )
        assert put.status_code == 200
        got = api_env.client.get("/api/rules", headers=api_env.headers).json()["rules"]
        assert got == put.json()["rules"]

    def test_put_rules_preserves_unrelated_config_keys(self, api_env):
        assert api_env.api._save_config({"rules": [], "custom_flag": "keep"}) is True
        response = api_env.client.put(
            "/api/rules", json={"rules": [simple_rule()]}, headers=api_env.headers
        )
        assert response.status_code == 200
        loaded = api_env.api._load_config()
        assert loaded["custom_flag"] == "keep"

    def test_put_rules_rejects_empty_nested_condition_group(self, api_env):
        rule = make_rule(
            {"op": "any", "children": []},
            [{"type": "open_url", "params": {"url": "http://example.com"}}],
        )
        response = api_env.client.put(
            "/api/rules", json={"rules": [rule]}, headers=api_env.headers
        )
        assert response.status_code == 400
        assert response.json()["error"] == "规则结构校验失败"

    def test_put_rules_accepts_typed_trigger_binding(self, api_env):
        rule = typed_binding_rule("delay_seconds")
        response = api_env.client.put(
            "/api/rules", json={"rules": [rule]}, headers=api_env.headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True

    def test_put_rules_rejects_binding_type_mismatch(self, api_env):
        rule = typed_binding_rule("force")
        response = api_env.client.put(
            "/api/rules", json={"rules": [rule]}, headers=api_env.headers
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "规则数据绑定无效"
        assert body["details"][0]["code"] == "binding_type_mismatch"

    def _install_engine(self, api_env):
        engine = FakeEngine()
        api_env.runner.current_engine = engine
        assert api_env.api._save_config({"rules": [bound_rule()]}) is True
        return engine

    def test_manual_run_requires_trigger_payload_for_bound_rule(self, api_env):
        self._install_engine(api_env)
        response = api_env.client.post("/api/rules/0/run", headers=api_env.headers)
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "missing_test_context"
        assert body["required_trigger_ids"] == ["t_usb001"]

    def test_manual_run_accepts_explicit_trigger_payload(self, api_env):
        engine = self._install_engine(api_env)
        response = api_env.client.post(
            "/api/rules/0/run",
            json={"trigger_payloads": {"t_usb001": {"drive_letter": "E:"}}},
            headers=api_env.headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert len(engine.calls) == 1
        assert engine.calls[0]["kwargs"]["trigger_payloads"] == {
            "t_usb001": {"drive_letter": "E:"}
        }

    def test_manual_run_uses_verified_saved_snapshot(self, api_env):
        engine = self._install_engine(api_env)
        saved = api_env.api._load_config()["rules"][0]
        response = api_env.client.post(
            "/api/rules/0/run",
            json={
                "rule": saved,
                "trigger_payloads": {"t_usb001": {"drive_letter": "E:"}},
            },
            headers=api_env.headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert len(engine.calls) == 1

    def test_manual_run_rejects_stale_snapshot(self, api_env):
        engine = self._install_engine(api_env)
        stale = api_env.api._load_config()["rules"][0]
        stale = {**stale, "name": "被改过的规则"}
        response = api_env.client.post(
            "/api/rules/0/run", json={"rule": stale}, headers=api_env.headers
        )
        assert response.status_code == 409
        assert "规则保存版本已变化" in response.json()["error"]
        assert engine.calls == []

    def test_manual_run_rejects_malformed_body_without_executing(self, api_env):
        engine = self._install_engine(api_env)
        response = api_env.client.post(
            "/api/rules/0/run",
            content="{bad json",
            headers={**api_env.headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert engine.calls == []


class TestScanPlugins:
    def test_directory_not_exists(self, tmp_path):
        assert api_server._scan_plugins(str(tmp_path), "triggers", "trigger.json") == {}

    def test_missing_json(self, tmp_path):
        (tmp_path / "triggers" / "demo").mkdir(parents=True)
        assert api_server._scan_plugins(str(tmp_path), "triggers", "trigger.json") == {}

    def test_corrupt_json_skipped(self, tmp_path):
        folder = tmp_path / "triggers" / "demo"
        folder.mkdir(parents=True)
        (folder / "trigger.json").write_text("{bad json", encoding="utf-8")
        assert api_server._scan_plugins(str(tmp_path), "triggers", "trigger.json") == {}

    def test_missing_id_field_skipped(self, tmp_path):
        meta = make_meta()
        del meta["id"]
        write_plugin_dir(tmp_path, "triggers", "demo", meta, "trigger.json")
        assert api_server._scan_plugins(str(tmp_path), "triggers", "trigger.json") == {}

    def test_missing_required_fields_skipped(self, tmp_path):
        meta = make_meta()
        del meta["name"]
        write_plugin_dir(tmp_path, "triggers", "demo", meta, "trigger.json")
        assert api_server._scan_plugins(str(tmp_path), "triggers", "trigger.json") == {}

    def test_disabled_plugin_skipped(self, tmp_path):
        write_plugin_dir(
            tmp_path, "triggers", "demo", make_meta(enabled=False), "trigger.json"
        )
        assert api_server._scan_plugins(str(tmp_path), "triggers", "trigger.json") == {}

    def test_skips_non_directories(self, tmp_path):
        (tmp_path / "triggers").mkdir()
        (tmp_path / "triggers" / "stray.json").write_text(
            "{}", encoding="utf-8"
        )
        write_plugin_dir(tmp_path, "triggers", "demo", make_meta(), "trigger.json")
        found = api_server._scan_plugins(str(tmp_path), "triggers", "trigger.json")
        assert set(found) == {"demo"}

    def test_valid_plugin_returned(self, tmp_path):
        write_plugin_dir(tmp_path, "triggers", "demo", make_meta(), "trigger.json")
        found = api_server._scan_plugins(str(tmp_path), "triggers", "trigger.json")
        assert set(found) == {"demo"}
        assert found["demo"]["id"] == "demo"

    def test_multiple_plugins_mixed(self, tmp_path):
        write_plugin_dir(tmp_path, "triggers", "good", make_meta(id="good"), "trigger.json")
        write_plugin_dir(
            tmp_path, "triggers", "off", make_meta(id="off", enabled=False), "trigger.json"
        )
        found = api_server._scan_plugins(str(tmp_path), "triggers", "trigger.json")
        assert set(found) == {"good"}

    def test_scan_actions(self, tmp_path):
        write_plugin_dir(tmp_path, "actions", "demo", make_meta(), "action.json")
        found = api_server._scan_plugins(str(tmp_path), "actions", "action.json")
        assert set(found) == {"demo"}
