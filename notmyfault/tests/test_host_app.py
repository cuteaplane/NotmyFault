"""宿主首次运行安全模式通知"""

from notmyfault.host import alert, app as host_app


def test_notify_first_run_mode_calls_alert_user(monkeypatch):
    calls = []
    monkeypatch.setattr(
        alert, "alert_user",
        lambda title, message, open_dashboard=True: calls.append(
            (title, message, open_dashboard)
        ),
    )
    host_app._notify_first_run_mode("permissive")
    assert len(calls) == 1
    title, message, open_dashboard = calls[0]
    assert title == "NotmyFault 首次运行"
    assert "permissive" in message
    assert open_dashboard is False


def test_notify_first_run_mode_survives_alert_failure(monkeypatch, capsys):
    def broken(title, message, open_dashboard=True):
        raise RuntimeError("通知组件不可用")

    monkeypatch.setattr(alert, "alert_user", broken)
    host_app._notify_first_run_mode("develop（normal）")
    assert "无法发送安全模式提示" in capsys.readouterr().err
