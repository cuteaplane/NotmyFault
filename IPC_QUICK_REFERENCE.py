"""
NotmyFault IPC 快速参考卡 - 60 秒掌握核心用法
"""

# ============================================================
# 🚀 最小化启动示例 (复制即用)
# ============================================================

# ===== 后台引擎启动 =====
if __name__ == "__main__":
    from notmyfault.communication_bus import initialize_communication_bus
    from notmyfault.ipc_server import start_ipc_server
    
    # 初始化
    initialize_communication_bus()
    
    # 启动 IPC 服务器
    server = start_ipc_server()
    print(f"✨ IPC 服务器已启动: {server.address}")
    
    # 启动你的引擎
    run_engine()


# ===== UI 端集成 =====
import flet as ft
from notmyfault.ipc_client import UIRemoteController
from notmyfault.communication_bus import Channels

def main(page: ft.Page):
    # 创建远程控制器
    remote = UIRemoteController()
    
    # 定义状态更新回调
    def on_engine_state_changed(msg):
        state = msg.data['state']
        print(f"引擎状态: {state}")
        # 更新 UI...
    
    # 注册回调
    remote.on(Channels.ENGINE_STATE_CHANGED, on_engine_state_changed)
    
    # 连接到后台
    remote.connect()
    
    # 页面关闭时断开
    page.on_close = lambda e: remote.disconnect()
    
    # UI 内容
    page.add(ft.Text("Connected!" if remote.is_connected() else "Not connected"))

ft.run(main)


# ============================================================
# 📡 常用操作 - 复制粘贴版
# ============================================================

# --- 后台端 (backend) ---

# 1️⃣ 广播状态变化给所有 UI
from notmyfault.communication_bus import get_communication_bus, Message, MessageType, Channels

bus = get_communication_bus()
bus.publish(
    Channels.ENGINE_STATE_CHANGED,
    {"state": "running"},
    msg_type=MessageType.STATUS.value
)

# 2️⃣ 处理 UI 的创建规则请求
def handle_rule_create(message: Message):
    rule = message.data.get("rule")
    print(f"创建规则: {rule['name']}")
    # 执行创建逻辑...

bus.subscribe(Channels.RULE_CREATE, handle_rule_create)

# 3️⃣ 通知 UI 规则被触发
bus.publish(
    Channels.RULE_TRIGGERED,
    {"rule_name": "my_rule"},
    msg_type=MessageType.EVENT.value
)

# 4️⃣ 发送错误信息
bus.publish(
    Channels.ERROR_OCCURRED,
    {"error": "Something went wrong"},
    msg_type=MessageType.ERROR.value
)


# --- UI 端 (frontend) ---

# 1️⃣ 启动引擎
remote.start_engine()

# 2️⃣ 停止引擎
remote.stop_engine()

# 3️⃣ 创建规则
remote.create_rule({
    "name": "test_rule",
    "event": {"type": "usb_insert", "params": {}},
    "actions": []
})

# 4️⃣ 更新规则
remote.update_rule("rule_id", rule_data)

# 5️⃣ 删除规则
remote.delete_rule("rule_id")

# 6️⃣ 查询引擎状态
remote.request_engine_status()

# 7️⃣ 发现可用插件
remote.request_plugin_discovery()

# 8️⃣ 注册事件监听
def on_rule_triggered(msg):
    rule_name = msg.data['rule_name']
    print(f"规则触发: {rule_name}")

remote.on(Channels.RULE_TRIGGERED, on_rule_triggered)

# 9️⃣ 取消监听
remote.off(Channels.RULE_TRIGGERED)

# 🔟 检查连接状态
if remote.is_connected():
    print("已连接")


# ============================================================
# 🔌 预定义频道速查表
# ============================================================

from notmyfault.communication_bus import Channels

