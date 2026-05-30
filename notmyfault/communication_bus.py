"""
通信总线模块 - 用于UI和后台脚本之间的双向通信

支持消息传递、事件系统和RPC-like的请求-响应模式
"""

import threading
import queue
import json
import time
import uuid
from typing import Callable, Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from enum import Enum


class MessageType(Enum):
    """消息类型枚举"""
    EVENT = "event"  # 事件消息
    REQUEST = "request"  # 请求消息
    RESPONSE = "response"  # 响应消息
    ERROR = "error"  # 错误消息
    STATUS = "status"  # 状态更新


@dataclass
class Message:
    """消息结构"""
    type: str  # MessageType
    channel: str  # 消息频道
    data: Dict[str, Any]  # 消息数据
    request_id: Optional[str] = None  # 用于请求-响应匹配
    timestamp: float = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
        if self.request_id is None and self.type in [MessageType.REQUEST.value, MessageType.RESPONSE.value]:
            self.request_id = str(uuid.uuid4())
    
    def to_dict(self):
        """转换为字典"""
        return asdict(self)
    
    def to_json(self):
        """转换为JSON字符串"""
        return json.dumps(self.to_dict(), default=str)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        """从字典创建消息"""
        return cls(
            type=data.get("type", ""),
            channel=data.get("channel", ""),
            data=data.get("data", {}),
            request_id=data.get("request_id"),
            timestamp=data.get("timestamp", time.time())
        )


