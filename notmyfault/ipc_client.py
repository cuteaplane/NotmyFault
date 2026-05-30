"""
IPC 客户端 - 运行在 UI (Flet) 进程中
用于与后台引擎进行跨进程通信

特性：
- 连接到后台引擎服务器
- 发送控制指令
- 实时接收引擎事件和状态更新
- 自动重连机制
"""

import threading
import time
import uuid
from multiprocessing.connection import Client
from typing import Dict, Callable, Optional, List
from notmyfault.communication_bus import (
    Message,
    MessageType,
    Channels
)


class UIRemoteController:
    """
    UI 的远程控制器 - 连接到后台引擎的客户端
    
    负责：
    1. 连接到后台引擎的 IPC 服务器
    2. 向引擎发送控制指令
    3. 接收并处理引擎的事件
    """
    
    def __init__(self, host: str = 'localhost', port: int = 19198, authkey: bytes = b'notmyfault_ipc_key'):
        """
        初始化 UI 远程控制器
        
        Args:
            host: 服务器地址
            port: 服务器端口
            authkey: 认证密钥
        """
        self.address = (host, port)
        self.authkey = authkey
        self.client_id = str(uuid.uuid4())[:8]
        self.conn: Optional[object] = None
        
        # 回调函数存储
        self.callbacks: Dict[str, List[Callable]] = {}  # 频道 -> 回调列表
        
        # 连接状态
        self.connected = False
        self.receiver_thread = None
        self.reconnect_enabled = True
        self.reconnect_interval = 2.0  # 秒
        
        print(f"[IPC Client] 初始化完成，客户端 ID: {self.client_id}")
    
    def connect(self, timeout: float = 5.0) -> bool:
        """
        连接到后台引擎
        
        Args:
            timeout: 连接超时时间（秒）
        
        Returns:
            是否连接成功
        """
        if self.connected:
            return True
        
        try:
            print(f"[IPC Client] 正在连接后台引擎 {self.address[0]}:{self.address[1]}...")
            self.conn = Client(self.address, authkey=self.authkey)
            
            # 发送 hello 消息
            self.conn.send({
                "type": "client_hello",
                "client_id": self.client_id,
                "timestamp": time.time()
            })
            
            # 等待 hello 回复
            try:
                self.conn.settimeout(timeout)
                response = self.conn.recv()
                if response.get("type") == "server_hello":
                    print(f"[IPC Client] ✨ 成功连上后台守护引擎！( •̀ ω •́ )✧")
                    self.connected = True
                    
                    # 启动接收线程
                    self.receiver_thread = threading.Thread(
                        target=self._receive_loop,
                        name="IPC-Receiver",
                        daemon=True
                    )
                    self.receiver_thread.start()
                    
                    # 启动自动重连监控线程
                    threading.Thread(
                        target=self._reconnect_monitor,
                        name="IPC-Reconnect",
                        daemon=True
                    ).start()
                    
                    return True
            except (TimeoutError, EOFError):
                print("[IPC Client] 服务器未响应")
                self.conn = None
                return False
        
        except (ConnectionRefusedError, OSError) as e:
            print(f"[IPC Client] 连接失败: {e}")
            print("[IPC Client] 引擎似乎还在睡懒觉，没连上哦。")
            self.conn = None
            return False
    
    def disconnect(self):
        """断开连接"""
        self.reconnect_enabled = False
        self.connected = False
        
        if self.conn:
            try:
                self.conn.close()
            except:
                pass
            self.conn = None
        
        print("[IPC Client] 已断开连接")
    
    def _receive_loop(self):
        """接收来自引擎的消息"""
        try:
            while self.connected and self.conn:
                try:
                    self.conn.settimeout(5.0)  # 5秒超时
                    msg_dict = self.conn.recv()
                    
                    # 解析消息
                    try:
                        msg = Message.from_dict(msg_dict)
                        
                        # 查找对应的回调
                        if msg.channel in self.callbacks:
                            for callback in self.callbacks[msg.channel]:
                                try:
                                    callback(msg)
                                except Exception as e:
                                    print(f"[IPC Client] 回调执行错误: {e}")
                    
                    except Exception as e:
                        print(f"[IPC Client] 消息解析失败: {e}")
                
                except TimeoutError:
                    # 超时是正常的，继续
                    continue
        
        except EOFError:
            print("[IPC Client] 和引擎的连接已断开")
            self.connected = False
        except Exception as e:
            print(f"[IPC Client] 接收线程错误: {e}")
            self.connected = False
    
    def _reconnect_monitor(self):
        """自动重连监控线程"""
        while self.reconnect_enabled:
            if not self.connected:
                time.sleep(self.reconnect_interval)
                if self.reconnect_enabled:
                    print("[IPC Client] 尝试重新连接...")
                    self.connect()
            else:
                time.sleep(1)
    
    def on(self, channel: str, callback: Callable):
        """
        绑定频道的监听回调
        
        Args:
            channel: 频道名称
            callback: 回调函数，接收 Message 对象
        """
        if channel not in self.callbacks:
            self.callbacks[channel] = []
        
        self.callbacks[channel].append(callback)
        print(f"[IPC Client] 已注册监听: {channel}")
    
    def off(self, channel: str, callback: Optional[Callable] = None):
        """
        取消监听
        
        Args:
            channel: 频道名称
            callback: 要移除的回调，如果为 None 则移除所有
        """
        if channel not in self.callbacks:
            return
        
        if callback is None:
            del self.callbacks[channel]
            print(f"[IPC Client] 已取消监听: {channel}")
        else:
            if callback in self.callbacks[channel]:
                self.callbacks[channel].remove(callback)
    
    def send_request(self, channel: str, data: Optional[Dict] = None) -> bool:
        """
        向后台发送控制请求
        
        Args:
            channel: 目标频道
            data: 请求数据
        
        Returns:
            是否发送成功
        """
        if not self.connected or not self.conn:
            print(f"[IPC Client] 未连接到引擎，无法发送请求: {channel}")
            return False
        
        try:
            msg = Message(
                type=MessageType.REQUEST.value,
                channel=channel,
                data=data or {}
            )
            self.conn.send(msg.to_dict())
            print(f"[IPC Client] 已发送请求: {channel}")
            return True
        except Exception as e:
            print(f"[IPC Client] 发送失败 ({channel}): {e}")
            self.connected = False
            return False
    
    def send_event(self, channel: str, data: Optional[Dict] = None) -> bool:
        """向后台发送事件"""
        if not self.connected or not self.conn:
            return False
        
        try:
            msg = Message(
                type=MessageType.EVENT.value,
                channel=channel,
                data=data or {}
            )
            self.conn.send(msg.to_dict())
            return True
        except Exception as e:
            print(f"[IPC Client] 发送事件失败: {e}")
            self.connected = False
            return False
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.connected and self.conn is not None
    
    # ==================== 便捷方法 ====================
    
    def start_engine(self) -> bool:
        """启动引擎"""
        return self.send_request(Channels.ENGINE_START, {})
    
    def stop_engine(self) -> bool:
        """停止引擎"""
        return self.send_request(Channels.ENGINE_STOP, {})
    
    def request_engine_status(self) -> bool:
        """请求引擎状态"""
        return self.send_request(Channels.ENGINE_STATUS, {})
    
    def create_rule(self, rule_data: Dict) -> bool:
        """创建规则"""
        return self.send_request(Channels.RULE_CREATE, {"rule": rule_data})
    
    def update_rule(self, rule_id: str, rule_data: Dict) -> bool:
        """更新规则"""
        return self.send_request(Channels.RULE_UPDATE, {
            "rule_id": rule_id,
            "rule": rule_data
        })
    
    def delete_rule(self, rule_id: str) -> bool:
        """删除规则"""
        return self.send_request(Channels.RULE_DELETE, {"rule_id": rule_id})
    
    def enable_rule(self, rule_id: str) -> bool:
        """启用规则"""
        return self.send_request(Channels.RULE_ENABLE, {"rule_id": rule_id})
    
    def disable_rule(self, rule_id: str) -> bool:
        """禁用规则"""
        return self.send_request(Channels.RULE_DISABLE, {"rule_id": rule_id})
    
    def request_plugin_discovery(self) -> bool:
        """请求发现可用插件"""
        return self.send_request(Channels.PLUGIN_DISCOVERY, {})


# 使用示例
if __name__ == "__main__":
    import time
    
    # 创建客户端
    remote = UIRemoteController()
    
    # 设置回调
    def on_engine_state_changed(msg):
        print(f"[Demo] 引擎状态变化: {msg.data}")
    
    def on_rule_triggered(msg):
        print(f"[Demo] 规则被触发: {msg.data}")
    
    remote.on(Channels.ENGINE_STATE_CHANGED, on_engine_state_changed)
    remote.on(Channels.RULE_TRIGGERED, on_rule_triggered)
    
    # 尝试连接
    if remote.connect():
        print("[Demo] 连接成功，发送测试请求...")
        remote.request_engine_status()
        
        # 保持运行以接收消息
        print("[Demo] 等待消息... (按 Ctrl+C 退出)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("[Demo] 正在退出...")
            remote.disconnect()
    else:
        print("[Demo] 连接失败")
