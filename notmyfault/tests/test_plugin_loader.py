"""插件加载器的引擎兼容导出与注册表原子性测试。"""

from notmyfault.core import engine
from notmyfault.security import plugin_loader
from notmyfault.security.plugin_loader import PluginRegistry

_COMPAT_EXPORTS = [
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


def test_engine_keeps_plugin_loader_compatibility_exports():
    for name in _COMPAT_EXPORTS:
        assert getattr(engine, name) is getattr(plugin_loader, name)


def test_plugin_registry_registers_and_unregisters_atomically():
    registry = PluginRegistry()

    def trigger_run(meta, config, emit, shutdown_event=None):
        pass

    def action_run(action_info, params):
        pass

    trigger_meta = {"id": "demo_trigger"}
    action_meta = {"id": "demo_action"}
    trigger_module = object()
    action_module = object()

    registry.register("trigger", "demo_trigger", trigger_meta, trigger_run, trigger_module)
    registry.register("action", "demo_action", action_meta, action_run, action_module)

    assert registry.triggers_meta["demo_trigger"] is trigger_meta
    assert registry.triggers_funcs["demo_trigger"] is trigger_run
    assert registry.actions_meta["demo_action"] is action_meta
    assert registry.actions_funcs["demo_action"] is action_run
    assert registry.get_module("demo_trigger") is trigger_module
    assert registry.get_module("demo_action") is action_module

    registry.unregister("trigger", "demo_trigger")
    registry.unregister("action", "demo_action")

    assert "demo_trigger" not in registry.triggers_meta
    assert "demo_trigger" not in registry.triggers_funcs
    assert "demo_action" not in registry.actions_meta
    assert "demo_action" not in registry.actions_funcs
    assert registry.get_module("demo_trigger") is None
    assert registry.get_module("demo_action") is None

    # 重复卸载不存在的插件不应抛异常
    registry.unregister("trigger", "demo_trigger")
