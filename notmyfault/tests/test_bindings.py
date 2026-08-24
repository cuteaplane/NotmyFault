"""数据绑定：旧 event.payload 模板与结构化 $ref 的可用性判断"""

from notmyfault.core.bindings import (
    iter_legacy_event_payload_paths,
    references_available,
    resolve_value,
    unavailable_references,
)


class LegacyBindingTests:
    def test_legacy_event_payload_paths_include_nested_and_full_payload(self):
        value = {
            "text": "{{ event.payload.file_path }}",
            "whole": "{{ event.payload }}",
            "nested": [{"x": "{{ event.payload.info.name }}"}],
            "other": "{{ triggers.t1 }}",
        }
        paths = list(iter_legacy_event_payload_paths(value))
        assert ("file_path",) in paths
        assert () in paths
        assert ("info", "name") in paths
        # 非 event.payload 的旧模板不在手动输入清单里
        assert len(paths) == 3

    def test_legacy_event_payload_resolves_with_manual_context(self):
        context = {"event": {"payload": {"file_path": "D:\\x.txt"}}}
        assert (
            resolve_value("{{ event.payload.file_path }}", context) == "D:\\x.txt"
        )
        assert resolve_value("{{ event.payload }}", context) == {"file_path": "D:\\x.txt"}


class StructuredBindingTests:
    def _ref(self, scope, node=None, path=None):
        ref = {"scope": scope, "path": path or []}
        if node is not None:
            ref["node"] = node
        return {"$ref": ref}

    def test_conditional_trigger_source_is_valid_for_action(self):
        # any 分支的触发器只要实际命中并进入上下文，动作就可以使用
        context = {"triggers": {"t_usb": {"payload": {"drive": "E:"}}}, "steps": {}}
        assert references_available(self._ref("trigger", "t_usb", ["drive"]), context)

    def test_action_with_unavailable_source_is_skipped(self):
        context = {"triggers": {}, "steps": {}}
        assert not references_available(
            self._ref("trigger", "t_usb", ["drive"]), context
        )

    def test_downstream_action_is_skipped_after_source_step_is_skipped(self):
        context = {"steps": {"s1": {"status": "skipped", "result": {}}}}
        assert not references_available(
            self._ref("step", "s1", ["url"]), context
        )

    def test_unavailable_references_keep_parameter_location_and_source(self):
        value = {
            "message": self._ref("trigger", "t_usb", ["drive"]),
            "url": self._ref("step", "s1", ["url"]),
        }
        missing = unavailable_references(value, {"triggers": {}, "steps": {}})

        assert [(item.location, item.reference["node"]) for item in missing] == [
            ("$.message", "t_usb"),
            ("$.url", "s1"),
        ]

    def test_reference_availability_only_checks_source_participation(self):
        # 来源参与了运行即算可用，具体路径存不存在留到运行时报错
        context = {"triggers": {"t_usb": {"payload": {}}}, "steps": {}}
        assert references_available(
            self._ref("trigger", "t_usb", ["not", "there"]), context
        )
