
import sys
import os
import time
import signal
import threading
import json

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from notmyfault import run as start_engine
from notmyfault.communication_bus import (
    initialize_communication_bus, shutdown_communication_bus,
    get_communication_bus, Message, MessageType
)
from notmyfault.ipc_server import start_ipc_server, stop_ipc_server
from notmyfault.config_sync import ConfigSyncManager, ConfigSyncBridge
from notmyfault.config import CONFIG_FILE


class EngineRunner:
    """后台引擎运行器 — 管理引擎生命周期，注册 IPC 命令处理器"""

    def __init__(self):
        self.engine_running = False
        self.shutdown_event = threading.Event()
        self.engine_thread = None
        self.bus = None

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        print(f"\n[Engine] 收到关闭信号 ({signum})，正在优雅关闭...")
        self.shutdown_event.set()

    # ================================================================
    # IPC 命令处理器
    # ================================================================
    def _setup_ipc_handlers(self):
        """注册 IPC 命令处理器到通信总线"""
        self.bus = get_communication_bus()

        def on_engine_start(msg: Message):
            print("[Engine] 收到 IPC 启动指令")
            if not self.engine_running:
                self._start_engine_core()
            self.bus.respond(msg, {"ok": True, "running": self.engine_running})

        def on_engine_stop(msg: Message):
            print("[Engine] 收到 IPC 停止指令")
            self.shutdown_event.set()
            if self.engine_thread and self.engine_thread.is_alive():
                self.engine_thread.join(timeout=3)
            self.engine_running = False
            self.bus.respond(msg, {"ok": True})

        def on_engine_status(msg: Message):
            config = {}
            try:
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        config = json.load(f)
            except Exception:
                pass
            rules = config.get("rules", [])
            self.bus.respond(msg, {
                "running": self.engine_running,
                "pid": os.getpid(),
                "rules_count": len(rules),
                "triggers_count": len(set(r.get("event", {}).get("type", "") for r in rules)),
                "actions_count": len(set(a.get("type", "") for r in rules for a in r.get("actions", []))),
            })

        def on_rules_list(msg: Message):
            try:
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        config = json.load(f)
                else:
                    config = {"rules": []}
            except Exception:
                config = {"rules": []}
            self.bus.respond(msg, config)

        def on_rules_save(msg: Message):
            new_config = msg.data.get("config", {})
            try:
                os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(new_config, f, ensure_ascii=False, indent=4)
                print(f"[Engine] 规则已更新 ({len(new_config.get('rules', []))} 条)")
                self.bus.respond(msg, {"ok": True})
            except Exception as e:
                self.bus.respond(msg, {"ok": False, "error": str(e)})

        self.bus.subscribe("engine.start", on_engine_start)
        self.bus.subscribe("engine.stop", on_engine_stop)
        self.bus.subscribe("engine.status", on_engine_status)
        self.bus.subscribe("rules.list", on_rules_list)
        self.bus.subscribe("rules.save", on_rules_save)
        print("[Engine] IPC 命令处理器已注册")

    # ================================================================
    # 引擎生命周期
    # ================================================================
    def _start_engine_core(self):
        self.shutdown_event.clear()
        self.engine_thread = threading.Thread(
            target=self._run_engine,
            name="Engine-Core",
            daemon=False
        )
        self.engine_thread.start()

    def _run_engine(self):
        try:
            self.engine_running = True
            start_engine(shutdown_event=self.shutdown_event)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(f"[Engine] 引擎错误: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.engine_running = False

    # ================================================================
    # 启动流程
    # ================================================================
    def run(self):
        print("=" * 50)
        print("  NotmyFault 后台守护引擎")
        print("=" * 50)

        try:
            # 1. 通信总线
            print("[启动] 初始化通信总线...")
            initialize_communication_bus()
            time.sleep(0.3)

            # 2. IPC 服务器
            print("[启动] 启动 IPC 服务器...")
            ipc_server = start_ipc_server(host='localhost', port=19198)

            # 3. 配置同步
            print("[启动] 启用配置同步...")
            config_sync = ConfigSyncManager(CONFIG_FILE)
            bridge = ConfigSyncBridge(config_sync)
            bridge.setup()

            # 4. 注册 IPC 命令处理器
            self._setup_ipc_handlers()

            # 5. 启动引擎
            print("[启动] 启动主引擎...")
            self._start_engine_core()

            print("\n  IPC: localhost:19198")
            print("  按 Ctrl+C 停止\n")

            # 6. 状态监控
            self._status_loop(ipc_server)

        except Exception as e:
            print(f"\n启动失败: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._cleanup()

    def _status_loop(self, ipc_server):
        import datetime
        while not self.shutdown_event.is_set():
            try:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                clients = ipc_server.get_client_count() if ipc_server else 0
                state = "running" if self.engine_running else "stopped"
                print(f"  [{ts}] 引擎: {state} | 客户端: {clients}")
            except Exception:
                pass
            self.shutdown_event.wait(30)

    def _cleanup(self):
        print("\n[清理] 正在关闭...")
        self.shutdown_event.set()
        if self.engine_thread and self.engine_thread.is_alive():
            self.engine_thread.join(timeout=5)
        try:
            stop_ipc_server()
        except Exception:
            pass
        try:
            shutdown_communication_bus()
        except Exception:
            pass
        print("[清理] 完成")


def main():
    runner = EngineRunner()
    runner.run()


if __name__ == "__main__":
    main()
