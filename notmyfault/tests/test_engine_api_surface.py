"""钉住 engine 模块的对外名字，拆分 engine.py 时导出和成员不能丢。

兼容导出名单与 tests/test_plugin_loader.py 的 _COMPAT_EXPORTS 保持一致，
改名单要两边同步。
"""

from notmyfault.core import engine
from notmyfault.core.engine import AutomationEngine
from notmyfault.security import plugin_loader

# 旧代码 from notmyfault.core.engine import ... 在用这些名字
_PLUGIN_LOADER_COMPAT_EXPORTS = [
    "PluginKind",
    "PluginLoader",
    "PluginRegistry",
    "_check_sudo_import",
    "_validate_plugin_meta",
    "check_permissions_conform",
    "check_sudo_import",
    "is_known_permission",
    "scan_plugin_capabilities",
    "validate_plugin_meta",
    "verify_plugin_integrity",
    "verify_plugin_sig",
]

_CLASS_METHODS = [
    "_run_trigger",
    "_validate_all_rules",
    "emit_event",
    "call_notmyfault",
    "execute_workflow",
    "run_manual_rule_snapshot",
    "get_diagnostics",
    "cancel_run",
    "start",
    "shutdown",
    "auto_load",
    # 转发到 AdminSessionManager，测试和 _run 都直接调用老名字
    "_required_admin_plugins",
    "_authorize_admin_rules_at_startup",
    "_recheck_admin_session",
    # 插件组件入口，API server 用它取录制器等组件
    "component",
]

_INSTANCE_ATTRS = [
    "_diag_obj",
    "_shutdown_flag",
    "_plugin_modules",
    "_sudo",
    "_manual_threads",
    "_trigger_threads",
    "_trigger_events",
    "_trigger_lock",
    "_trigger_supervisor",
    "_admin_session",
    "_event_bus",
    "_hot_reloader",
    "_condition_runtime",
    "_alert_user",
    "actions_meta",
    "triggers_meta",
    "admin_authorization_mode",
]


def test_engine_reexports_plugin_loader_names():
    for name in _PLUGIN_LOADER_COMPAT_EXPORTS:
        assert hasattr(engine, name), name
        # 转发的是同一个对象，不是复制一份
        assert getattr(engine, name) is getattr(plugin_loader, name), name


def test_engine_module_has_rules_file():
    assert hasattr(engine, "RULES_FILE")


def test_automation_engine_class_methods():
    for name in _CLASS_METHODS:
        assert callable(getattr(AutomationEngine, name)), name


def test_automation_engine_instance_attrs():
    instance = AutomationEngine({"rules": []})
    instance._alert_user = lambda *a, **k: None
    for name in _INSTANCE_ATTRS:
        assert hasattr(instance, name), name
