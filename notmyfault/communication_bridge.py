

import threading
from notmyfault.communication_bus import (
    get_communication_bus,
    initialize_communication_bus,
    shutdown_communication_bus,
    Message,
    Channels,
    MessageType
)


class ScriptComHandler:
    """脚本端的通信处理器"""
    
    def __init__(self):
        self.bus = get_communication_bus()
    
    def setup_handlers(self):
        """设置所有请求处理器"""
        self.bus.subscribe(Channels.ENGINE_START, self._handle_engine_start)
        self.bus.subscribe(Channels.ENGINE_STOP, self._handle_engine_stop)
        self.bus.subscribe(Channels.ENGINE_STATUS, self._handle_engine_status)
        self.bus.subscribe(Channels.RULE_CREATE, self._handle_rule_create)
        self.bus.subscribe(Channels.RULE_UPDATE, self._handle_rule_update)
        self.bus.subscribe(Channels.RULE_DELETE, self._handle_rule_delete)
        self.bus.subscribe(Channels.PLUGIN_DISCOVERY, self._handle_plugin_discovery)
    
    def _handle_engine_start(self, message: Message):
        """处理启动引擎请求"""
        print("[脚本] 收到启动引擎请求")
        # 这里应该调用实际的engine.start()
        # self.engine.start()
        
        # 广播引擎状态变化
        self.bus.publish(
            Channels.ENGINE_STATE_CHANGED,
            {"state": "running"},
            msg_type=MessageType.STATUS.value
        )
    
    def _handle_engine_stop(self, message: Message):
        """处理停止引擎请求"""
        print("[脚本] 收到停止引擎请求")
        # self.engine.stop()
        
        self.bus.publish(
            Channels.ENGINE_STATE_CHANGED,
            {"state": "stopped"},
            msg_type=MessageType.STATUS.value
        )
    
    def _handle_engine_status(self, message: Message):
        """处理查询引擎状态请求"""
        print("[脚本] 收到查询引擎状态请求")
        # status = self.engine.get_status()
        
        self.bus.respond(
            message,
            {"status": "running", "rules_count": 5}
        )
    
    def _handle_rule_create(self, message: Message):
        """处理创建规则请求"""
        rule = message.data.get("rule", {})
        print(f"[脚本] 收到创建规则请求: {rule}")
        # 在这里实现规则创建逻辑
    
    def _handle_rule_update(self, message: Message):
        """处理更新规则请求"""
        rule_id = message.data.get("rule_id")
        rule = message.data.get("rule", {})
        print(f"[脚本] 收到更新规则请求: {rule_id}")
    
    def _handle_rule_delete(self, message: Message):
        """处理删除规则请求"""
        rule_id = message.data.get("rule_id")
        print(f"[脚本] 收到删除规则请求: {rule_id}")
    
    def _handle_plugin_discovery(self, message: Message):
        """处理插件发现请求"""
        print("[脚本] 收到插件发现请求")
        
        # 这里应该扫描实际的插件目录
        plugins = {
            "triggers": {
                "process_state": {"name": "Process State", "description": "监控进程状态变化"},
                "usb_insert": {"name": "USB Insert", "description": "检测USB设备插入"}
            },
            "actions": {
                "set_volume": {"name": "Set Volume", "description": "设置系统音量"},
                "notify": {"name": "Show Notification", "description": "显示通知"}
            }
        }
        
        self.bus.respond(message, plugins)
    
    def notify_rule_triggered(self, rule_name: str):
        """通知规则被触发"""
        self.bus.publish(
            Channels.RULE_TRIGGERED,
            {"rule_name": rule_name},
            msg_type=MessageType.EVENT.value
        )
    
    def notify_action_executed(self, action_name: str, result: str):
        """通知动作执行"""
        self.bus.publish(
            Channels.ACTION_EXECUTED,
            {"action_name": action_name, "result": result},
            msg_type=MessageType.EVENT.value
        )
    
    def notify_error(self, error_msg: str):
        """通知发生错误"""
        self.bus.publish(
            Channels.ERROR_OCCURRED,
            {"error": error_msg},
            msg_type=MessageType.ERROR.value
        )


# 使用示例
def setup_communication():
    """在main()前调用此函数初始化通信"""
    initialize_communication_bus()


def cleanup_communication():
    """在程序退出时调用此函数清理通信"""
    shutdown_communication_bus()


# 快速测试脚本
if __name__ == "__main__":
    import time

    setup_communication()

    handler = ScriptComHandler()
    handler.setup_handlers()

    time.sleep(0.5)

    print("\n=== 测试: 触发规则通知 ===")
    handler.notify_rule_triggered("test_rule")
    time.sleep(0.5)

    print("\n=== 测试: 动作执行通知 ===")
    handler.notify_action_executed("set_volume", "ok")
    time.sleep(0.5)

    cleanup_communication()
    print("\n通信总线已关闭")
