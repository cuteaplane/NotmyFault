import os
import json
import webview
import threading
import sys

from notmyfault.config import get_config
from notmyfault.engine import AutomationEngine
from Win_toaster.AUMID_Register import register_toaster

class ControlCenterApi:
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.config_path = os.path.join(base_dir, "config.json")
        self.engine_thread = None
        self.is_running = False

    def get_plugins_schema(self):
        """扫描 triggers 和 actions 文件夹，把插件清单告诉前端"""
        schema = {"triggers": {}, "actions": {}}
        
        for plugin_type in ["triggers", "actions"]:
            plugin_dir = os.path.join(self.base_dir, "notmyfault", plugin_type)
            if not os.path.exists(plugin_dir):
                continue
                
            for folder in os.listdir(plugin_dir):
                json_file = os.path.join(plugin_dir, folder, f"{plugin_type[:-1]}.json")
                if os.path.exists(json_file):
                    try:
                        with open(json_file, 'r', encoding='utf-8') as f:
                            schema[plugin_type][folder] = json.load(f)
                    except Exception as e:
                        print(f"读取清单 {json_file} 失败: {e}")
                        
        return json.dumps(schema, ensure_ascii=False)

    def load_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.dumps(json.load(f), ensure_ascii=False)
            except Exception:
                pass
        return json.dumps({"rules": []})

    def save_config(self, json_data):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(json.loads(json_data), f, ensure_ascii=False, indent=4)
            return "SUCCESS"
        except Exception as e:
            return f"ERROR: {e}"

    def start_engine(self):
        """不改动架构的前提下，一键启动你的无敌引擎！"""
        if self.is_running:
            return "ALREADY_RUNNING"
            
        try:
            register_toaster()
            config = get_config()
            engine = AutomationEngine(config)
            engine.auto_load(os.path.join(self.base_dir, "notmyfault"))
            
            # 放在守护线程里跑，关掉界面就会自动停止~
            self.engine_thread = threading.Thread(target=engine.start, daemon=True)
            self.engine_thread.start()
            self.is_running = True
            return "STARTED"
        except Exception as e:
            return f"ERROR: {e}"

def run_control_center():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "dashboard.html")
    
    api = ControlCenterApi(base_dir)
    
    window = webview.create_window(
        title="NotmyFault - 控制中心",
        url=html_path,
        js_api=api,
        width=1000,
        height=800,
        background_color="#fdfcff"  # MD3 Surface 颜色
    )
    webview.start()

if __name__ == '__main__':
    run_control_center()