# UI -> 后台 (请求)
Channels.ENGINE_START          # 启动引擎
Channels.ENGINE_STOP           # 停止引擎
Channels.ENGINE_STATUS         # 查询状态
Channels.RULE_CREATE           # 创建规则
Channels.RULE_UPDATE           # 更新规则
Channels.RULE_DELETE           # 删除规则
Channels.RULE_ENABLE           # 启用规则
Channels.RULE_DISABLE          # 禁用规则
Channels.PLUGIN_DISCOVERY      # 发现插件

# 后台 -> UI (事件)
Channels.ENGINE_STATE_CHANGED  # 引擎状态变化
Channels.RULE_TRIGGERED        # 规则被触发
Channels.ACTION_EXECUTED       # 动作执行完毕
Channels.ERROR_OCCURRED        # 错误发生


# ============================================================
# 💾 配置同步用法
# ============================================================

from notmyfault.config_sync import ConfigSyncManager, ConfigSyncBridge

# 初始化配置同步
config_mgr = ConfigSyncManager('/path/to/config.json')

# 启用文件监视
config_mgr.watch_file_changes(poll_interval=2.0)

# 注册配置变化回调
def on_config_updated(source, config):
    if source == "ui":
        print("UI 更新了配置")
    elif source == "backend":
        print("后台更新了配置")
    elif source == "file":
        print("文件被外部修改")

config_mgr.on_change(on_config_updated)

# 从 UI 更新配置
config_mgr.update_from_ui(new_config, ui_id='ui-001')

# 从后台更新配置
config_mgr.update_from_backend(new_config)

# 获取当前配置
config = config_mgr.get_config()

# 创建桥接启用 IPC 同步
bridge = ConfigSyncBridge(config_mgr)
bridge.setup()


# ============================================================
# 🎯 完整示例 - 复制即可运行
# ============================================================

# --- backend_example.py (后台) ---
"""
from notmyfault.communication_bus import (
    initialize_communication_bus, get_communication_bus,
    Message, Channels
)
from notmyfault.ipc_server import start_ipc_server
import time
import threading

def main():
    # 初始化
    initialize_communication_bus()
    server = start_ipc_server()
    bus = get_communication_bus()
    
    print("✨ 后台引擎已启动")
    
    # 设置请求处理器
    def handle_engine_status_request(msg):
        bus.respond(msg, {
            "status": "running",
            "uptime": 123.45,
            "client_count": server.get_client_count()
        })
    
    bus.subscribe(Channels.ENGINE_STATUS, handle_engine_status_request)
    
    # 定期广播状态
    def broadcast_status():
        while True:
            time.sleep(30)
            bus.publish(
                Channels.ENGINE_STATE_CHANGED,
                {"state": "running"},
                msg_type="status"
            )
    
    threading.Thread(target=broadcast_status, daemon=True).start()
    
    # 保持运行
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        print("后台已关闭")

if __name__ == "__main__":
    main()
"""

# --- ui_example.py (UI) ---
"""
import flet as ft
from notmyfault.ipc_client import UIRemoteController
from notmyfault.communication_bus import Channels

def main(page: ft.Page):
    page.title = "NotmyFault UI"
    
    remote = UIRemoteController()
    status_text = ft.Text("连接中...")
    
    def on_engine_state(msg):
        status_text.value = f"状态: {msg.data['state']}"
        page.update()
    
    def connect_click(e):
        if remote.connect():
            remote.on(Channels.ENGINE_STATE_CHANGED, on_engine_state)
            remote.request_engine_status()
            status_text.value = "已连接"
        else:
            status_text.value = "连接失败"
        page.update()
    
    def start_click(e):
        remote.start_engine()
        status_text.value = "已发送启动命令"
        page.update()
    
    def stop_click(e):
        remote.stop_engine()
        status_text.value = "已发送停止命令"
        page.update()
    
    page.add(
        status_text,
        ft.Row([
            ft.FilledButton("连接", on_click=connect_click),
            ft.FilledButton("启动", on_click=start_click),
            ft.FilledButton("停止", on_click=stop_click),
        ])
    )

ft.run(main)
"""


# ============================================================
# 🔧 命令行速查
# ============================================================

