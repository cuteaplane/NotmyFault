"""ask_user 动作：消息框与输入框参数及平台分发。"""

import importlib.util
import json
from pathlib import Path

import pytest

from notmyfault.security.plugin_schema import validate_plugin_meta


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "ask_user"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("ask_user_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_is_cross_platform_native_dialog():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    valid, errors = validate_plugin_meta(meta, "action")
    assert valid, errors
    assert meta["id"] == "ask_user"
    assert meta["name"] == "询问用户"
    assert meta["platforms"] == ["windows", "linux"]
    assert "native_api" in meta["permissions"]


def test_unknown_mode_rejected():
    module = load_module()
    with pytest.raises(ValueError, match="未知模式"):
        module.run({}, {"mode": "confirm"})


def test_message_mode_uses_show_message_dialog(monkeypatch):
    module = load_module()
    calls = []

    def fake_show(title, message):
        calls.append((title, message))
        return True

    monkeypatch.setattr(module, "show_message_dialog", fake_show)
    result = module.run(
        {},
        {"mode": "message", "title": "提示", "message": "请确认继续"},
    )
    assert result == {
        "mode": "message",
        "title": "提示",
        "ok": True,
        "confirmed": True,
        "text": "",
        "error": "",
    }
    assert calls == [("提示", "请确认继续")]


def test_message_mode_soft_fails_when_dialog_unavailable(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "show_message_dialog", lambda *a, **k: False)
    result = module.run({}, {"mode": "message", "title": "t", "message": "m"})
    assert result["ok"] is False
    assert result["confirmed"] is False
    assert "对话框" in result["error"]


def test_input_mode_returns_text_on_confirm(monkeypatch):
    module = load_module()

    def fake_ask(title, message, default=""):
        assert title == "输入"
        assert message == "请填写名称"
        assert default == "默认"
        return {"available": True, "confirmed": True, "text": "小明"}

    monkeypatch.setattr(module, "ask_text_dialog", fake_ask)
    result = module.run(
        {},
        {
            "mode": "input",
            "title": "输入",
            "message": "请填写名称",
            "default": "默认",
        },
    )
    assert result["ok"] is True
    assert result["confirmed"] is True
    assert result["text"] == "小明"
    assert result["error"] == ""


def test_input_mode_cancel_keeps_ok_when_backend_available(monkeypatch):
    module = load_module()
    monkeypatch.setattr(
        module,
        "ask_text_dialog",
        lambda *a, **k: {"available": True, "confirmed": False, "text": ""},
    )
    result = module.run({}, {"mode": "input", "title": "t", "message": "m"})
    assert result["ok"] is True
    assert result["confirmed"] is False
    assert result["text"] == ""
    assert result["error"] == ""


def test_input_mode_soft_fails_without_backend(monkeypatch):
    module = load_module()
    monkeypatch.setattr(
        module,
        "ask_text_dialog",
        lambda *a, **k: {"available": False, "confirmed": False, "text": ""},
    )
    result = module.run({}, {"mode": "input", "title": "t", "message": "m"})
    assert result["ok"] is False
    assert "对话框" in result["error"]
