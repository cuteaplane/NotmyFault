import pytest

from notmyfault.host.ai_proposals import parse_plugin_proposal
from notmyfault.host.ai_tools import ToolCallError


class TestProposalIdSyntax:
    @pytest.mark.parametrize("plugin_id", [
        "a.b", "a b", "a/b", "-lead", "_lead", "路径", "a\\b", "a:b",
    ])
    def test_rejects_invalid_id_syntax(self, plugin_id):
        args = {
            "kind": "action", "id": plugin_id, "name": "x", "description": "x",
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    @pytest.mark.parametrize("plugin_id", [
        "motion", "motion_detect", "motion-detect", "Motion123", "9lives", "a",
    ])
    def test_accepts_valid_id_syntax(self, plugin_id):
        result = parse_plugin_proposal({
            "kind": "action", "id": plugin_id, "name": "x", "description": "x",
        })
        assert result["id"] == plugin_id


class TestProposalItems:
    def test_parameters_items_are_normalized(self):
        result = parse_plugin_proposal({
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "parameters": [
                {"name": "url", "type": "string", "label": "网址"},
                {"name": "count", "type": "number", "label": "次数"},
            ],
        })
        assert result["parameters"] == [
            {"name": "url", "type": "string", "label": "网址"},
            {"name": "count", "type": "number", "label": "次数"},
        ]

    def test_outputs_items_are_normalized(self):
        result = parse_plugin_proposal({
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "outputs": [{"name": "status", "type": "object", "label": "状态"}],
        })
        assert result["outputs"] == [
            {"name": "status", "type": "object", "label": "状态"},
        ]

    @pytest.mark.parametrize("bad_type", ["evil", "str", "STRING", ""])
    def test_rejects_unknown_parameter_type(self, bad_type):
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "parameters": [{"name": "url", "type": bad_type, "label": "网址"}],
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    @pytest.mark.parametrize("bad_type", ["evil", "list", "objectish"])
    def test_rejects_unknown_output_type(self, bad_type):
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "outputs": [{"name": "status", "type": bad_type, "label": "状态"}],
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    @pytest.mark.parametrize("field", ["source", "handler"])
    def test_rejects_executable_parameter_item_field(self, field):
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "parameters": [{
                "name": "url", "type": "string", "label": "网址", field: "x",
            }],
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    def test_rejects_parameter_metadata_on_output_item(self):
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "outputs": [{
                "name": "status", "type": "string", "label": "状态",
                "default": "ready",
            }],
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    def test_rejects_non_object_item(self):
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "parameters": ["not-an-object"],
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    @pytest.mark.parametrize("missing", ["name", "type", "label"])
    def test_rejects_missing_item_field(self, missing):
        item = {"name": "url", "type": "string", "label": "网址"}
        del item[missing]
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "parameters": [item],
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    @pytest.mark.parametrize("field", ["name", "type", "label"])
    def test_rejects_empty_item_field(self, field):
        item = {"name": "url", "type": "string", "label": "网址"}
        item[field] = ""
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "parameters": [item],
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    def test_items_are_copied_not_shared(self):
        params = [{"name": "url", "type": "string", "label": "网址"}]
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "parameters": params,
        }
        result = parse_plugin_proposal(args)
        result_params = result["parameters"]
        assert isinstance(result_params, list)
        assert result_params[0] is not params[0]
        params[0]["name"] = "改了"
        assert result_params[0] == {"name": "url", "type": "string", "label": "网址"}