"""
# 启动后台引擎（含 IPC）
python NOTMYFAULT_Enhanced.pyw

# 启动 UI（自动连接）
python ControlCenter_Enhanced.pyw
python test_winui3.py

# 启动后台（禁用 IPC）
python NOTMYFAULT_Enhanced.pyw --no-ipc

# 启动后台（禁用配置同步）
python NOTMYFAULT_Enhanced.pyw --no-config-sync

# 测试 IPC 连接
python -c "from notmyfault.ipc_client import UIRemoteController; r = UIRemoteController(); print('Connected!' if r.connect() else 'Failed')"
"""


# ============================================================
# ⚡ 性能优化技巧
# ============================================================

# 1️⃣ 批量操作
# 而不是:
for rule in rules:
    remote.create_rule(rule)
# 应该:
for rule in rules:
    config['rules'].append(rule)
# 一次性更新
remote.send_request('config.update', {'config': config})

# 2️⃣ 事件去抖
import time
from collections import deque

class Debouncer:
    def __init__(self, delay=1.0):
        self.delay = delay
        self.last_call = 0
    
    def should_call(self):
        now = time.time()
        if now - self.last_call >= self.delay:
            self.last_call = now
            return True
        return False

debouncer = Debouncer(delay=1.0)
def on_frequent_event(msg):
    if debouncer.should_call():
        # 处理事件
        pass

# 3️⃣ 消息过滤
def on_error_occurred(msg):
    error = msg.data['error']
    if 'timeout' in error.lower():
        print("超时错误，自动重试")

# 4️⃣ 心跳检测
def heartbeat_monitor():
    import time
    import threading
    
    def monitor():
        while remote.is_connected():
            time.sleep(30)
            remote.request_engine_status()
    
    threading.Thread(target=monitor, daemon=True).start()


# ============================================================
# 🐛 快速调试
# ============================================================

# 打印消息详情
def debug_message(msg):
    print(f"类型: {msg.type}")
    print(f"频道: {msg.channel}")
    print(f"数据: {msg.data}")
    print(f"ID: {msg.request_id}")
    print(f"时间戳: {msg.timestamp}")

bus.subscribe("*", debug_message)  # 监听所有消息

# 连接调试
remote = UIRemoteController()
print(f"[DEBUG] 尝试连接到 {remote.address}")
if remote.connect(timeout=10.0):
    print("[DEBUG] 连接成功")
    print(f"[DEBUG] 连接状态: {remote.is_connected()}")
else:
    print("[DEBUG] 连接失败")

# 消息队列监控
bus = get_communication_bus()
print(f"队列大小: {bus.message_queue.qsize()}")
print(f"挂起请求: {len(bus.pending_requests)}")


# ============================================================
# 📚 完整 API 速查
# ============================================================

# UIRemoteController 方法
remote.connect(timeout=5.0)                           # 连接
remote.disconnect()                                    # 断开
remote.is_connected()                                  # 检查连接
remote.send_request(channel, data)                     # 发送请求
remote.send_event(channel, data)                       # 发送事件
remote.on(channel, callback)                           # 注册回调
remote.off(channel, callback)                          # 取消回调
# 便捷方法:
remote.start_engine()
remote.stop_engine()
remote.request_engine_status()
remote.create_rule(rule_data)
remote.update_rule(rule_id, rule_data)
remote.delete_rule(rule_id)
remote.enable_rule(rule_id)
remote.disable_rule(rule_id)
remote.request_plugin_discovery()

# EngineIPCServer 方法
server.start()                                        # 启动
server.stop()                                         # 停止
server.get_client_count()                             # 获取客户端数
server.send_to_ui(message)                            # 向所有 UI 发送

# ConfigSyncManager 方法
config_mgr.get_config()                               # 获取配置
config_mgr.update_from_ui(config, ui_id)             # UI 更新
config_mgr.update_from_backend(config)                # 后台更新
config_mgr.watch_file_changes(interval)               # 启动文件监视
config_mgr.on_change(callback)                        # 注册变化回调
