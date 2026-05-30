"""
IPC 服务器 - 运行在后台引擎进程中
用于与 UI 进程进行跨进程通信

特性：
- 监听 UI 连接请求
- 转发引擎内部事件给所有连接的 UI
- 接收 UI 指令并投递到引擎
- 自动清理掉线的客户端
"""

import threading
import time
from multiprocessing.connection import Listener
from typing import List, Optional, Callable
from notmyfault.communication_bus import (
    get_communication_bus, 
    Message, 
    MessageType,
    Channels
)


class EngineIPCServer:
    """
    后台引擎的 IPC 服务器
    
    负责：
    1. 监听 UI 的连接请求
    2. 将引擎内部事件广播给所有连接的 UI
    3. 接收并转发 UI 的控制指令到引擎
    """
    
    def __init__(self, host: str = 'localhost', port: int = 19198, authkey: bytes = b'notmyfault_ipc_key'):
        """
        初始化 IPC 服务器
        
        Args:
            host: 监听地址
            port: 监听端口
            authkey: 认证密钥（防止未授权连接）
        """
        self.address = (host, port)
        self.authkey = authkey
        self.bus = get_communication_bus()
        self.clients: List = []  # 所有连接的 UI 客户端
        self.lock = threading.Lock()
        self.running = False
        self.listener_thread = None
        self.event_forwarder_thread = None
        
        print(f"[IPC Server] 初始化完成，准备在 {host}:{port} 启动")
    
    def start(self):
        """启动 IPC 服务器"""
        if self.running:
            print("[IPC Server] 服务器已在运行")
            return
        
        self.running = True
        
        # 启动监听线程
        self.listener_thread = threading.Thread(
            target=self._listen_for_connections,
            name="IPC-Listener",
            daemon=True
        )
        self.listener_thread.start()
        
        # 启动事件转发线程
        self.event_forwarder_thread = threading.Thread(
            target=self._setup_event_forwarding,
            name="IPC-Forwarder",
            daemon=True
        )
        self.event_forwarder_thread.start()
        
        print(f"[IPC Server] 引擎后台天线已升起，监听 {self.address[0]}:{self.address[1]} 📡")
    
    def stop(self):
        """停止 IPC 服务器"""
        self.running = False
        
        # 关闭所有客户端连接
        with self.lock:
            for conn in self.clients:
                try:
                    conn.close()
                except:
                    pass
            self.clients.clear()
        
        print("[IPC Server] 服务器已关闭")
    
    def _listen_for_connections(self):
        """监听来自 UI 的连接请求"""
        try:
            with Listener(self.address, authkey=self.authkey) as listener:
                print(f"[IPC Server] 监听器已就绪，等待 UI 连接...")
                
                while self.running:
                    try:
                        # 设置超时以便定期检查 self.running 标志
                        listener.close()  # 关闭旧的监听器
                        
                        # 重新创建以支持超时
                        with Listener(self.address, authkey=self.authkey) as new_listener:
                            conn = new_listener.accept()
                            print("[IPC Server] ✨ 捕捉到一个可爱的 UI 遥控器连接！")
                            
                            with self.lock:
                                self.clients.append(conn)
                            
                            # 为每个 UI 开个小线程听它说话
                            client_thread = threading.Thread(
                                target=self._handle_client,
                                args=(conn,),
                                name=f"IPC-Client-{len(self.clients)}",
                                daemon=True
                            )
                            client_thread.start()
                    except OSError:
                        # 端口被占用或其他错误，重试
                        if self.running:
                            time.sleep(1)
                        break
        except Exception as e:
            print(f"[IPC Server] 监听器错误: {e}")
    
    def _handle_client(self, conn):
        """处理单个 UI 客户端的连接"""
        client_id = None
        
        try:
            while self.running:
                msg_dict = conn.recv()  # 接收 UI 发来的字典
                
                # 第一条消息应该是客户端标识
                if msg_dict.get("type") == "client_hello":
                    client_id = msg_dict.get("client_id", "unknown")
                    print(f"[IPC Server] UI 客户端 {client_id} 已确认连接")
                    # 回复 hello
                    conn.send({"type": "server_hello", "status": "connected"})
                    continue
                
                # 处理正常的消息
                try:
                    msg = Message.from_dict(msg_dict)
                    print(f"[IPC Server] 收到 UI 指令 ({client_id}): {msg.channel}")
                    
                    # 将消息直接投递到通信总线处理
                    self.bus.message_queue.put_nowait(msg)
                except Exception as e:
                    print(f"[IPC Server] 消息反序列化失败: {e}")
        
        except EOFError:
            print(f"[IPC Server] UI 客户端 {client_id} 断开连接了~")
        except Exception as e:
            print(f"[IPC Server] 客户端处理错误 ({client_id}): {e}")
        finally:
            try:
                conn.close()
            except:
                pass
            
            with self.lock:
                if conn in self.clients:
                    self.clients.remove(conn)
    
    def _setup_event_forwarding(self):
        """设置事件转发 - 将引擎内部事件广播给所有 UI"""
        
        def broadcast_event(message: Message):
            """将消息广播给所有连接的 UI"""
            # 只转发 EVENT、STATUS、ERROR 类型的消息
            if message.type not in [MessageType.EVENT.value, MessageType.STATUS.value, MessageType.ERROR.value]:
                return
            
            dead_clients = []
            msg_dict = message.to_dict()
            
            with self.lock:
                clients_copy = self.clients[:]
            
            for conn in clients_copy:
                try:
                    conn.send(msg_dict)
                except (EOFError, BrokenPipeError, OSError):
                    dead_clients.append(conn)
                except Exception as e:
                    print(f"[IPC Server] 转发消息时出错: {e}")
                    dead_clients.append(conn)
            
            # 清理掉线的客户端
            if dead_clients:
                with self.lock:
                    for conn in dead_clients:
                        if conn in self.clients:
                            self.clients.remove(conn)
        
        # 订阅所有事件（使用通配符）
        self.bus.subscribe("*", broadcast_event)
        print("[IPC Server] 事件转发已启用，将转播所有事件给 UI")
    
    def send_to_ui(self, message: Message):
        """直接向所有 UI 发送消息"""
        msg_dict = message.to_dict()
        dead_clients = []
        
        with self.lock:
            clients_copy = self.clients[:]
        
        for conn in clients_copy:
            try:
                conn.send(msg_dict)
            except (EOFError, BrokenPipeError, OSError):
                dead_clients.append(conn)
        
        if dead_clients:
            with self.lock:
                for conn in dead_clients:
                    if conn in self.clients:
                        self.clients.remove(conn)
    
    def get_client_count(self) -> int:
        """获取当前连接的 UI 数量"""
        with self.lock:
            return len(self.clients)


# 全局 IPC 服务器实例
_global_ipc_server = None


def get_ipc_server(host: str = 'localhost', port: int = 19198) -> EngineIPCServer:
    """获取全局 IPC 服务器实例"""
    global _global_ipc_server
    if _global_ipc_server is None:
        _global_ipc_server = EngineIPCServer(host=host, port=port)
    return _global_ipc_server


def start_ipc_server(host: str = 'localhost', port: int = 19198) -> EngineIPCServer:
    """启动全局 IPC 服务器"""
    server = get_ipc_server(host=host, port=port)
    server.start()
    return server


def stop_ipc_server():
    """停止全局 IPC 服务器"""
    global _global_ipc_server
    if _global_ipc_server:
        _global_ipc_server.stop()
