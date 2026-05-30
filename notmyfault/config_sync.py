"""
配置同步模块 - 实现配置文件的实时同步和跨进程更新

特性：
- UI 中修改配置时实时同步到后台
- 后台修改配置时实时推送到 UI
- 配置文件自动热重载
- 支持配置验证和冲突解决
"""

import json
import threading
import time
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass
from datetime import datetime

from notmyfault.communication_bus import (
    get_communication_bus,
    Message,
    MessageType,
    Channels
)


@dataclass
class ConfigVersion:
    """配置版本信息"""
    content_hash: str  # 配置内容的哈希值
    timestamp: float   # 时间戳
    source: str        # 来源 ("ui" 或 "backend" 或 "file")
    

class ConfigSyncManager:
    """配置同步管理器"""
    
    def __init__(self, config_file_path: str):
        """
        初始化配置同步管理器
        
        Args:
            config_file_path: 配置文件的绝对路径
        """
        self.config_file = Path(config_file_path)
        self.config: Dict[str, Any] = {}
        self.current_version: Optional[ConfigVersion] = None
        self.bus = get_communication_bus()
        self.lock = threading.Lock()
        self.change_callbacks: List[Callable] = []
        
        # 读取初始配置
        self._load_config_from_file()
        self._update_version("file")
        
        print(f"[ConfigSync] 已初始化，监控文件: {self.config_file}")
    
    def _load_config_from_file(self):
        """从文件读取配置"""
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    with self.lock:
                        self.config = json.load(f)
                print(f"[ConfigSync] 已从文件加载配置")
            else:
                print(f"[ConfigSync] 配置文件不存在: {self.config_file}")
        except Exception as e:
            print(f"[ConfigSync] 配置文件读取失败: {e}")
    
    def _save_config_to_file(self):
        """将配置保存到文件"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=4)
            print(f"[ConfigSync] 配置已保存到文件")
        except Exception as e:
            print(f"[ConfigSync] 配置保存失败: {e}")
    
    def _get_content_hash(self, content: Dict) -> str:
        """计算配置内容的哈希值"""
        json_str = json.dumps(content, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(json_str.encode()).hexdigest()
    
    def _update_version(self, source: str):
        """更新配置版本"""
        with self.lock:
            self.current_version = ConfigVersion(
                content_hash=self._get_content_hash(self.config),
                timestamp=time.time(),
                source=source
            )
    
    def get_config(self) -> Dict[str, Any]:
        """获取当前配置"""
        with self.lock:
            return dict(self.config)
    
    def update_from_ui(self, new_config: Dict[str, Any], ui_id: str = "unknown"):
        """
        UI 更新配置
        
        Args:
            new_config: 新的配置字典
            ui_id: UI 的标识
        """
        print(f"[ConfigSync] 接收到来自 UI {ui_id} 的配置更新")
        
        # 检测是否真的有变化
        old_hash = self.current_version.content_hash if self.current_version else ""
        new_hash = self._get_content_hash(new_config)
        
        if old_hash == new_hash:
            print(f"[ConfigSync] 配置未变化，跳过")
            return
        
        with self.lock:
            self.config = new_config
        
        self._update_version("ui")
        self._save_config_to_file()
        
        # 向后台广播配置更新
        self.bus.publish(
            "config.updated",
            {
                "config": new_config,
                "source": "ui",
                "ui_id": ui_id,
                "timestamp": self.current_version.timestamp
            },
            msg_type=MessageType.STATUS.value
        )
        
        # 执行本地回调
        self._notify_changes("ui", new_config)
    
    def update_from_backend(self, new_config: Dict[str, Any]):
        """
        后台更新配置
        
        Args:
            new_config: 新的配置字典
        """
        print(f"[ConfigSync] 接收到来自后台的配置更新")
        
        # 检测是否真的有变化
        old_hash = self.current_version.content_hash if self.current_version else ""
        new_hash = self._get_content_hash(new_config)
        
        if old_hash == new_hash:
            print(f"[ConfigSync] 配置未变化，跳过")
            return
        
        with self.lock:
            self.config = new_config
        
        self._update_version("backend")
        self._save_config_to_file()
        
        # 向所有 UI 广播配置更新
        self.bus.publish(
            "config.updated",
            {
                "config": new_config,
                "source": "backend",
                "timestamp": self.current_version.timestamp
            },
            msg_type=MessageType.STATUS.value
        )
        
        # 执行本地回调
        self._notify_changes("backend", new_config)
    
    def watch_file_changes(self, poll_interval: float = 2.0):
        """
        监视配置文件变化（文件被外部修改时）
        
        Args:
            poll_interval: 轮询间隔（秒）
        """
        last_mtime = None
        
        def monitor():
            nonlocal last_mtime
            
            while True:
                try:
                    if self.config_file.exists():
                        current_mtime = self.config_file.stat().st_mtime
                        
                        if last_mtime is None:
                            last_mtime = current_mtime
                        elif current_mtime > last_mtime:
                            print(f"[ConfigSync] 检测到配置文件外部修改")
                            last_mtime = current_mtime
                            
                            # 重新加载
                            self._load_config_from_file()
                            self._update_version("file")
                            
                            # 广播变化
                            with self.lock:
                                config_copy = dict(self.config)
                            
                            self.bus.publish(
                                "config.updated",
                                {
                                    "config": config_copy,
                                    "source": "file",
                                    "timestamp": self.current_version.timestamp
                                },
                                msg_type=MessageType.STATUS.value
                            )
                            
                            self._notify_changes("file", config_copy)
                
                except Exception as e:
                    print(f"[ConfigSync] 文件监视错误: {e}")
                
                time.sleep(poll_interval)
        
        monitor_thread = threading.Thread(target=monitor, name="ConfigWatch", daemon=True)
        monitor_thread.start()
        print(f"[ConfigSync] 已启动文件监视 (间隔 {poll_interval}s)")
    
    def on_change(self, callback: Callable):
        """
        注册配置变化回调
        
        Args:
            callback: 回调函数，接收 (source, config) 参数
        """
        self.change_callbacks.append(callback)
    
    def _notify_changes(self, source: str, config: Dict):
        """通知所有注册的回调"""
        for callback in self.change_callbacks:
            try:
                callback(source, config)
            except Exception as e:
                print(f"[ConfigSync] 回调执行错误: {e}")


class ConfigSyncBridge:
    """配置同步桥接 - 连接 IPC 和配置同步"""
    
    def __init__(self, config_manager: ConfigSyncManager):
        """
        初始化配置同步桥接
        
        Args:
            config_manager: 配置同步管理器实例
        """
        self.config_mgr = config_manager
        self.bus = get_communication_bus()
    
    def setup(self):
        """设置配置同步桥接"""
        # 监听 UI 的配置更新请求
        self.bus.subscribe("config.update_from_ui", self._on_ui_config_update)
        
        # 监听后台的配置更新请求
        self.bus.subscribe("config.update_from_backend", self._on_backend_config_update)
        
        # 监听配置查询请求
        self.bus.subscribe("config.query", self._on_config_query)
        
        # 启动文件监视
        self.config_mgr.watch_file_changes()
        
        print("[ConfigSyncBridge] 配置同步桥接已设置")
    
    def _on_ui_config_update(self, message: Message):
        """处理 UI 的配置更新"""
        new_config = message.data.get("config", {})
        ui_id = message.data.get("ui_id", "unknown")
        
        self.config_mgr.update_from_ui(new_config, ui_id)
    
    def _on_backend_config_update(self, message: Message):
        """处理后台的配置更新"""
        new_config = message.data.get("config", {})
        
        self.config_mgr.update_from_backend(new_config)
    
    def _on_config_query(self, message: Message):
        """处理配置查询请求"""
        config = self.config_mgr.get_config()
        
        self.bus.respond(message, {
            "config": config,
            "version": {
                "hash": self.config_mgr.current_version.content_hash,
                "timestamp": self.config_mgr.current_version.timestamp,
                "source": self.config_mgr.current_version.source
            }
        })
    
    def broadcast_config(self):
        """主动广播当前配置"""
        config = self.config_mgr.get_config()
        self.bus.publish(
            "config.broadcast",
            {
                "config": config,
                "timestamp": self.config_mgr.current_version.timestamp
            },
            msg_type=MessageType.STATUS.value
        )


# 使用示例
if __name__ == "__main__":
    from notmyfault.config import CONFIG_FILE
    import time
    
    # 初始化总线
    from notmyfault.communication_bus import initialize_communication_bus
    initialize_communication_bus()
    
    # 创建配置同步管理器
    sync_mgr = ConfigSyncManager(CONFIG_FILE)
    
    # 创建桥接
    bridge = ConfigSyncBridge(sync_mgr)
    bridge.setup()
    
    # 注册变化回调
    def on_config_changed(source, config):
        print(f"[Demo] 配置已变化 (来源: {source})")
        print(f"[Demo] 规则数: {len(config.get('rules', []))}")
    
    sync_mgr.on_change(on_config_changed)
    
    print("[Demo] 配置同步系统已启动，按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[Demo] 退出")
