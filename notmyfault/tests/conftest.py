import os
import sys
import pytest

# notmyfault/tests 位于包内，需把项目根加入 sys.path 才能导入 notmyfault 包
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True)
def close_static_rules_stores(monkeypatch):
    from notmyfault.tests.api_support import StaticRulesStore

    stores = []
    initialize = StaticRulesStore.__init__

    def create(store, rules):
        initialize(store, rules)
        stores.append(store)

    monkeypatch.setattr(StaticRulesStore, "__init__", create)
    yield
    for store in stores:
        store.close()


@pytest.fixture(autouse=True)
def stub_desktop_alerts(monkeypatch):
    """单元测试不弹真实系统通知：WinRT 并发时会偶发访问越界，直接把测试进程打死"""
    import notmyfault.host.alert as alert_module
    import notmyfault.platform.platform_support as platform_support

    monkeypatch.setattr(
        alert_module,
        "alert_user",
        lambda title, message, open_dashboard=True, display_seconds=15: None,
    )
    monkeypatch.setattr(
        platform_support,
        "show_notification",
        lambda title, message: True,
    )


@pytest.fixture(autouse=True)
def close_hosted_samplers(close_static_rules_stores):
    from notmyfault.tests.plugin_support import _hosted_samplers

    yield
    for engine in _hosted_samplers:
        engine.shutdown(timeout=3)
    _hosted_samplers.clear()
