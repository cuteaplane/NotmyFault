"""
NotmyFault 后台引擎启动脚本（增强版）
集成了 IPC 服务器，支持 UI 远程控制

特性：
- 启动后台引擎
- 启动 IPC 服务器用于 UI 通信
- 启用配置同步
- 支持优雅关闭

使用方法：
    python NOTMYFAULT_Enhanced.pyw
    
或者设为开机自启动
"""

import sys
import os
import time
import signal
import threading

# 添加项目路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# 导入必要的模块
from notmyfault import run as start_engine
from notmyfault.communication_bus import initialize_communication_bus, shutdown_communication_bus
from notmyfault.ipc_server import start_ipc_server, stop_ipc_server
from notmyfault.config_sync import ConfigSyncManager, ConfigSyncBridge
from notmyfault.config import CONFIG_FILE


class EngineRunner:
    """后台引擎运行器"""
    
    def __init__(self):
        self.engine_running = False
        self.shutdown_event = threading.Event()
        
        # 设置信号处理（优雅关闭）
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """处理系统信号（Ctrl+C）"""
        print(f"\n[Engine] 收到关闭信号 ({signum})，正在优雅关闭...")
        self.shutdown_event.set()
    
    def run(self, enable_ipc: bool = True, enable_config_sync: bool = True):
        """
        运行后台引擎
        
        Args:
            enable_ipc: 是否启用 IPC 服务器
            enable_config_sync: 是否启用配置同步
        """
        print("=" * 60)
        print("🚀 NotmyFault 后台守护引擎正在启动...")
        print("=" * 60)
        
        try:
            # 1. 初始化通信总线
            print("\n[启动序列] 初始化通信总线...")
            initialize_communication_bus()
            time.sleep(0.5)
            
            # 2. 启动 IPC 服务器（如果启用）
            if enable_ipc:
                print("[启动序列] 启动 IPC 服务器...")
                ipc_server = start_ipc_server(host='localhost', port=19198)
                print(f"[启动序列] ✨ IPC 服务器已启动，UI 可以连接了！")
            else:
                print("[启动序列] 已禁用 IPC 服务器")
                ipc_server = None
            
            # 3. 启用配置同步（如果启用）
            if enable_config_sync:
                print("[启动序列] 启用配置同步...")
                config_sync = ConfigSyncManager(CONFIG_FILE)
                bridge = ConfigSyncBridge(config_sync)
                bridge.setup()
                print(f"[启动序列] 配置同步已启用")
            else:
                print("[启动序列] 已禁用配置同步")
            
            # 4. 启动主引擎（在单独的线程中）
            print("[启动序列] 启动主引擎核心...")
            engine_thread = threading.Thread(
                target=self._run_engine,
                name="Engine-Core",
                daemon=False
            )
            engine_thread.start()
            
            # 5. 启动监控线程（定期输出状态）
            print("[启动序列] 启动状态监控...")
            monitor_thread = threading.Thread(
                target=self._status_monitor,
                args=(ipc_server,),
                name="Engine-Monitor",
                daemon=True
            )
            monitor_thread.start()
            
            print("\n" + "=" * 60)
            print("✅ NotmyFault 后台引擎已启动！")
            print("=" * 60)
            print("\n📡 IPC 服务器监听地址: localhost:19198")
            print("🎮 现在可以启动 UI 进行远程控制")
            print("\n按 Ctrl+C 或关闭此窗口以停止引擎")
            print("=" * 60 + "\n")
            
            # 等待关闭信号
            engine_thread.join()
        
        except Exception as e:
            print(f"\n❌ 启动失败: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._cleanup()
    
    def _run_engine(self):
        """运行引擎核心"""
        try:
            self.engine_running = True
            start_engine()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(f"[Engine] 引擎错误: {e}")
        finally:
            self.engine_running = False
            self.shutdown_event.set()
    
    def _status_monitor(self, ipc_server):
        """定期输出状态"""
        import datetime
        
        while not self.shutdown_event.is_set():
            try:
                timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                
                status_info = f"[{timestamp}] 引擎状态: "
                
                if self.engine_running:
                    status_info += "✅ 运行中"
                else:
                    status_info += "⏸️ 已停止"
                
                if ipc_server:
                    client_count = ipc_server.get_client_count()
                    status_info += f" | IPC 客户端: {client_count}"
                
                print(status_info)
            
            except Exception as e:
                print(f"[Monitor] 监控错误: {e}")
            
            # 每 30 秒输出一次
            self.shutdown_event.wait(30)
    
    def _cleanup(self):
        """清理资源"""
        print("\n[清理] 正在关闭引擎...")
        
        try:
            stop_ipc_server()
            print("[清理] IPC 服务器已关闭")
        except:
            pass
        
        try:
            shutdown_communication_bus()
            print("[清理] 通信总线已关闭")
        except:
            pass
        
        print("[清理] 引擎已完全关闭")
        print("\n" + "=" * 60)
        print("👋 NotmyFault 后台引擎已停止")
        print("=" * 60)


def main():
    """主函数"""
    runner = EngineRunner()
    
    # 检查命令行参数
    enable_ipc = "--no-ipc" not in sys.argv
    enable_config_sync = "--no-config-sync" not in sys.argv
    
    # 运行引擎
    runner.run(enable_ipc=enable_ipc, enable_config_sync=enable_config_sync)


if __name__ == "__main__":
    main()
