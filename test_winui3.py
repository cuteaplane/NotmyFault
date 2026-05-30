import flet as ft
import os
import json
import threading

# 导入项目配置和通信模块
from notmyfault.config import get_config, CONFIG_FILE
from notmyfault.communication_bridge import UIComBridge, setup_communication, cleanup_communication
from notmyfault.communication_bus import Channels, initialize_communication_bus
from notmyfault.ipc_client import UIRemoteController

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class NotmyFaultUI:
    def __init__(self):
        self.config = get_config()
        # 确保 rules 是个列表
        if "rules" not in self.config:
            self.config["rules"] = []
        
        self.plugins = {"triggers": {}, "actions": {}}
        self.scan_plugins()

    def scan_plugins(self):
        """扫描本地的 triggers 和 actions 插件"""
        for p_type in ["triggers", "actions"]:
            p_dir = os.path.join(BASE_DIR, "notmyfault", p_type)
            if not os.path.exists(p_dir):
                continue
            for folder in os.listdir(p_dir):
                json_file = os.path.join(p_dir, folder, f"{p_type[:-1]}.json")
                if os.path.exists(json_file):
                    try:
                        with open(json_file, 'r', encoding='utf-8') as f:
                            self.plugins[p_type][folder] = json.load(f)
                    except Exception as e:
                        print(f"读取插件 {json_file} 失败: {e}")

    def save_config_to_disk(self):
        """将内存中的 self.config 写入 config.json"""
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            print(f"保存失败: {e}")
            return False