class CommunicationBus:
    """
    通信总线 - 管理UI和脚本之间的消息传递
    
    特性：
    - 发布-订阅事件系统
    - 请求-响应模式
    - 异步消息处理
    - 线程安全
    """
    
    def __init__(self, max_queue_size: int = 1000):
        self.message_queue = queue.Queue(maxsize=max_queue_size)
        self.subscribers: Dict[str, List[Callable]] = {}  # 频道 -> 回调列表
        self.pending_requests: Dict[str, Dict] = {}  # request_id -> 响应信息
        self.lock = threading.Lock()
        self.running = False
        self.worker_thread = None
        self.callbacks_ui_to_script: List[Callable] = []  # UI->脚本的回调
        self.callbacks_script_to_ui: List[Callable] = []  # 脚本->UI的回调
    
    def start(self):
        """启动消息处理线程"""
        if self.running:
            return
        
        self.running = True
        self.worker_thread = threading.Thread(target=self._process_messages, daemon=True)
        self.worker_thread.start()
    
    def stop(self):
        """停止消息处理线程"""
        self.running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=5)
    
    def _process_messages(self):
        """消息处理线程主循环"""
        while self.running:
            try:
                message = self.message_queue.get(timeout=0.5)
                self._handle_message(message)
            except queue.Empty:
                continue
            except Exception as e:
                print(f"消息处理错误: {e}")
    
    def _handle_message(self, message: Message):
        """处理消息"""
        # 处理响应消息
        if message.type == MessageType.RESPONSE.value:
            with self.lock:
                if message.request_id in self.pending_requests:
                    self.pending_requests[message.request_id]["response"] = message
                    self.pending_requests[message.request_id]["event"].set()
            return
        
        # 处理其他消息类型 - 支持通配符匹配
        matched_callbacks = []
        with self.lock:
            for pattern in self.subscribers:
                if self._match_channel(pattern, message.channel):
                    callbacks = self.subscribers[pattern][:]
                    matched_callbacks.extend(callbacks)
        
        for callback in matched_callbacks:
            try:
                callback(message)
            except Exception as e:
                print(f"回调执行错误 ({message.channel}): {e}")
    
    def subscribe(self, channel: str, callback: Callable):
        """
        订阅频道，支持通配符
        
        Args:
            channel: 频道名称或通配符（如 ".*" 匹配所有频道，"rule.*" 匹配 rule 开头的频道）
            callback: 回调函数，接收Message对象
        """
        with self.lock:
            if channel not in self.subscribers:
                self.subscribers[channel] = []
            self.subscribers[channel].append(callback)
    
    def _match_channel(self, pattern: str, channel: str) -> bool:
        """判断频道是否匹配模式（支持简单的通配符）"""
        import fnmatch
        return fnmatch.fnmatch(channel, pattern)
    
    def unsubscribe(self, channel: str, callback: Callable):
        """取消订阅"""
        with self.lock:
            if channel in self.subscribers:
                if callback in self.subscribers[channel]:
                    self.subscribers[channel].remove(callback)
    
    def publish(self, channel: str, data: Dict[str, Any], msg_type: str = "event"):
        """
        发布消息
        
        Args:
            channel: 频道名称
            data: 消息数据
            msg_type: 消息类型
        """
        message = Message(
            type=msg_type,
            channel=channel,
            data=data
        )
        try:
            self.message_queue.put_nowait(message)
        except queue.Full:
            print(f"消息队列已满，消息丢弃: {channel}")
    
    def request(self, channel: str, data: Dict[str, Any], timeout: float = 5.0) -> Optional[Message]:
        """
        发送请求并等待响应
        
        Args:
            channel: 频道名称
            data: 请求数据
            timeout: 等待超时时间（秒）
        
        Returns:
            响应Message对象，或None表示超时
        """
        message = Message(
            type=MessageType.REQUEST.value,
            channel=channel,
            data=data
        )
        
        # 注册待处理请求
        event = threading.Event()
        with self.lock:
            self.pending_requests[message.request_id] = {
                "event": event,
                "response": None,
                "request": message
            }
        
        try:
            self.message_queue.put_nowait(message)
        except queue.Full:
            with self.lock:
                del self.pending_requests[message.request_id]
            return None
        
        # 等待响应
        if event.wait(timeout=timeout):
            with self.lock:
                result = self.pending_requests.get(message.request_id, {}).get("response")
                if message.request_id in self.pending_requests:
                    del self.pending_requests[message.request_id]
            return result
        else:
            # 超时
            with self.lock:
                if message.request_id in self.pending_requests:
                    del self.pending_requests[message.request_id]
            print(f"请求超时: {channel} (request_id: {message.request_id})")
            return None
    
    def respond(self, request_message: Message, data: Dict[str, Any]):
        """
        响应请求
        
        Args:
            request_message: 原始请求消息
            data: 响应数据
        """
        response = Message(
            type=MessageType.RESPONSE.value,
            channel=request_message.channel,
            data=data,
            request_id=request_message.request_id
        )
        try:
            self.message_queue.put_nowait(response)
        except queue.Full:
            print(f"消息队列已满，响应丢弃: {request_message.channel}")
    
    def send_error(self, channel: str, error_msg: str, request_id: Optional[str] = None):
        """发送错误消息"""
        message = Message(
            type=MessageType.ERROR.value,
            channel=channel,
            data={"error": error_msg},
            request_id=request_id
        )
        try:
            self.message_queue.put_nowait(message)
        except queue.Full:
            print(f"消息队列已满，错误消息丢弃: {channel}")


# 全局通信总线实例
_global_bus = None


def get_communication_bus() -> CommunicationBus:
    """获取全局通信总线实例"""
    global _global_bus
    if _global_bus is None:
        _global_bus = CommunicationBus()
        _global_bus.start()
    return _global_bus


def initialize_communication_bus() -> CommunicationBus:
    """初始化全局通信总线"""
    return get_communication_bus()


def shutdown_communication_bus():
    """关闭全局通信总线"""
    global _global_bus
    if _global_bus:
        _global_bus.stop()
        _global_bus = None


# 预定义的频道常量
class Channels:
    """预定义的通信频道"""
    # UI -> Script 频道
    RULE_CREATE = "rule.create"
    RULE_UPDATE = "rule.update"
    RULE_DELETE = "rule.delete"
    RULE_ENABLE = "rule.enable"
    RULE_DISABLE = "rule.disable"
    
    ENGINE_START = "engine.start"
    ENGINE_STOP = "engine.stop"
    ENGINE_STATUS = "engine.status"
    
    # Script -> UI 频道
    ENGINE_STATE_CHANGED = "engine.state_changed"
    RULE_TRIGGERED = "rule.triggered"
    ACTION_EXECUTED = "action.executed"
    ERROR_OCCURRED = "error.occurred"
    
    # 双向频道
    CONFIG_SYNC = "config.sync"
    PLUGIN_DISCOVERY = "plugin.discovery"
