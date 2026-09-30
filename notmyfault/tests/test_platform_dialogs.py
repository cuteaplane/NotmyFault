"""platform_support 对话框后端选择与命令构造。"""

from types import SimpleNamespace

import notmyfault.platform.platform_support as support


def test_linux_message_prefers_zenity(monkeypatch):
    monkeypatch.setattr(support.sys, "platform", "linux")
    monkeypatch.setattr(
        support.shutil,
        "which",
        lambda name: "/usr/bin/zenity" if name == "zenity" else None,
    )
    calls = []

    def fake_run(command, timeout=600, check=False):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(support.subprocess, "run", fake_run)
    assert support.show_message_dialog("标题", "正文") is True
    assert calls == [
        ["/usr/bin/zenity", "--info", "--title=标题", "--text=正文", "--width=360"]
    ]


def test_linux_input_cancel_and_confirm(monkeypatch):
    monkeypatch.setattr(support.sys, "platform", "linux")
    monkeypatch.setattr(
        support.shutil,
        "which",
        lambda name: "/usr/bin/zenity" if name == "zenity" else None,
    )

    def fake_run(command, capture_output=False, timeout=600, check=False):
        assert capture_output is True
        if "--entry-text=预填" in command:
            return SimpleNamespace(returncode=0, stdout="回答\n".encode("utf-8"), stderr=b"")
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(support.subprocess, "run", fake_run)
    confirmed = support.ask_text_dialog("标题", "说明", "预填")
    assert confirmed == {"available": True, "confirmed": True, "text": "回答"}

    cancelled = support.ask_text_dialog("标题", "说明", "")
    assert cancelled == {"available": True, "confirmed": False, "text": ""}


def test_linux_dialog_unavailable_without_backends(monkeypatch):
    monkeypatch.setattr(support.sys, "platform", "linux")
    monkeypatch.setattr(support.shutil, "which", lambda name: None)
    assert support.show_message_dialog("t", "m") is False
    assert support.ask_text_dialog("t", "m") == {
        "available": False,
        "confirmed": False,
        "text": "",
    }
