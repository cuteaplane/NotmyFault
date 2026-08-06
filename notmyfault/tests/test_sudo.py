"""run_as_admin 提权命令生成与插件授权会话管理"""

import subprocess
import types

import pytest

from notmyfault.security import sudo

TOKEN = "test-engine-token"


@pytest.fixture(autouse=True)
def clean_sudo_state():
    yield
    sudo._engine_token = None
    sudo._admin_plugins.clear()
    sudo._admin_by_module.clear()


def make_plugin_module(name="notmyfault.action_testplug"):
    return types.ModuleType(name)


def call_from(module, func, *args, **kwargs):
    """在插件命名空间的模块全局里调用函数，模拟插件调用栈"""
    ns = module.__dict__
    ns["func"], ns["args"], ns["kwargs"] = func, args, kwargs
    exec("result = func(*args, **kwargs)", ns)
    return ns["result"]


@pytest.fixture
def authorized_plugin(monkeypatch):
    """返回 (module, captured)：插件已授权，subprocess.run 被替换为记录器"""
    sudo.begin_engine_session(TOKEN)
    module = make_plugin_module()
    sudo.authorize_plugin("testplug", TOKEN, module=module)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return captured.get(
            "result", subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        )

    monkeypatch.setattr(sudo.subprocess, "run", fake_run)
    return module, captured


class TestRunAsAdmin:
    def test_empty_command_raises(self):
        with pytest.raises(ValueError):
            sudo.run_as_admin([])

    def test_basic_command_generates_correct_ps(self, authorized_plugin):
        module, captured = authorized_plugin
        call_from(module, sudo.run_as_admin, ["cmd.exe", "/c", "dir"])
        assert captured["cmd"] == [
            "powershell",
            "-NoProfile",
            "-Command",
            "Start-Process -FilePath 'cmd.exe'"
            " -ArgumentList '/c', 'dir' -Verb RunAs -Wait",
        ]

    def test_ps_quote_simple(self, authorized_plugin):
        module, captured = authorized_plugin
        call_from(module, sudo.run_as_admin, ["notepad.exe", "plain"])
        assert "'plain'" in captured["cmd"][3]

    def test_ps_quote_with_apostrophe(self, authorized_plugin):
        module, captured = authorized_plugin
        call_from(module, sudo.run_as_admin, ["notepad.exe", "it's a test"])
        # PowerShell 单引号内的单引号必须双写转义
        assert "'it''s a test'" in captured["cmd"][3]

    def test_single_executable_no_args(self, authorized_plugin):
        module, captured = authorized_plugin
        call_from(module, sudo.run_as_admin, ["notepad.exe"])
        script = captured["cmd"][3]
        assert "-ArgumentList" not in script
        assert script == "Start-Process -FilePath 'notepad.exe' -Verb RunAs -Wait"

    def test_wait_false(self, authorized_plugin, monkeypatch):
        module, captured = authorized_plugin
        popened = []
        monkeypatch.setattr(
            sudo.subprocess, "Popen",
            lambda cmd, **kwargs: popened.append((cmd, kwargs)),
        )
        result = call_from(module, sudo.run_as_admin, ["cmd.exe"], wait=False)
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode == 0
        assert len(popened) == 1
        cmd, kwargs = popened[0]
        assert "-Wait" not in cmd[3]
        assert kwargs["stdin"] is subprocess.DEVNULL

    def test_returns_completed_process(self, authorized_plugin):
        module, captured = authorized_plugin
        expected = subprocess.CompletedProcess(["x"], 0, stdout="out", stderr="")
        captured["result"] = expected
        result = call_from(module, sudo.run_as_admin, ["cmd.exe"])
        assert result is expected

    def test_zero_return_code_silent(self, authorized_plugin, capsys):
        module, captured = authorized_plugin
        captured["result"] = subprocess.CompletedProcess(
            ["x"], 0, stdout="", stderr="无关输出"
        )
        call_from(module, sudo.run_as_admin, ["cmd.exe"])
        assert capsys.readouterr().err == ""

    def test_non_zero_return_code(self, authorized_plugin, capsys):
        module, captured = authorized_plugin
        captured["result"] = subprocess.CompletedProcess(
            ["x"], 1, stdout="", stderr="boom"
        )
        result = call_from(module, sudo.run_as_admin, ["cmd.exe"])
        assert result.returncode == 1
        err = capsys.readouterr().err
        assert "命令执行可能失败" in err
        assert "boom" in err

    def test_timeout_expired_raises(self, authorized_plugin, monkeypatch, capsys):
        module, captured = authorized_plugin

        def timeout_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))

        monkeypatch.setattr(sudo.subprocess, "run", timeout_run)
        with pytest.raises(subprocess.TimeoutExpired):
            call_from(module, sudo.run_as_admin, ["cmd.exe"], timeout=5)
        assert "命令超时" in capsys.readouterr().err

    def test_os_error_propagates(self, authorized_plugin, monkeypatch, capsys):
        module, captured = authorized_plugin

        def broken_run(cmd, **kwargs):
            raise OSError("找不到可执行文件")

        monkeypatch.setattr(sudo.subprocess, "run", broken_run)
        with pytest.raises(OSError):
            call_from(module, sudo.run_as_admin, ["cmd.exe"])
        assert "命令执行异常" in capsys.readouterr().err


