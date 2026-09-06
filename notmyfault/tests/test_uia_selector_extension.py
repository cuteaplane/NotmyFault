import json
from pathlib import Path

import notmyfault.actions.uia_automation.selector_extension as selector_extension
from notmyfault.extensions.registry import ExtensionRegistry
from notmyfault.extensions.session import ExtensionContext, ExtensionSession


SELECTOR = {
    "version": 1,
    "window": {"process": "notepad.exe", "name": "无标题 - 记事本"},
    "target": {"name": "保存", "control_type": 50000},
    "display": {"control": "保存", "app": "notepad.exe"},
}


def _context():
    root = Path(selector_extension.__file__).parent
    meta = json.loads((root / "action.json").read_text(encoding="utf-8"))
    registry = ExtensionRegistry()
    registry.register_manifest("uia_automation", "action", meta, str(root))
    session = ExtensionSession(
        plugin_id="uia_automation",
        command_id="edit_selector",
        plugin_meta=meta,
        source_kind="parameter_editors",
        source_id="selector_editor",
        allowed_commands={
            "capture_selector",
            "verify_selector",
            "commit_selector",
            "cancel_selector",
        },
        data_type=registry.data_type("uia_automation", "uia_selector"),
        current_value=None,
    )
    return ExtensionContext(session, registry)


def test_capture_and_commit_plugin_owned_selector(monkeypatch):
    context = _context()
    monkeypatch.setattr(selector_extension.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        selector_extension,
        "capture_element_under_cursor",
        lambda: SELECTOR,
    )

    opened = selector_extension.edit_selector(context, {})
    assert opened["ok"] is True
    assert opened["view"] == "selector_workbench"

    captured = selector_extension.capture_selector(context, {})
    result = selector_extension.commit_selector(context, {})

    assert captured["data"]["selector"] == SELECTOR
    assert result["ok"] is True
    assert result["value"]["$type"] == (
        "io.github.notmyfault.uia_automation/uia_selector@1"
    )
    assert result["value"]["data"] == SELECTOR


def test_check_uses_current_plugin_data(monkeypatch):
    context = _context()
    context.session.current_value = SELECTOR
    monkeypatch.setattr(
        selector_extension,
        "check_selector",
        lambda selector: {"ok": selector == SELECTOR},
    )

    result = selector_extension.verify_selector(context, {})

    assert result["ok"] is True
    assert result["data"]["check"] == {"ok": True}
