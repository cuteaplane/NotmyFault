"""
通信总线集成示例
展示如何在UI和脚本中使用通信总线进行双向通信

使用方法：
1. 在main函数前调用 setup_communication() 初始化总线
2. UI事件通过publish或request与脚本通信
3. 脚本通过respond或publish回复UI
"""

import threading
from notmyfault.communication_bus import (
    get_communication_bus, 
    initialize_communication_bus,
    shutdown_communication_bus,
    Message,
    Channels,
    MessageType
)


class UIComBridge:
    """UI与脚本的通信桥接类"""
    
    def __init__(self, page=None):
        """
        初始化通信桥接
        
        Args:
            page: Flet Page对象，用于UI更新回调
        """
        self.bus = get_communication_bus()
        self.page = page
        self.engine_running = False
    
    def setup_listeners(self):
        """设置所有监听器"""
        # 监听来自脚本的状态变化
        self.bus.subscribe(Channels.ENGINE_STATE_CHANGED, self._on_engine_state_changed)
        self.bus.subscribe(Channels.RULE_TRIGGERED, self._on_rule_triggered)
        self.bus.subscribe(Channels.ACTION_EXECUTED, self._on_action_executed)
        self.bus.subscribe(Channels.ERROR_OCCURRED, self._on_error_occurred)
    
    # ============ UI -> Script 的操作 ============
    
    def request_engine_status(self) -> dict:
        """请求引擎状态"""
        response = self.bus.request(
            Channels.ENGINE_STATUS,
            {"command": "get_status"},
            timeout=2.0
        )
        if response:
            return response.data
        return {"status": "unknown"}
    
    def start_engine(self):
        """启动引擎"""
        self.bus.publish(
            Channels.ENGINE_START,
            {"timestamp": None},
            msg_type=MessageType.REQUEST.value
        )
    
    def stop_engine(self):
        """停止引擎"""
        self.bus.publish(
            Channels.ENGINE_STOP,
            {"timestamp": None},
            msg_type=MessageType.REQUEST.value
        )
    
    def create_rule(self, rule_data: dict):
        """创建规则"""
        self.bus.publish(
            Channels.RULE_CREATE,
            {"rule": rule_data},
            msg_type=MessageType.REQUEST.value
        )
    
    def update_rule(self, rule_id: str, rule_data: dict):
        """更新规则"""
        self.bus.publish(
            Channels.RULE_UPDATE,
            {"rule_id": rule_id, "rule": rule_data},
            msg_type=MessageType.REQUEST.value
        )
    
    def delete_rule(self, rule_id: str):
        """删除规则"""
        self.bus.publish(
            Channels.RULE_DELETE,
            {"rule_id": rule_id},
            msg_type=MessageType.REQUEST.value
        )
    
    def request_plugin_discovery(self) -> dict:
        """请求插件发现"""
        response = self.bus.request(
            Channels.PLUGIN_DISCOVERY,
            {"command": "discover"},
            timeout=3.0
        )
        if response:
            return response.data
        return {"triggers": {}, "actions": {}}
    
    # ============ 来自 Script 的事件处理 ============
    
    def _on_engine_state_changed(self, message: Message):
        """处理引擎状态变化"""
        state = message.data.get("state", "unknown")
        print(f"[通信总线] 引擎状态变化: {state}")
        
        self.engine_running = state == "running"
        
        if self.page:
            # 在UI线程中执行更新
            def update_ui():
                # 这里可以更新UI中的状态显示
                # 例如更新dashboard中的状态标签
                pass
            
            # 使用thread安全的方式更新UI
            threading.Timer(0, update_ui).start()
    
    def _on_rule_triggered(self, message: Message):
        """处理规则被触发"""
        rule_name = message.data.get("rule_name", "unknown")
        print(f"[通信总线] 规则被触发: {rule_name}")
        
        if self.page:
            # 显示通知
            def show_notification():
                try:
                    import flet as ft
                    snack_bar = ft.SnackBar(ft.Text(f"规则 '{rule_name}' 已触发"))
                    self.page.snack_bar = snack_bar
                    snack_bar.open = True
                    self.page.update()
                except:
                    pass
            
            threading.Timer(0, show_notification).start()
    
    def _on_action_executed(self, message: Message):
        """处理动作执行"""
        action_name = message.data.get("action_name", "unknown")
        result = message.data.get("result", "unknown")
        print(f"[通信总线] 动作执行完成: {action_name} -> {result}")
    
    def _on_error_occurred(self, message: Message):
        """处理错误"""
        error = message.data.get("error", "Unknown error")
        print(f"[通信总线] 错误: {error}")
        
        if self.page:
            def show_error():
                try:
                    import flet as ft
                    snack_bar = ft.SnackBar(ft.Text(error), bgcolor=ft.Colors.ERROR)
                    self.page.snack_bar = snack_bar
                    snack_bar.open = True
                    self.page.update()
                except:
                    pass
            
            threading.Timer(0, show_error).start()


# 脚本端的处理器示例
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
    
    # 初始化
    setup_communication()
    
    # 创建脚本端处理器
    script_handler = ScriptComHandler()
    script_handler.setup_handlers()
    
    # 创建UI端桥接（模拟）
    ui_bridge = UIComBridge()
    ui_bridge.setup_listeners()
    
    # 等待总线启动
    time.sleep(0.5)
    
    # 测试：模拟UI请求引擎状态
    print("\n=== 测试1: 请求引擎状态 ===")
    status = ui_bridge.request_engine_status()
    print(f"收到状态: {status}")
    
    # 测试：启动引擎
    print("\n=== 测试2: 启动引擎 ===")
    ui_bridge.start_engine()
    time.sleep(1)
    
    # 测试：规则触发
    print("\n=== 测试3: 规则触发通知 ===")
    script_handler.notify_rule_triggered("test_rule")
    time.sleep(0.5)
    
    # 清理
    cleanup_communication()
    print("\n通信总线已关闭")
