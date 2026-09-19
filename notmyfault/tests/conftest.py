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