def main(page: ft.Page):
    # ==========================================
    # 初始化通信系统
    # ==========================================
    # 初始化本地通信总线
    setup_communication()
    com_bridge = UIComBridge(page)
    com_bridge.setup_listeners()
    
    # 初始化 IPC 客户端（连接到后台引擎）
    remote = UIRemoteController()
    connection_status = {"connected": False, "engine_running": False}
    
    def on_ipc_connected():
        """IPC 连接成功处理"""
        connection_status["connected"] = True
        print("[UI] ✨ IPC 客户端已连接到后台引擎")
        # 请求初始状态
        remote.request_engine_status()
    
    def on_ipc_disconnected():
        """IPC 连接断开处理"""
        connection_status["connected"] = False
        connection_status["engine_running"] = False
        print("[UI] ❌ IPC 连接已断开")
    
    # 设置 IPC 事件监听
    def on_engine_state_changed(msg):
        state = msg.data.get("state", "unknown")
        connection_status["engine_running"] = (state == "running")
        if engine_status_text:
            if state == "running":
                engine_status_text.value = "✅ 引擎运行中"
                engine_status_text.color = ft.Colors.PRIMARY
            else:
                engine_status_text.value = "⏸️ 引擎已停止"
                engine_status_text.color = ft.Colors.OUTLINE
            page.update()
    
    remote.on(Channels.ENGINE_STATE_CHANGED, on_engine_state_changed)
    remote.on(Channels.RULE_TRIGGERED, lambda msg: print(f"[UI] 规则触发: {msg.data}"))
    remote.on(Channels.ERROR_OCCURRED, lambda msg: print(f"[UI] 错误: {msg.data}"))
    
    # 尝试连接到后台引擎
    def connect_to_backend():
        if remote.connect(timeout=5.0):
            on_ipc_connected()
        else:
            print("[UI] ⚠️  未能连接到后台引擎，继续以本地模式运行")
    
    # 在后台线程中尝试连接
    threading.Thread(target=connect_to_backend, daemon=True).start()
    
    # 页面关闭时清理
    def on_page_close(e):
        cleanup_communication()
        remote.disconnect()
    
    page.on_close = on_page_close
    
    # ==========================================
    # 🌟 纯正 Material Design 3 引擎注入！
    # ==========================================
    page.theme = ft.Theme(
        # 只要给一个种子颜色，MD3 就会自动计算出所有和谐的 Surface, Primary 等颜色！
        color_scheme_seed=ft.Colors.BLUE, 
        use_material3=True, 
        font_family="Segoe UI Variable Text, Microsoft YaHei UI, sans-serif"
    )
    # 使用 MD3 标准的底层背景色
    page.bgcolor = ft.Colors.SURFACE
    page.title = "NotmyFault Control Center"
    page.window.width = 1050
    page.window.height = 750
    page.padding = 0
    
    data_manager = NotmyFaultUI()

    # 预先声明主要画板
    dashboard_content = ft.Column(expand=True)
    rules_content = ft.Column(expand=True)
    plugins_content = ft.Column(expand=True)
    # 这是我们的新朋友：规则编辑子 UI！📝
    rule_editor_content = ft.Column(expand=True) 

    # ==========================================
    # 🔌 页面 1: 仪表盘 (MD3 卡片风格)
    # ==========================================
    engine_status_text = ft.Text("连接中... 📡", size=16, color=ft.Colors.PRIMARY, weight=ft.FontWeight.BOLD)
    
    def refresh_engine_status():
        """刷新引擎状态"""
        if connection_status["connected"]:
            # 通过 IPC 请求状态
            remote.request_engine_status()
        else:
            engine_status_text.value = "⚠️ 未连接到引擎"
            engine_status_text.color = ft.Colors.WARNING
        page.update()
    
    def start_engine_click(e):
        """点击启动引擎"""
        if connection_status["connected"]:
            remote.start_engine()
            engine_status_text.value = "正在启动... ⏳"
            engine_status_text.color = ft.Colors.TERTIARY
            page.update()
        else:
            page.snack_bar = ft.SnackBar(ft.Text("未连接到后台引擎"), bgcolor=ft.Colors.WARNING)
            page.snack_bar.open = True
            page.update()
    
    def stop_engine_click(e):
        """点击停止引擎"""
        if connection_status["connected"]:
            remote.stop_engine()
            engine_status_text.value = "正在停止... ⏳"
            engine_status_text.color = ft.Colors.TERTIARY
            page.update()
        else:
            page.snack_bar = ft.SnackBar(ft.Text("未连接到后台引擎"), bgcolor=ft.Colors.WARNING)
            page.snack_bar.open = True
            page.update()
    
    dashboard_content.controls = [
        ft.Text("系统概览", size=32, weight=ft.FontWeight.BOLD, color=ft.Colors.ON_SURFACE),
        ft.Container(height=16),
        # 使用 MD3 专属的 Card 组件，自带优雅的表面色调和圆角！
        ft.Card(
            variant=ft.CardVariant.ELEVATED,
            elevation=1,
            content=ft.Container(
                padding=24,
                content=ft.Column([
                    ft.Text("后台引擎状态", size=14, color=ft.Colors.ON_SURFACE_VARIANT),
                    engine_status_text,
                    ft.Text("※ 通过 IPC 与后台实时通信", size=12, color=ft.Colors.OUTLINE),
                    ft.Container(height=16),
                    ft.Row([
                        ft.FilledButton("启动引擎", icon=ft.Icons.PLAY_ARROW, on_click=start_engine_click),
                        ft.OutlinedButton("停止引擎", icon=ft.Icons.STOP, on_click=stop_engine_click),
                        ft.FilledTonalButton("刷新状态", icon=ft.Icons.REFRESH, on_click=lambda e: refresh_engine_status())
                    ], spacing=8)
                ])
            )
        )
    ]

    # ==========================================
    # 🧩 页面 2: 插件库 (MD3 列表风格)
    # ==========================================
    plugin_tiles = []
    for plugin_id, meta in data_manager.plugins["triggers"].items():
        # 使用 MD3 最经典的 ListTile (瓦片) 来排版，极其规范！
        plugin_tiles.append(
            ft.Card(
                variant=ft.CardVariant.OUTLINED,
                content=ft.ListTile(
                    leading=ft.Icon(ft.Icons.BOLT, color=ft.Colors.PRIMARY),
                    title=ft.Text(meta.get("name", plugin_id), weight=ft.FontWeight.BOLD),
                    subtitle=ft.Text(meta.get("description", "无描述")),
                    trailing=ft.Text(f"ID: {plugin_id}", color=ft.Colors.OUTLINE),
                )
            )
        )
    
    plugins_content.controls = [
        ft.Text("已加载插件库", size=32, weight=ft.FontWeight.BOLD),
        ft.Container(height=16),
        ft.Text("触发器 (Triggers)", size=18, weight=ft.FontWeight.W_600, color=ft.Colors.PRIMARY),
        ft.Column(plugin_tiles, scroll=ft.ScrollMode.AUTO, expand=True)
    ]

    # ==========================================
    # 📝 页面 3 & 4: 规则列表 与 规则编辑器
    # ==========================================
    
    # --- 编辑器表单 UI ---
    edit_rule_index = [-1] # 用列表包装以便在函数内修改
    
    # 表单控件
    input_rule_name = ft.TextField(label="规则名称", variant=ft.TextFieldVariant.OUTLINE)
    dropdown_trigger = ft.Dropdown(
        label="选择触发器事件",
        options=[ft.dropdown.Option(pid) for pid in data_manager.plugins["triggers"].keys()],
        variant=ft.TextFieldVariant.OUTLINE
    )
    # 为了演示简单，把 params 用一个多行文本框装 JSON 字符串
    input_trigger_params = ft.TextField(
        label="触发器参数 (JSON格式)", 
        multiline=True, min_lines=3, 
        variant=ft.TextFieldVariant.OUTLINE
    )

    def save_rule_click(e):
        """保存规则的逻辑"""
        # 验证必填字段
        if not input_rule_name.value.strip():
            page.snack_bar = ft.SnackBar(ft.Text("规则名称不能为空！"), bgcolor=ft.Colors.ERROR)
            page.snack_bar.open = True
            page.update()
            return
        
        if not dropdown_trigger.value:
            page.snack_bar = ft.SnackBar(ft.Text("请选择触发器事件！"), bgcolor=ft.Colors.ERROR)
            page.snack_bar.open = True
            page.update()
            return
            
        try:
            params_dict = json.loads(input_trigger_params.value) if input_trigger_params.value.strip() else {}
        except Exception:
            page.snack_bar = ft.SnackBar(ft.Text("触发器参数 JSON 格式有误哦！"), bgcolor=ft.Colors.ERROR)
            page.snack_bar.open = True
            page.update()
            return
            
        new_rule = {
            "name": input_rule_name.value.strip(),
            "event": {
                "type": dropdown_trigger.value,
                "params": params_dict
            },
            "actions": data_manager.config["rules"][edit_rule_index[0]].get("actions", []) if edit_rule_index[0] != -1 else []
        }

        if edit_rule_index[0] == -1:
            data_manager.config["rules"].append(new_rule)
            # 通过本地通信通知
            com_bridge.create_rule(new_rule)
            # 如果已连接到后台，也通过 IPC 发送
            if connection_status["connected"]:
                remote.create_rule(new_rule)
        else:
            data_manager.config["rules"][edit_rule_index[0]] = new_rule
            # 通过本地通信通知
            com_bridge.update_rule(str(edit_rule_index[0]), new_rule)
            # 如果已连接到后台，也通过 IPC 发送
            if connection_status["connected"]:
                remote.update_rule(str(edit_rule_index[0]), new_rule)
            
        if data_manager.save_config_to_disk():
            page.snack_bar = ft.SnackBar(ft.Text("规则已成功保存到 config.json！✨"))
            page.snack_bar.open = True
            refresh_rules_list()
            # 切换回规则列表视图
            main_content.content = rules_content
            page.update()

    def cancel_edit_click(e):
        main_content.content = rules_content
        page.update()

    # 编辑器组装 (标准 MD3 表单排版)
    rule_editor_content.controls = [
        ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, on_click=cancel_edit_click),
            ft.Text("编辑规则", size=24, weight=ft.FontWeight.BOLD)
        ]),
        ft.Container(height=16),
        input_rule_name,
        dropdown_trigger,
        input_trigger_params,
        ft.Container(height=24),
        ft.Row([
            ft.TextButton("取消", on_click=cancel_edit_click),
            ft.FilledButton("保存规则", icon=ft.Icons.SAVE, on_click=save_rule_click)
        ], alignment=ft.MainAxisAlignment.END)
    ]

    # --- 规则列表 UI ---
    rules_list_view = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True)

    def open_editor(idx=-1):
        """打开编辑器。idx=-1表示新建，否则表示编辑第idx条"""
        edit_rule_index[0] = idx
        if idx == -1:
            input_rule_name.value = ""
            dropdown_trigger.value = list(data_manager.plugins["triggers"].keys())[0] if data_manager.plugins["triggers"] else ""
            input_trigger_params.value = "{}"
        else:
            rule = data_manager.config["rules"][idx]
            input_rule_name.value = rule.get("name", "")
            dropdown_trigger.value = rule.get("event", {}).get("type", "")
            input_trigger_params.value = json.dumps(rule.get("event", {}).get("params", {}), ensure_ascii=False, indent=2)
        
        # 将主画板切换为编辑器
        main_content.content = rule_editor_content
        page.update()

    def delete_rule(idx):
        del data_manager.config["rules"][idx]
        data_manager.save_config_to_disk()
        # 通过本地通信通知
        com_bridge.delete_rule(str(idx))
        # 如果已连接到后台，也通过 IPC 发送
        if connection_status["connected"]:
            remote.delete_rule(str(idx))
        refresh_rules_list()
        page.update()

    def refresh_rules_list():
        rules_list_view.controls.clear()
        for idx, rule in enumerate(data_manager.config.get("rules", [])):
            rule_name = rule.get("name", f"未命名规则 {idx+1}")
            event_type = rule.get("event", {}).get("type", "未知触发器")
            
            # 用 ListTile 打造完美的 Material 规则项
            rules_list_view.controls.append(
                ft.Card(
                    variant=ft.CardVariant.OUTLINE,
                    margin=ft.Margin(0, 0, 0, 12),
                    content=ft.ListTile(
                        leading=ft.Icon(ft.Icons.RULE, color=ft.Colors.PRIMARY),
                        title=ft.Text(rule_name, weight=ft.FontWeight.BOLD),
                        subtitle=ft.Text(f"监听事件: {event_type}"),
                        trailing=ft.Row([
                            ft.IconButton(ft.Icons.EDIT, tooltip="编辑", on_click=lambda e, i=idx: open_editor(i)),
                            ft.IconButton(ft.Icons.DELETE, tooltip="删除", icon_color=ft.Colors.ERROR, on_click=lambda e, i=idx: delete_rule(i))
                        ], tight=True) # tight=True 让按钮紧凑排列
                    )
                )
            )

    # 初始化渲染规则列表
    refresh_rules_list()

    rules_content.controls = [
        ft.Row([
            ft.Text("自动化规则", size=32, weight=ft.FontWeight.BOLD),
            # MD3 的悬浮操作按钮 (FAB) 感觉的添加按钮
            ft.FilledButton("新建规则", icon=ft.Icons.ADD, on_click=lambda e: open_editor(-1))
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Container(height=16),
        rules_list_view
    ]


    # ==========================================
    # 🪄 侧边栏与页面调度容器
    # ==========================================
    main_content = ft.Container(content=dashboard_content, expand=True, padding=40)

    def on_nav_change(e):
        idx = e.control.selected_index
        if idx == 0:
            main_content.content = dashboard_content
        elif idx == 1:
            main_content.content = rules_content
        elif idx == 2:
            main_content.content = plugins_content
        page.update()

    # MD3 NavigationRail
    rail = ft.NavigationRail(
        selected_index=0, 
        label_type=ft.NavigationRailLabelType.ALL, 
        min_width=100, 
        group_alignment=-0.9,
        destinations=[
            # 使用标准的 Material Icons！
            ft.NavigationRailDestination(icon=ft.Icons.DASHBOARD_OUTLINE, selected_icon=ft.Icons.DASHBOARD, label="遥控器"),
            ft.NavigationRailDestination(icon=ft.Icons.LIST_ALT_OUTLINE, selected_icon=ft.Icons.LIST_ALT, label="规则管理"),
            ft.NavigationRailDestination(icon=ft.Icons.EXTENSION_OUTLINE, selected_icon=ft.Icons.EXTENSION, label="插件库"),
        ],
        on_change=on_nav_change,
    )

    page.add(ft.Row([rail, ft.VerticalDivider(width=1, color=ft.Colors.OUTLINE_VARIANT), main_content], expand=True))

if __name__ == '__main__':
    ft.run(main)