def test_authorize_binds_to_module_identity(monkeypatch):
    sudo.begin_engine_session(TOKEN)
    authorized = make_plugin_module()
    sudo.authorize_plugin("testplug", TOKEN, module=authorized)
    monkeypatch.setattr(
        sudo.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0),
    )

    # 同名插件命名空间的另一个模块没有授权记录，不能冒用
    impostor = make_plugin_module()
    with pytest.raises(PermissionError) as excinfo:
        call_from(impostor, sudo.run_as_admin, ["cmd.exe"])
    assert "未授权使用" in str(excinfo.value)


def test_deauthorize_plugin_revokes_both_registries(monkeypatch):
    sudo.begin_engine_session(TOKEN)
    module = make_plugin_module()
    sudo.authorize_plugin("testplug", TOKEN, module=module)
    assert sudo.is_authorized("testplug")

    assert sudo.deauthorize_plugin("testplug", TOKEN) is True
    assert sudo.is_authorized("testplug") is False
    assert sudo.get_authorized_plugins() == []
    assert sudo._admin_by_module == {}

    monkeypatch.setattr(
        sudo.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0),
    )
    with pytest.raises(PermissionError):
        call_from(module, sudo.run_as_admin, ["cmd.exe"])


def test_deauthorize_wrong_token_rejected():
    sudo.begin_engine_session(TOKEN)
    module = make_plugin_module()
    sudo.authorize_plugin("testplug", TOKEN, module=module)
    assert sudo.deauthorize_plugin("testplug", "wrong-token") is False
    assert sudo.is_authorized("testplug") is True


def test_engine_session_rotation_revokes_old_token_and_authorizations():
    old_token = sudo.begin_engine_session()
    sudo.authorize_plugin("testplug", old_token, module=make_plugin_module())
    assert sudo.is_authorized("testplug")

    new_token = sudo.begin_engine_session()
    assert new_token != old_token
    # 旧令牌在新会话中既不能授权也不能结束会话
    with pytest.raises(PermissionError):
        sudo.authorize_plugin("testplug", old_token)
    assert sudo.is_authorized("testplug") is False
    assert sudo.end_engine_session(old_token) is False
    assert sudo.end_engine_session(new_token) is True
    assert sudo._engine_token is None


def test_override_does_not_inherit_admin(monkeypatch):
    sudo.begin_engine_session(TOKEN)
    original = make_plugin_module("notmyfault.action_plug")
    sudo.authorize_plugin("plug", TOKEN, module=original)

    override = make_plugin_module("notmyfault.action_plug")
    monkeypatch.setattr(
        sudo.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0),
    )
    # 覆盖插件是新模块对象，模块身份不同，不继承原插件的管理员授权
    with pytest.raises(PermissionError):
        call_from(override, sudo.run_as_admin, ["cmd.exe"])
