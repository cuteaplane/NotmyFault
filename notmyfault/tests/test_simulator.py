"""模拟器端到端测试：SimulatedEnvironment 提供受控环境，SimulatedRunner 注入打桩引擎。"""

from notmyfault.simulator import SimulatedEnvironment, SimulatedRunner


def make_config(rules):
    return {"disabled_plugins": {"triggers": [], "actions": []}, "rules": rules}


def simple_rule(name, event, actions):
    return {"name": name, "event": event, "actions": actions}


def notify(title, message):
    return {"type": "notify", "params": {"title": title, "message": message}}


def test_basic():
    env = SimulatedEnvironment()
    with SimulatedRunner(env) as runner:
        runner.start()
        env.processes.add("WeChat.exe")
        runner.emit("process_state", {"process_name": "WeChat.exe", "state": "running"})
        actions = [
            (e["data"]["action_type"], e["data"]["params"])
            for e in runner.events if e["type"] == "action_executed"
        ]
        assert actions == [
            ("set_volume", {"action": "max"}),
            ("notify", {"title": "微信正在运行", "message": "音量已设置为100%"}),
        ]


def test_wechat_close():
    env = SimulatedEnvironment()
    with SimulatedRunner(env) as runner:
        runner.start()
        env.processes.add("WeChat.exe")
        runner.emit("process_state", {"process_name": "WeChat.exe", "state": "running"})
        runner.clear_events()
        env.processes.remove("WeChat.exe")
        runner.emit("process_state", {"process_name": "WeChat.exe", "state": "stopped"})
        assert runner.assert_action(
            "notify", {"title": "微信已退出", "message": "音量已恢复至50%"}
        )


def test_usb_insert():
    env = SimulatedEnvironment()
    config = make_config([
        simple_rule(
            "U盘备份",
            {"type": "usb_insert", "params": {"drive_letter": "E:"}},
            [notify("U盘已插入", "E:")],
        )
    ])
    with SimulatedRunner(env) as runner:
        runner.start(config)
        env.usb.insert("E:", "备份盘")
        runner.emit("usb_insert", {"drive_letter": "E:"})
        assert runner.assert_action("notify", {"title": "U盘已插入", "message": "E:"})


def test_window_title():
    env = SimulatedEnvironment()
    config = make_config([
        simple_rule(
            "记事本检测",
            {"type": "window_title", "params": {"title": "记事本"}},
            [notify("窗口出现", "记事本")],
        )
    ])
    with SimulatedRunner(env) as runner:
        runner.start(config)
        env.windows.open("记事本")
        runner.emit("window_title", {"title": "记事本"})
        assert runner.assert_action("notify", {"title": "窗口出现", "message": "记事本"})


def test_or_condition():
    config = make_config([
        {
            "name": "或条件",
            "condition": {
                "op": "any",
                "children": [
                    {"type": "process_state",
                     "params": {"process_name": "WeChat.exe", "state": "running"}},
                    {"type": "window_title", "params": {"title": "记事本"}},
                ],
            },
            "actions": [notify("命中", "或条件触发")],
        }
    ])
    with SimulatedRunner(SimulatedEnvironment()) as runner:
        runner.start(config)
        runner.emit("window_title", {"title": "记事本"})
        assert runner.assert_action("notify", {"title": "命中", "message": "或条件触发"})
        runner.clear_events()
        runner.emit("process_state", {"process_name": "WeChat.exe", "state": "running"})
        assert runner.assert_action("notify", {"title": "命中", "message": "或条件触发"})


def test_process_name_without_exe():
    env = SimulatedEnvironment()
    with SimulatedRunner(env) as runner:
        runner.start()
        # 规则期望完整进程名 WeChat.exe，不带扩展名的名字不算命中
        env.processes.add("WeChat")
        runner.emit("process_state", {"process_name": "WeChat", "state": "running"})
        assert runner.last_action() is None


def test_rapid_restart():
    env = SimulatedEnvironment()
    with SimulatedRunner(env) as runner:
        runner.start()
        runner.emit("process_state", {"process_name": "WeChat.exe", "state": "stopped"})
        assert runner.assert_action(
            "notify", {"title": "微信已退出", "message": "音量已恢复至50%"}
        )
        runner.emit("process_state", {"process_name": "WeChat.exe", "state": "running"})
        assert runner.assert_action(
            "notify", {"title": "微信正在运行", "message": "音量已设置为100%"}
        )
        triggered = [e for e in runner.events if e["type"] == "rule_triggered"]
        assert len(triggered) == 2
