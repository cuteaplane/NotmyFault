"""组件会话：状态保持、会话管理和超时清理"""

import time

from notmyfault.components.session import (
    ComponentSession,
    ComponentSessionManager,
)


class TestComponentSession:
    def test_data_and_status(self):
        session = ComponentSession("demo_action", "record", {"id": "demo_action"})
        session.data["steps"] = [{"kind": "click"}]
        session.set_status("已录下 1 步")
        assert session.data["steps"][0]["kind"] == "click"
        assert session.status == "已录下 1 步"
        assert session.plugin_id == "demo_action"
        assert session.component_id == "record"

    def test_mark_finalized(self):
        session = ComponentSession("demo_action", "record", {})
        session.mark_finalized()
        assert session.status == ""


class TestComponentSessionManager:
    def test_create_get_drop(self):
        manager = ComponentSessionManager()
        session = manager.create("demo_action", "record", {"id": "demo_action"})
        assert manager.get(session.session_id) is session
        assert manager.drop(session.session_id) is session
        assert manager.get(session.session_id) is None

    def test_expired_session_is_cleaned(self):
        manager = ComponentSessionManager(ttl_seconds=0.05)
        session = manager.create("demo_action", "record", {})
        time.sleep(0.2)
        assert manager.get(session.session_id) is None
        assert manager.list_active() == []

    def test_get_touches_active_session(self):
        manager = ComponentSessionManager(ttl_seconds=0.2)
        session = manager.create("demo_action", "record", {})
        time.sleep(0.1)
        assert manager.get(session.session_id) is session
        time.sleep(0.1)
        # 中间那次 get 刷新了活动时间，会话应该还活着。
        assert manager.get(session.session_id) is session
