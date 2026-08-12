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
        self.cancel_calls = []
        self.cancel_result = True

    def run_manual_rule_snapshot(self, rule, index, **kwargs):
        self.calls.append({"rule": rule, "index": index, "kwargs": kwargs})
        return True, "已执行", "run_fake001"

    def cancel_run(self, run_id):
        self.cancel_calls.append(run_id)
        return self.cancel_result


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr(api_server, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    rules_file = str(tmp_path / "rules.json")
    monkeypatch.setattr(api_server, "RULES_FILE", rules_file)
    monkeypatch.setattr(config_mod, "RULES_FILE", rules_file)
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


def _desktop_selector():
    return {
        "version": 1,
        "window": {
            "process": "notepad.exe",
            "name": "无标题 - 记事本",
            "control_type": 50032,
        },
        "target": {
            "automation_id": "FileSave",
            "name": "保存",
            "control_type": 50000,
        },
        "ancestors": [],
        "display": {
            "control": "保存",
            "control_type": "按钮",
            "window": "无标题 - 记事本",
            "app": "notepad.exe",
        },
    }


def test_capture_desktop_element_waits_then_returns_selector(
    api_env, monkeypatch
):
    from notmyfault.native import uia

    waits = []

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(api_server.asyncio, "sleep", no_wait)
    monkeypatch.setattr(
        uia, "capture_element_under_cursor", _desktop_selector
    )

    response = api_env.client.post(
        "/api/desktop-elements/capture",
        json={"delay_seconds": 2},
        headers=api_env.headers,
    )

    assert response.status_code == 200
    assert response.json()["selector"]["target"]["name"] == "保存"
    assert waits == [2.0]


def test_check_desktop_element_reports_locator_error(api_env, monkeypatch):
    from notmyfault.native import uia

    def missing(_selector):
        raise uia.DesktopElementError(
            "element_not_found", "窗口已经打开，但找不到录制的控件"
        )

    monkeypatch.setattr(uia, "check_selector", missing)

    response = api_env.client.post(
        "/api/desktop-elements/check",
        json={"selector": _desktop_selector()},
        headers=api_env.headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "element_not_found"
    assert "找不到录制的控件" in response.json()["error"]


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
        assert api_env.api._save_config({}) is True
        assert os.path.exists(api_server.CONFIG_FILE)

    def test_save_and_load_roundtrip(self, api_env):
        config = {"custom_key": 123}
        assert api_env.api._save_config(config) is True
        loaded = api_env.api._load_config()
        assert loaded["custom_key"] == 123
        assert "rules" not in loaded
        assert "_signature" not in loaded

    def test_load_rules_file_missing(self, api_env):
        assert api_env.api._load_rules() == []

    def test_save_and_load_rules_roundtrip(self, api_env):
        assert api_env.api._save_rules([simple_rule()]) is True
        loaded = api_env.api._load_rules()
        assert len(loaded) == 1
        assert loaded[0]["name"] == "通知规则"


class TestAdminAuthorizationSettings:
    def test_get_defaults_to_per_execution(self, api_env):
        response = api_env.client.get(
            "/api/settings/admin-authorization", headers=api_env.headers
        )
        assert response.status_code == 200
        assert response.json()["mode"] == "per_execution"

    @pytest.mark.skipif(os.name != "nt", reason="启动时一次授权只支持 Windows")
    def test_put_saves_mode_and_reports_restart(self, api_env):
        api_env.runner.current_engine = SimpleNamespace(
            admin_authorization_mode="per_execution"
        )
        response = api_env.client.put(
            "/api/settings/admin-authorization",
            json={"mode": "engine_start"},
            headers=api_env.headers,
        )
        assert response.status_code == 200
        assert response.json()["restart_required"] is True
        saved = api_env.api._load_config()
        assert saved["settings"]["admin_authorization_mode"] == "engine_start"

    def test_put_rejects_unknown_mode(self, api_env):
        response = api_env.client.put(
            "/api/settings/admin-authorization",
            json={"mode": "unrestricted"},
            headers=api_env.headers,
        )
        assert response.status_code == 400

    def test_put_rejects_tampered_config(self, api_env):
        assert api_env.api._save_config({"custom_flag": "keep"}) is True
        with open(api_server.CONFIG_FILE, "r", encoding="utf-8") as file:
            tampered = json.load(file)
        tampered["custom_flag"] = "changed outside NotmyFault"
        with open(api_server.CONFIG_FILE, "w", encoding="utf-8") as file:
            json.dump(tampered, file, ensure_ascii=False)

        response = api_env.client.put(
            "/api/settings/admin-authorization",
            json={"mode": "per_execution"},
            headers=api_env.headers,
        )

        assert response.status_code == 409
        assert "完整性校验" in response.json()["error"]


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
        assert api_env.api._save_rules([rule]) is True
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

    def test_run_event_is_saved_without_sse_subscriber(self, api_env):
        api_env.api.push_event(
            "rule_triggered",
            {
                "run_id": "run_offline",
                "rule_id": "r_rule001",
                "rule_name": "离线运行",
                "event_type": "manual",
                "action_count": 0,
            },
        )
        api_env.api.push_event(
            "workflow_completed",
            {
                "run_id": "run_offline",
                "rule_id": "r_rule001",
                "rule_name": "离线运行",
                "status": "succeeded",
            },
        )

        response = api_env.client.get("/api/runs", headers=api_env.headers)

        assert response.status_code == 200
        assert response.json()["runs"][0]["run_id"] == "run_offline"
        assert response.json()["runs"][0]["status"] == "succeeded"

    def test_get_missing_run_returns_404(self, api_env):
        response = api_env.client.get(
            "/api/runs/run_missing", headers=api_env.headers
        )

        assert response.status_code == 404

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
        assert api_env.api._save_rules(rules) is True
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

    def test_validate_rule_draft_checks_failure_action_plugins(self, api_env):
        rule = simple_rule()
        rule["actions"][0]["failure_actions"] = [{
            "type": "missing_action",
            "params": {},
        }]

        response = api_env.client.post(
            "/api/rules/validate",
            json={"rule": rule},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        issue = next(
            issue
            for issue in response.json()["issues"]
            if issue["code"] == "plugin_reference"
        )
        assert response.json()["valid"] is False
        assert issue["location"] == "actions[0].failure_actions[0]"
        assert "补救动作 1" in issue["message"]

    def test_validate_rule_draft_rejects_timeout_without_safe_cancel(self, api_env):
        rule = simple_rule()
        rule["actions"][0]["timeout_seconds"] = 60

        response = api_env.client.post(
            "/api/rules/validate",
            json={"rule": rule},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        issue = next(
            issue
            for issue in response.json()["issues"]
            if issue["code"] == "timeout_not_supported"
        )
        assert response.json()["valid"] is False
        assert issue["location"] == "actions[0]"
        assert "不支持安全取消" in issue["message"]

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

    def test_local_rule_draft_previews_without_saving(self, api_env):
        response = api_env.client.post(
            "/api/rules/draft",
            json={"description": "每天 08:30 提醒我提交月报"},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "local"
        assert body["draft"]["event"]["type"] == "time_schedule"
        assert body["draft"]["event"]["params"]["time"] == "08:30"
        assert body["draft"]["actions"][0]["type"] == "notify"
        assert body["draft"]["actions"][0]["params"]["message"] == "提交月报"
        assert body["validation"]["ok"] is True
        assert not os.path.exists(api_server.RULES_FILE)

    def test_usb_backup_draft_binds_inserted_drive(self, api_env):
        response = api_env.client.post(
            "/api/rules/draft",
            json={"description": "U盘插入后备份文件"},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        body = response.json()
        trigger_id = body["draft"]["event"]["binding_id"]
        source = body["draft"]["actions"][0]["params"]["source"]
        assert source == {
            "$ref": {
                "scope": "trigger",
                "node": trigger_id,
                "path": ["actual_drive"],
            }
        }
        assert body["assumptions"] == ["文件来源使用本次插入的 U 盘"]
        assert all("源路径" not in item for item in body["missing"])
        assert any("目标路径" in item for item in body["missing"])

    def test_local_rule_draft_rejects_empty_description(self, api_env):
        response = api_env.client.post(
            "/api/rules/draft",
            json={"description": "  "},
            headers=api_env.headers,
        )

        assert response.status_code == 400
        assert response.json()["code"] == "empty_description"

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
        assert body["rules"][0]["rule_id"].startswith("r_")
        saved = api_env.api._load_rules()
        assert len(saved) == 1
        assert saved[0]["name"] == "通知规则"
        assert saved[0]["rule_id"] == body["rules"][0]["rule_id"]

    def test_rules_roundtrip(self, api_env):
        put = api_env.client.put(
            "/api/rules", json={"rules": [simple_rule()]}, headers=api_env.headers
        )
        assert put.status_code == 200
        got = api_env.client.get("/api/rules", headers=api_env.headers).json()["rules"]
        assert got == put.json()["rules"]

    def test_put_rules_does_not_rewrite_config(self, api_env):
        # 规则单独落 rules.json，保存规则不再重写 config.json
        assert api_env.api._save_config({"custom_flag": "keep"}) is True
        response = api_env.client.put(
            "/api/rules", json={"rules": [simple_rule()]}, headers=api_env.headers
        )
        assert response.status_code == 200
        loaded = api_env.api._load_config()
        assert loaded["custom_flag"] == "keep"

    def test_put_rules_rejects_tampered_rules_file(self, api_env):
        assert api_env.api._save_rules([simple_rule()]) is True
        with open(api_server.RULES_FILE, "r", encoding="utf-8") as file:
            tampered = json.load(file)
        tampered["rules"][0]["name"] = "changed outside NotmyFault"
        with open(api_server.RULES_FILE, "w", encoding="utf-8") as file:
            json.dump(tampered, file, ensure_ascii=False)

        response = api_env.client.put(
            "/api/rules", json={"rules": [simple_rule()]}, headers=api_env.headers
        )

        assert response.status_code == 409
        assert "完整性校验" in response.json()["error"]

    def test_run_rule_rejects_tampered_rules_file(self, api_env):
        rule = simple_rule()
        assert api_env.api._save_rules([rule]) is True
        with open(api_server.RULES_FILE, "r", encoding="utf-8") as file:
            tampered = json.load(file)
        tampered["rules"][0]["actions"][0]["params"]["url"] = "https://evil.test"
        with open(api_server.RULES_FILE, "w", encoding="utf-8") as file:
            json.dump(tampered, file, ensure_ascii=False)
        engine = FakeEngine()
        api_env.runner.current_engine = engine

        response = api_env.client.post(
            "/api/rules/0/run", json={"rule": rule}, headers=api_env.headers
        )

        assert response.status_code == 409
        assert engine.calls == []

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
        assert api_env.api._save_rules([bound_rule()]) is True
        return engine

    def _install_partial_engine(self, api_env):
        rule = {
            "name": "局部运行规则",
            "event": {"type": "hotkey", "params": {}, "binding_id": "t_hotkey001"},
            "actions": [
                {
                    "type": "append_text",
                    "binding_id": "a_first001",
                    "params": {"file": "a.txt", "text": "hello"},
                },
                {
                    "type": "open_url",
                    "binding_id": "a_second001",
                    "params": {
                        "url": {
                            "$ref": {
                                "scope": "step",
                                "node": "a_first001",
                                "path": ["file"],
                            }
                        }
                    },
                },
            ],
        }
        engine = FakeEngine()
        api_env.runner.current_engine = engine
        assert api_env.api._save_rules([rule]) is True
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
        assert response.json()["run_id"] == "run_fake001"
        assert len(engine.calls) == 1
        assert engine.calls[0]["kwargs"]["trigger_payloads"] == {
            "t_usb001": {"drive_letter": "E:"}
        }

    def test_cancel_run_forwards_to_active_engine(self, api_env):
        engine = self._install_engine(api_env)

        response = api_env.client.post(
            "/api/runs/run_fake001/cancel", headers=api_env.headers
        )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert engine.cancel_calls == ["run_fake001"]

    def test_cancel_run_returns_not_found_after_completion(self, api_env):
        engine = self._install_engine(api_env)
        engine.cancel_result = False

        response = api_env.client.post(
            "/api/runs/run_finished/cancel", headers=api_env.headers
        )

        assert response.status_code == 404
        assert response.json()["ok"] is False

    def test_partial_run_requires_skipped_upstream_result(self, api_env):
        engine = self._install_partial_engine(api_env)

        response = api_env.client.post(
            "/api/rules/0/run",
            json={"start_step_id": "a_second001"},
            headers=api_env.headers,
        )

        assert response.status_code == 400
        assert response.json()["required_step_ids"] == ["a_first001"]
        assert engine.calls == []

    def test_partial_run_passes_range_upstream_results_and_assertions(self, api_env):
        engine = self._install_partial_engine(api_env)
        assertions = [
            {
                "step_id": "a_second001",
                "path": ["opened"],
                "operator": "gte",
                "expected": 1,
            }
        ]

        response = api_env.client.post(
            "/api/rules/0/run",
            json={
                "start_step_id": "a_second001",
                "step_outputs": {"a_first001": {"file": "https://example.com"}},
                "test_assertions": assertions,
            },
            headers=api_env.headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["action_count"] == 1
        kwargs = engine.calls[0]["kwargs"]
        assert kwargs["start_step_id"] == "a_second001"
        assert kwargs["step_outputs"]["a_first001"]["file"] == "https://example.com"
        assert kwargs["test_assertions"] == assertions

    def test_partial_run_rejects_missing_referenced_upstream_field(self, api_env):
        engine = self._install_partial_engine(api_env)

        response = api_env.client.post(
            "/api/rules/0/run",
            json={
                "start_step_id": "a_second001",
                "step_outputs": {"a_first001": {"other": "value"}},
            },
            headers=api_env.headers,
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_test_payload"
        assert "file" in response.json()["details"][0]
        assert engine.calls == []

    def test_partial_run_rejects_non_object_step_outputs(self, api_env):
        engine = self._install_partial_engine(api_env)

        response = api_env.client.post(
            "/api/rules/0/run",
            json={"start_step_id": "a_second001", "step_outputs": []},
            headers=api_env.headers,
        )

        assert response.status_code == 400
        assert "上游动作结果必须是 JSON 对象" in response.json()["error"]
        assert engine.calls == []

    def test_partial_run_rejects_reversed_range(self, api_env):
        engine = self._install_partial_engine(api_env)

        response = api_env.client.post(
            "/api/rules/0/run",
            json={"start_step_id": "a_second001", "end_step_id": "a_first001"},
            headers=api_env.headers,
        )

        assert response.status_code == 400
        assert "起始动作不能晚于结束动作" in response.json()["error"]
        assert engine.calls == []

    def test_manual_run_rejects_unknown_trigger_context(self, api_env):
        engine = self._install_engine(api_env)

        response = api_env.client.post(
            "/api/rules/0/run",
            json={
                "trigger_payloads": {
                    "t_usb001": {"drive_letter": "E:"},
                    "t_unknown": {"secret": "value"},
                }
            },
            headers=api_env.headers,
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_test_payload"
        assert engine.calls == []

    def test_manual_run_rejects_oversized_test_context(self, api_env):
        engine = self._install_engine(api_env)

        response = api_env.client.post(
            "/api/rules/0/run",
            content=json.dumps({"event_payload": {"text": "x" * (1024 * 1024)}}),
            headers={**api_env.headers, "Content-Type": "application/json"},
        )

        assert response.status_code == 413
        assert engine.calls == []

    def test_manual_run_uses_verified_saved_snapshot(self, api_env):
        engine = self._install_engine(api_env)
        saved = api_env.api._load_rules()[0]
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
        stale = api_env.api._load_rules()[0]
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


class FakeComponent:
    """测试用组件，模拟“录制热键”式的一次调用采集"""

    def invoke(self, session, method, payload):
        if method == "capture":
            session.set_status("已采集")
            return {"ok": True, "data": {"hotkey": "Ctrl+Shift+M"}}
        if method == "accumulate":
            session.data.setdefault("items", []).append(payload)
            return {"ok": True, "data": {"items": list(session.data["items"])}}
        if method == "close":
            return {"ok": True, "close": True, "data": {}}
        return {"ok": False, "error": f"未知方法: {method}"}


def make_component_engine(component):
    return SimpleNamespace(
        actions_meta={
            "demo_action": {
                "id": "demo_action",
                "name": "演示动作",
                "description": "测试录制组件",
                "components": [{
                    "id": "record",
                    "name": "录制",
                    "entrypoint": "component.py",
                    "ui": {"button_label": "录制演示动作"},
                }],
            }
        },
        triggers_meta={},
        component=lambda plugin_id, component_id: (
            component if plugin_id == "demo_action" and component_id == "record" else None
        ),
    )


class TestComponentEndpoints:
    def test_list_requires_engine(self, api_env):
        response = api_env.client.get("/api/plugins/components", headers=api_env.headers)
        assert response.status_code == 200
        assert response.json()["components"] == []

    def test_list_and_invoke_flow(self, api_env):
        api_env.runner.current_engine = make_component_engine(FakeComponent())
        listed = api_env.client.get(
            "/api/plugins/components", headers=api_env.headers
        ).json()
        assert listed["components"][0]["plugin_id"] == "demo_action"
        assert listed["components"][0]["id"] == "record"
        assert listed["components"][0]["available"] is True
        assert listed["components"][0]["ui"]["button_label"] == "录制演示动作"

        captured = api_env.client.post(
            "/api/plugins/demo_action/components/record/invoke",
            json={"method": "capture"},
            headers=api_env.headers,
        )
        assert captured.status_code == 200
        body = captured.json()
        assert body["data"]["data"]["hotkey"] == "Ctrl+Shift+M"
        assert body["status"] == "已采集"
        session_id = body["session_id"]

        accumulated = api_env.client.post(
            "/api/plugins/demo_action/components/record/invoke",
            json={"method": "accumulate", "payload": {"step": 1}, "session_id": session_id},
            headers=api_env.headers,
        )
        assert accumulated.status_code == 200
        assert accumulated.json()["data"]["data"]["items"] == [{"step": 1}]

        closed = api_env.client.post(
            "/api/plugins/demo_action/components/record/invoke",
            json={"method": "close", "session_id": session_id},
            headers=api_env.headers,
        )
        assert closed.status_code == 200
        assert api_env.api._component_sessions.get(session_id) is None

    def test_unknown_action_or_session_rejected(self, api_env):
        api_env.runner.current_engine = make_component_engine(FakeComponent())
        response = api_env.client.post(
            "/api/plugins/missing/components/record/invoke",
            json={"method": "capture"},
            headers=api_env.headers,
        )
        assert response.status_code == 404

        response = api_env.client.post(
            "/api/plugins/demo_action/components/record/invoke",
            json={"method": "capture", "session_id": "not-a-session"},
            headers=api_env.headers,
        )
        assert response.status_code == 404

    def test_component_endpoints_require_auth(self, api_env):
        api_env.runner.current_engine = make_component_engine(FakeComponent())
        response = api_env.client.get("/api/plugins/components")
        assert response.status_code == 403
        response = api_env.client.post(
            "/api/plugins/demo_action/components/record/invoke"
        )
        assert response.status_code == 403

    def test_component_failure_returns_400(self, api_env):
        api_env.runner.current_engine = make_component_engine(FakeComponent())
        response = api_env.client.post(
            "/api/plugins/demo_action/components/record/invoke",
            json={"method": "nope"},
            headers=api_env.headers,
        )
        assert response.status_code == 400
        assert "未知方法" in response.json()["error"]
