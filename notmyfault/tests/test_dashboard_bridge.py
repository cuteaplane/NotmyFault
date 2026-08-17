import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_dashboard_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace())
    path = Path(__file__).resolve().parents[2] / "dashboard.pyw"
    loader = importlib.machinery.SourceFileLoader("dashboard_bridge_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_first_rule_save_checks_approval_without_loading_missing_file(
    tmp_path, monkeypatch
):
    import notmyfault.config as config_mod
    from notmyfault.security import rule_approval

    dashboard = load_dashboard_module(monkeypatch)
    rules_file = tmp_path / "rules.json"
    monkeypatch.setattr(dashboard, "RULES_FILE", str(rules_file))
    monkeypatch.setattr(
        dashboard,
        "_get_plugins_schema",
        lambda: {
            "triggers": {"usb_insert": {"permissions": []}},
            "actions": {"notify": {"permissions": []}},
        },
    )
    monkeypatch.setattr(
        config_mod,
        "load_verified_rules",
        lambda: (_ for _ in ()).throw(AssertionError("不应读取不存在的规则文件")),
    )
    saved = []
    monkeypatch.setattr(config_mod, "save_rules", lambda rules: saved.append(rules) or True)
    approvals = []
    monkeypatch.setattr(
        rule_approval,
        "require_admin_rule_approval",
        lambda previous, current, schema, password: approvals.append(
            (previous, current, schema, password)
        ),
    )
    rule = {
        "name": "首条规则",
        "event": {"type": "usb_insert", "params": {}},
        "actions": [{"type": "notify", "params": {}}],
    }

    result = dashboard.DashboardAPI().save_config([rule], "one-time-password")

    assert result["ok"] is True
    assert approvals[0][0] == []
    assert approvals[0][3] == "one-time-password"
    assert saved == [result["rules"]]


def test_ai_draft_bridge_waits_longer_than_regular_requests(monkeypatch):
    dashboard = load_dashboard_module(monkeypatch)
    api = dashboard.DashboardAPI()
    monkeypatch.setattr(api, "_get_api_token", lambda: "test-token")
    observed_timeouts = []

    class FakeResponse:
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        observed_timeouts.append(timeout)
        return FakeResponse()

    monkeypatch.setattr(dashboard.urllib.request, "urlopen", fake_urlopen)

    api.request_api(
        "/api/rules/draft/ai",
        "POST",
        {"description": "每天九点提醒我", "api_key": "session-key"},
    )
    api.request_api("/api/settings/ai-drafting", "GET")

    assert observed_timeouts == [30, 5]
