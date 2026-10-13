"""event-v2 触发器协议测试：清单声明、契约校验、配置匹配、实例管理与绑定。"""

import importlib.util
import json
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from notmyfault.core.bindings import BindingResolutionError, resolve_reference
from notmyfault.tests.api_support import create_test_engine
from notmyfault.tests.canvas_fixtures import native_rule
from notmyfault.core.rules import ConditionRuntime, config_fingerprint
from notmyfault.core.workflow import build_context
from notmyfault.security.plugin_schema import check_payload_contract, validate_plugin_meta

PKG_ROOT = Path(__file__).resolve().parents[1]
TRIGGERS_DIR = PKG_ROOT / "triggers"


def load_meta(name):
    return json.loads((TRIGGERS_DIR / name / "trigger.json").read_text(encoding="utf-8"))


def load_trigger_module(name):
    path = TRIGGERS_DIR / name / "trigger.py"
    spec = importlib.util.spec_from_file_location(f"v2_trigger_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def builtin_trigger_metas():
    metas = {}
    for folder in sorted(TRIGGERS_DIR.iterdir()):
        meta_file = folder / "trigger.json"
        if folder.is_dir() and meta_file.exists():
            metas[folder.name] = json.loads(meta_file.read_text(encoding="utf-8"))
    return metas


def make_engine(rule):
    engine = create_test_engine({"rules": [rule] if rule else []})
    return engine


class TriggerV2Tests:
    def test_all_builtin_triggers_declare_the_v2_entrypoint(self):
        metas = builtin_trigger_metas()
        assert metas, "未找到任何内置触发器"
        for name, meta in metas.items():
            assert meta.get("trigger_api") == "event-v2", f"{name} 未声明 event-v2 入口"

    def test_v2_manifest_is_valid(self):
        for name, meta in builtin_trigger_metas().items():
            is_valid, errors = validate_plugin_meta(meta, "trigger")
            assert is_valid, f"{name} 清单校验失败: {errors}"
            # 声明了 outputs 的触发器必须用列表描述输出契约
            if "outputs" in meta:
                assert isinstance(meta["outputs"], list), f"{name} outputs 必须是列表"

    def test_engine_binds_v2_payload_to_plugin_trigger_id(self):
        rule = native_rule({
                "type": "demo_trigger",
                "binding_id": "t_demo001",
                "params": {"watch": "folder_a"},
            }, [{"type": "capture", "params": {}}], name="v2 绑定规则")
        engine = make_engine(rule)
        engine.triggers_meta["demo_trigger"] = {
            "trigger_api": "event-v2",
            "semantic": "oneshot",
            "outputs": [{"name": "value", "type": "number"}],
        }
        engine.actions_funcs["capture"] = lambda meta, params: None
        engine.actions_meta["capture"] = {}

        engine.emit_event(
            "demo_trigger", {"value": 42}, instance={"config": {"watch": "folder_a"}}
        )

        assert engine._condition_runtime.last_match("v2 绑定规则") == []



class TriggerConfigBindingTests:
    def test_trigger_config_reference_resolves(self):
        context = {
            "triggers": {
                "t_folder": {
                    "type": "folder_monitor",
                    "payload": {"event": "created"},
                    "config": {"folder_path": "D:/watch", "event_type": "created"},
                }
            }
        }
        value = resolve_reference(
            {"scope": "trigger_config", "node": "t_folder", "path": ["folder_path"]},
            context,
            location="actions[0].params.target",
        )
        assert value == "D:/watch"

    def test_context_snapshots_trigger_config(self):
        leaf_params = {"folder_path": "D:/watch"}
        condition_events = [
            {
                "binding_id": "t_folder",
                "event": {"type": "folder_monitor", "params": leaf_params},
                "payload": {"event": "created"},
            }
        ]
        context = build_context("规则", "folder_monitor", {"event": "created"}, condition_events)

        leaf_params["folder_path"] = "被篡改"

        assert context["triggers"]["t_folder"]["config"] == {"folder_path": "D:/watch"}

    def test_trigger_config_missing_config_reports_error(self):
        context = {
            "triggers": {
                "t_folder": {"type": "folder_monitor", "payload": {"event": "created"}}
            }
        }
        with pytest.raises(BindingResolutionError) as excinfo:
            resolve_reference(
                {"scope": "trigger_config", "node": "t_folder", "path": ["folder_path"]},
                context,
                location="actions[0].params.target",
            )
        assert excinfo.value.code == "missing_binding_value"


class V2ConfigValidationTests:
    @pytest.mark.parametrize("resource", ["cpu", "memory", "disk", "network"])
    @pytest.mark.parametrize("disk_path", ["omitted", "", " provided\u3000", "\x00", None, 7])
    def test_system_resource_optional_disk_path(self, monkeypatch, resource, disk_path):
        from types import SimpleNamespace
        from notmyfault.core.runtime_rules import prepare_rules

        module = load_trigger_module("system_resource")
        meta = load_meta("system_resource")
        params = {"resource": resource}
        if disk_path != "omitted":
            params["disk_path"] = disk_path
        rule = native_rule({"type": "system_resource", "params": params}, [{"type": "noop", "params": {}}], name="资源监控", rule_id="r_resource1")
        prepared = prepare_rules([rule], {"system_resource": meta}, {"noop": {"params": []}})
        valid = isinstance(disk_path, str) and "\x00" not in disk_path
        assert len(prepared) == int(valid), prepared.issues
        if not valid:
            assert any("disk_path" in issue for _name, issue in prepared.issues)
            return
        config = prepared.trigger_params["system_resource"][0]
        trigger = module.SystemResourceTrigger(meta, config, lambda payload: None, threading.Event())
        trigger.validate()
        expected = None if disk_path in ("omitted", "") else disk_path
        assert trigger.disk_path == expected
        if resource == "disk":
            calls = []
            monkeypatch.setattr(module.psutil, "disk_usage", lambda path: calls.append(path) or SimpleNamespace(percent=95))
            trigger.setup()
            trigger.poll()
            default = module.os.environ.get("SystemDrive", "C:") + module.os.sep if module.os.name == "nt" else "/"
            assert calls == [expected if expected is not None else default]

    def test_invalid_folder_event_type_raises(self):
        module = load_trigger_module("folder_monitor")
        with pytest.raises(ValueError, match="无效的事件类型"):
            module.run(
                {"id": "folder_monitor"},
                {"event_type": "renamed"},
                lambda payload: None,
                threading.Event(),
            )

    def test_invalid_state_values_raise(self):
        module = load_trigger_module("process_state")
        with pytest.raises(ValueError, match="无效的进程状态"):
            module.run(
                {"id": "process_state"},
                {"process_name": "WeChat.exe", "state": "paused"},
                lambda payload: None,
                threading.Event(),
            )

    def test_invalid_system_resource_values_raise(self):
        module = load_trigger_module("system_resource")
        meta = {"id": "system_resource"}
        noop = lambda payload: None  # noqa: E731
        with pytest.raises(ValueError, match="无效的资源类型"):
            module.run(meta, {"resource": "gpu"}, noop, threading.Event())
        with pytest.raises(ValueError, match="无效的阈值方向"):
            module.run(
                meta,
                {"resource": "cpu", "direction": "sideways"},
                noop,
                threading.Event(),
            )
        with pytest.raises(ValueError, match="threshold 必须是数字"):
            module.run(
                meta,
                {"resource": "cpu", "threshold": "abc"},
                noop,
                threading.Event(),
            )


class V2ContractTests:
    OUTPUTS = [
        {"name": "event", "label": "变化类型", "type": "string"},
        {"name": "count", "label": "数量", "type": "number"},
    ]

    def test_payload_matching_outputs_passes(self):
        problems = check_payload_contract(self.OUTPUTS, {"event": "created", "count": 3})
        assert problems == []

    def test_missing_required_output_is_rejected(self):
        problems = check_payload_contract(self.OUTPUTS, {"event": "created"})
        assert any("缺少必填输出字段: count" in problem for problem in problems)

    def test_optional_missing_is_allowed(self):
        outputs = [{"name": "note", "type": "string", "required": False}]
        assert check_payload_contract(outputs, {}) == []

    def test_wrong_simple_type_is_rejected(self):
        problems = check_payload_contract(
            [{"name": "flag", "type": "bool"}], {"flag": "yes"}
        )
        assert any("应为 bool" in problem for problem in problems)

        problems = check_payload_contract(
            [{"name": "count", "type": "number"}], {"count": True}
        )
        assert any("应为 number" in problem for problem in problems)

    def test_unknown_field_is_rejected(self):
        problems = check_payload_contract(
            self.OUTPUTS, {"event": "created", "count": 1, "extra": 0}
        )
        assert any("未声明的输出字段: extra" in problem for problem in problems)




class V2MatchingTests:
    def _v2_rule(self):
        return {
            "name": "v2 匹配",
            "_matcher": {
                "op": "any",
                "children": [
                    {"type": "folder_monitor", "binding_id": "t_alpha01",
                     "params": {"folder_path": "A"}},
                    {"type": "folder_monitor", "binding_id": "t_beta02",
                     "params": {"folder_path": "B"}},
                ],
            },
            "actions": [],
        }

    @pytest.mark.parametrize("extra", [{}, {"amount": Decimal("1.20"), "data": b"\x00\xff", "day": date(2024, 2, 29)}])
    def test_v2_config_matching_hits_own_leaf(self, extra):
        runtime = ConditionRuntime()
        rule = self._v2_rule()
        for leaf in rule["_matcher"]["children"]:
            leaf["params"].update(extra)
        matched = runtime.match(
            "rule:1", rule, "folder_monitor", {"event": "created"},
            instance={"config": {"folder_path": "B", **extra}},
        )
        assert matched is True
        hits = runtime.last_match("rule:1")
        assert [item["binding_id"] for item in hits] == ["t_beta02"]

    def test_v2_config_mismatch_does_not_hit(self):
        runtime = ConditionRuntime()
        matched = runtime.match(
            "rule:1", self._v2_rule(), "folder_monitor", {"event": "created"},
            instance={"config": {"folder_path": "C"}},
        )
        assert matched is False

    @pytest.mark.parametrize("expected,actual,matched", [
        ("running", "running", True), ("running", "stopped", False),
        (True, True, True), (False, False, True),
        (True, 1, False), (False, 0, False),
        ([True], [1], False), ({"ready": [False]}, {"ready": [0]}, False),
        ({"ready": [False]}, {"ready": [False]}, True),
        (1, 1.0, True), (Decimal("1.0"), 1, True), (None, None, True),
    ])
    def test_v1_payload_filtering_still_works(self, expected, actual, matched):
        rule = {
            "name": "v1 过滤",
            "_matcher": {
                "type": "process_state",
                "binding_id": "t_proc001",
                "params": {"process_name": "WeChat.exe", "state": expected},
            },
            "actions": [],
        }
        runtime = ConditionRuntime()
        assert runtime.match(
            "rule:1", rule, "process_state",
            {"process_name": "WeChat.exe", "state": actual},
        ) is matched

        runtime = ConditionRuntime()
        assert runtime.match(
            "rule:2", rule, "process_state",
            {"process_name": "Other.exe", "state": actual},
        ) is False

        engine = make_engine(native_rule(rule["_matcher"], [], name=rule["name"]))
        engine.triggers_meta["process_state"] = {
            "trigger_api": "event-v1",
            "outputs": [
                {"name": "process_name", "type": "string"},
                {"name": "state", "value_type": "any"},
            ],
        }
        dispatched = []
        engine._event_bus._scheduler_submit_fn = lambda *args: dispatched.append(args)

        engine.emit_event("process_state", {"process_name": "Other.exe", "state": actual})
        engine.emit_event("process_state", {"process_name": "WeChat.exe", "state": actual})
        assert len(dispatched) == int(matched)
        if matched:
            assert dispatched[0][3]["event"]["payload"]["state"] == actual

    def test_v2_and_v1_semantics_coexist_in_one_rule(self):
        rule = {
            "name": "混合语义",
            "_matcher": {
                "op": "all",
                "children": [
                    {"type": "folder_monitor", "binding_id": "t_fold001",
                     "params": {"folder_path": "A"}},
                    {"type": "process_state", "binding_id": "t_proc002",
                     "params": {"process_name": "WeChat.exe", "state": "running"}},
                ],
            },
            "actions": [],
        }
        runtime = ConditionRuntime()
        assert runtime.match(
            "rule:1", rule, "folder_monitor", {"event": "created"},
            instance={"config": {"folder_path": "A"}},
        ) is False
        assert runtime.match(
            "rule:1", rule, "process_state",
            {"process_name": "WeChat.exe", "state": "running"},
        ) is True


class V2OptimizationTests:
    def test_fingerprint_normalizes_numeric_notation(self):
        assert config_fingerprint({"threshold": 90.0}) == config_fingerprint({"threshold": 90})
        assert config_fingerprint({"b": 2, "a": 1}) == config_fingerprint({"a": 1, "b": 2})
        assert config_fingerprint({"items": [1.0, 2]}) == config_fingerprint({"items": (1, 2.0)})
