"""
通信总线快速参考指南
保存此文件以便随时查阅
"""

# ============================================================
# 🎯 快速开始 - 60 秒内了解通信总线
# ============================================================

# 1️⃣ 导入必要的模块
from notmyfault.communication_bridge import (
    UIComBridge, 
    ScriptComHandler,
    setup_communication,
    cleanup_communication
)
from notmyfault.communication_bus import Channels, get_communication_bus

# 2️⃣ UI 端（Flet）
def setup_ui(page):
    setup_communication()
    bridge = UIComBridge(page)
    bridge.setup_listeners()
    page.on_close = lambda e: cleanup_communication()
    
    # 使用通信
    bridge.start_engine()                    # 启动引擎
    status = bridge.request_engine_status()  # 查询状态
    bridge.create_rule({...})               # 创建规则


# 3️⃣ 脚本端（Backend）
def setup_backend():
    setup_communication()
    handler = ScriptComHandler()
    handler.setup_handlers()
    
    # 通知事件
    handler.notify_rule_triggered("rule_name")
    handler.notify_action_executed("action", "success")
    handler.notify_error("error message")


# ============================================================
# 📚 常用操作速查表
# ============================================================

# 【发送消息】发布事件（异步）
bus = get_communication_bus()
bus.publish("channel_name", {"key": "value"}, msg_type="event")

# 【发送消息】发送请求并等待响应（同步）
response = bus.request("channel_name", {"query": "data"}, timeout=2.0)
if response:
    print(response.data)

# 【接收消息】订阅频道
def on_message(message):
    print(f"收到消息: {message.data}")

bus.subscribe("channel_name", on_message)

# 【接收消息】响应请求
def handle_request(message):
    bus.respond(message, {"result": "success"})

bus.subscribe("request_channel", handle_request)


# ============================================================
# 🔌 预定义频道速查
# ============================================================

# UI -> 脚本
Channels.RULE_CREATE           # 创建规则
Channels.RULE_UPDATE           # 更新规则
Channels.RULE_DELETE           # 删除规则
Channels.ENGINE_START          # 启动引擎
Channels.ENGINE_STOP           # 停止引擎
Channels.ENGINE_STATUS         # 查询状态

# 脚本 -> UI
Channels.ENGINE_STATE_CHANGED  # 引擎状态变化
Channels.RULE_TRIGGERED        # 规则触发
Channels.ACTION_EXECUTED       # 动作执行
Channels.ERROR_OCCURRED        # 错误发生


# ============================================================
# 💡 实用代码片段
# ============================================================

# 【片段1】完整的规则创建流程
rule_data = {
    "name": "我的规则",
    "event": {
        "type": "usb_insert",
        "params": {"device_id": "123"}
    },
    "actions": [
        {"type": "set_volume", "params": {"volume": 50}}
    ]
}
com_bridge.create_rule(rule_data)

# 【片段2】监听多个事件
def setup_listeners(com_bridge):
    bus = get_communication_bus()
    
    def on_rule_triggered(msg):
        print(f"规则 {msg.data['rule_name']} 被触发")
    
    def on_engine_changed(msg):
        print(f"引擎状态: {msg.data['state']}")
    
    def on_error(msg):
        print(f"错误: {msg.data['error']}")
    
    bus.subscribe(Channels.RULE_TRIGGERED, on_rule_triggered)
    bus.subscribe(Channels.ENGINE_STATE_CHANGED, on_engine_changed)
    bus.subscribe(Channels.ERROR_OCCURRED, on_error)

# 【片段3】异步操作的正确做法
import threading

def long_operation():
    # 这是一个长时间运行的操作
    time.sleep(5)
    handler.notify_action_executed("my_action", "done")

# 在线程中执行，不会阻塞 UI
threading.Thread(target=long_operation, daemon=True).start()

# 【片段4】错误处理
try:
    response = bus.request("channel", {}, timeout=1.0)
    if response is None:
        print("请求超时！")
    else:
        print(response.data)
except Exception as e:
    print(f"发生错误: {e}")


# ============================================================
# ⚡ 常见问题速答
# ============================================================

# Q: 消息会丢失吗？
# A: 队列满时会丢弃新消息。增加队列大小：
#    bus = CommunicationBus(max_queue_size=5000)

# Q: 请求超时如何处理？
# A: request() 返回 None
#    response = bus.request(..., timeout=2.0)
#    if response is None: print("超时")

# Q: 回调函数很慢会怎样？
# A: 不会阻塞 UI，会在后台线程中按顺序处理
#    如果需要快速响应，异步处理或启用多线程

# Q: 如何自定义频道？
# A: 直接使用字符串即可
#    bus.publish("my.custom.channel", {...})

# Q: 线程安全吗？
# A: 完全线程安全。可以从任何线程调用所有方法

# Q: 需要手动关闭吗？
# A: 是的，在程序退出时：
#    cleanup_communication()


# ============================================================
# 🔗 文件导航
# ============================================================

"""
核心模块：
  notmyfault/
    communication_bus.py       <- 总线实现
    communication_bridge.py    <- UI/脚本桥接
    COMMUNICATION_BUS_README.md <- 完整文档

测试和示例：
  test_communication.py        <- 集成测试
  test_winui3.py              <- UI 主程序（已集成）

文档：
  IMPROVEMENTS_SUMMARY.md      <- 改进总结
  COMMUNICATION_BUS_README.md  <- 详细文档
"""

# ============================================================
# 📊 架构概览
# ============================================================

"""
┌─────────────────────────────────────────┐
│         Flet UI (test_winui3.py)        │
│  - UIComBridge (通信桥接)                │
│  - 规则管理                              │
│  - 实时状态显示                          │
└────────────────┬────────────────────────┘
                 │
                 │ 消息队列
                 │
        ┌────────▼────────┐
        │ Communication   │
        │ Bus (总线)       │
        │ - 发布-订阅      │
        │ - 请求-响应      │
        └────────┬────────┘
                 │
    ┌────────────┴───────────┐
    │ 消息处理线程            │
    │ (线程安全)              │
    └────────────┬───────────┘
                 │
        ┌────────▼────────┐
        │ Backend Script  │
        │ (engine.py)     │
        │ - ScriptComHandler
        │ - 规则引擎       │
        │ - 动作执行       │
        └─────────────────┘
"""


# ============================================================
# 🚀 最小化完整示例
# ============================================================

if __name__ == "__main__":
    from notmyfault.communication_bridge import (
        UIComBridge, ScriptComHandler, 
        setup_communication, cleanup_communication
    )
    import time
    
    # 初始化
    setup_communication()
    
    # 设置脚本端处理
    script = ScriptComHandler()
    script.setup_handlers()
    
    # 设置 UI 端（模拟）
    ui = UIComBridge()
    ui.setup_listeners()
    
    time.sleep(0.5)
    
    # 测试：创建规则
    ui.create_rule({
        "name": "test",
        "event": {"type": "usb_insert", "params": {}},
        "actions": []
    })
    
    # 测试：查询状态
    status = ui.request_engine_status()
    print(f"状态: {status}")
    
    # 清理
    cleanup_communication()
    print("完成")
