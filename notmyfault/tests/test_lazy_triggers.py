"""规则驱动的触发器懒加载与重复启动行为（RED）

生产变更计划：PluginLoader 把触发器像动作一样推迟到物化阶段，
PluginRegistry 获得 resolve_trigger，AutomationEngine._start_trigger_threads
只物化规则引用的触发器，未被引用的触发器保持 pending 且不导入模块。

当前实现触发器在 load() 时全部立即导入，下面的测试先锁定期望行为，
生产代码改完后测试应转绿。
"""

import json
import sys
from pathlib import Path

import pytest

from notmyfault.core.engine import AutomationEngine

TRIGGER_ID_A = "t_lazy_a"
TRIGGER_ID_B = "t_lazy_b"
TRIGGER_ID_C = "t_lazy_c"
MODULE_PREFIX = "notmyfault.trigger_"

# 触发器线程只等停止信号，不产生真实事件
TRIGGER_CODE = (
    "def run(meta, config, emit_event, shutdown_event):\n"
    "    shutdown_event.wait()\n"
)


def make_meta(plugin_id: str) -> dict:
    return {
        "id": plugin_id,
        "name": f"懒加载触发器 {plugin_id}",
        "description": "测试用触发器",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": f"com.test.{plugin_id}",
        "permissions": [],
    }


def write_trigger(root: Path, plugin_id: str, py_code: str = TRIGGER_CODE) -> Path:
    folder = root / plugin_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "trigger.json").write_text(
        json.dumps(make_meta(plugin_id), ensure_ascii=False), encoding="utf-8"
    )
    (folder / "trigger.py").write_text(py_code, encoding="utf-8")
    return folder


def rule_with(trigger_type: str) -> dict:
    return {"name": f"规则-{trigger_type}", "event": {"type": trigger_type, "params": {}}}


def module_of(plugin_id: str) -> str:
    return f"{MODULE_PREFIX}{plugin_id}"


def make_engine(rules: list) -> AutomationEngine:
    engine = AutomationEngine({"rules": rules})
    engine._alert_user = lambda *a, **k: None
    return engine


def load_triggers(engine: AutomationEngine, root: Path) -> tuple[int, int]:
    return engine._load_plugins(
        base_dir=str(root),
        plugins_dir="triggers",
        json_filename="trigger.json",
        py_filename="trigger.py",
        module_prefix=MODULE_PREFIX,
        meta_store=engine.triggers_meta,
        func_store=engine.triggers_funcs,
        store_name="Trigger",
        origin="builtin",
    )


@pytest.fixture
def engine_with_three_triggers(tmp_path, monkeypatch):
    """引擎只引用 A，插件目录里有 A/B/C 三个触发器"""
    # 源码运行默认 strict，未签名临时插件会被拒，切到宽松模式
    monkeypatch.setenv("NOTMYFAULT_MODE", "alpha")
    engine = make_engine([rule_with(TRIGGER_ID_A)])
    triggers_root = tmp_path / "triggers"
    for plugin_id in (TRIGGER_ID_A, TRIGGER_ID_B, TRIGGER_ID_C):
        write_trigger(triggers_root, plugin_id)
    _, failed = load_triggers(engine, tmp_path)
    assert failed == 0
    yield engine
    engine.shutdown()


class TestRuleDrivenTriggerMaterialization:
    def test_start_materializes_only_referenced_trigger(self, engine_with_three_triggers):
        engine = engine_with_three_triggers
        registry = engine._plugin_registry

        started = engine._start_trigger_threads(rules=[rule_with(TRIGGER_ID_A)])
        assert started == 1

        # 规则引用的 A 被物化：入口函数、模块对象和 sys.modules 都真实存在
        assert TRIGGER_ID_A in engine.triggers_funcs
        assert registry.get_module(TRIGGER_ID_A) is not None
        assert module_of(TRIGGER_ID_A) in sys.modules

        # 未被引用的 B、C 保持 pending，不得导入模块、不得注册入口函数；
        # 元数据仍要在发现阶段入账，规则校验才能识别它们
        for plugin_id in (TRIGGER_ID_B, TRIGGER_ID_C):
            assert plugin_id in engine.triggers_meta
            assert plugin_id in registry.pending
            assert plugin_id not in engine.triggers_funcs
            assert registry.get_module(plugin_id) is None
            assert module_of(plugin_id) not in sys.modules

        # 只有 A 的触发器线程在运行
        assert set(engine._trigger_supervisor.health()) == {TRIGGER_ID_A}

    def test_repeat_start_materializes_newly_referenced_trigger(
        self, engine_with_three_triggers
    ):
        engine = engine_with_three_triggers
        registry = engine._plugin_registry

        engine._start_trigger_threads(rules=[rule_with(TRIGGER_ID_A)])

        # 第一次启动后 B 仍未物化，热重载等价的第二次启动才随新规则导入
        assert TRIGGER_ID_B not in engine.triggers_funcs
        assert registry.get_module(TRIGGER_ID_B) is None
        assert module_of(TRIGGER_ID_B) not in sys.modules

        # 规则换成 A+B 后再次启动，B 被物化
        started = engine._start_trigger_threads(
            rules=[rule_with(TRIGGER_ID_A), rule_with(TRIGGER_ID_B)]
        )
        assert started >= 1

        assert TRIGGER_ID_B in engine.triggers_funcs
        assert registry.get_module(TRIGGER_ID_B) is not None
        assert module_of(TRIGGER_ID_B) in sys.modules
        assert TRIGGER_ID_B not in registry.pending

        # C 始终未被引用，保持 pending 且未导入
        assert TRIGGER_ID_C in registry.pending
        assert TRIGGER_ID_C not in engine.triggers_funcs
        assert registry.get_module(TRIGGER_ID_C) is None
        assert module_of(TRIGGER_ID_C) not in sys.modules

        # A、B 都有运行中的线程，C 没有
        assert set(engine._trigger_supervisor.health()) == {TRIGGER_ID_A, TRIGGER_ID_B}
