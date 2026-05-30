"""
NotmyFault 控制中心启动脚本（增强版）
集成 IPC 客户端，支持与后台引擎通信

特性：
- 启动 Flet UI 界面
- 自动连接到后台引擎
- 显示连接状态
- 关闭 UI 时不会关闭后台引擎

使用方法：
    python ControlCenter.pyw
    
或者直接运行此脚本
"""

import sys
import os

# 添加项目路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# 导入必要的模块
import flet as ft
from notmyfault.ipc_client import UIRemoteController
from notmyfault.communication_bus import Channels
from notmyfault.communication_bridge import setup_communication, cleanup_communication


class ControlCenterUI:
    """控制中心 UI 应用"""
    
    def __init__(self):
        self.remote = None
        self.page = None
        self.engine_status_indicator = None
        self.client_list = None
        self.log_display = None
    
    def run(self):
        """运行 UI 应用"""
        ft.run(main=self.main)
    
    def main(self, page: ft.Page):
        """主 UI 函数"""
        self.page = page
        
        # 初始化通信
        setup_communication()
        
        # 配置页面
        page.title = "NotmyFault 控制中心"
        page.window.width = 1200
        page.window.height = 800
        page.theme = ft.Theme(
            color_scheme_seed=ft.Colors.BLUE,
            use_material3=True,
            font_family="Segoe UI Variable Text, Microsoft YaHei UI, sans-serif"
        )
        page.bgcolor = ft.Colors.SURFACE
        
        # 页面关闭处理
        def on_page_close(e):
            cleanup_communication()
            if self.remote:
                self.remote.disconnect()
        
        page.on_close = on_page_close
        
        # 创建 UI 组件
        self._build_ui(page)
        
        # 初始化远程控制器
        self._init_remote_controller()
    
    def _build_ui(self, page: ft.Page):
        """构建 UI 界面"""
        
        # 标题栏
        title_bar = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.DASHBOARD, size=32, color=ft.Colors.PRIMARY),
                ft.Text("NotmyFault 控制中心", size=28, weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                ft.Container(
                    content=ft.Column([
                        ft.Text("连接状态", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                        self._create_status_indicator()
                    ]),
                    padding=ft.Padding(16, 0, 16, 0)
                )
            ], alignment=ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding(20, 16, 20, 16),
            border_radius=0,
            bgcolor=ft.Colors.SURFACE
        )
        
        # 主要内容区域（标签页）
        main_content = ft.Tabs(
            selected_index=0,
            tabs=[
                ft.Tab(
                    text="仪表盘",
                    icon=ft.Icons.DASHBOARD,
                    content=self._build_dashboard_tab()
                ),
                ft.Tab(
                    text="规则管理",
                    icon=ft.Icons.RULE,
                    content=self._build_rules_tab()
                ),
                ft.Tab(
                    text="系统日志",
                    icon=ft.Icons.DESCRIPTION,
                    content=self._build_logs_tab()
                ),
                ft.Tab(
                    text="连接信息",
                    icon=ft.Icons.INFO,
                    content=self._build_info_tab()
                )
            ]
        )
        
        # 页面布局
        page.add(
            ft.Column([
                title_bar,
                ft.Divider(height=1, color=ft.Colors.OUTLINE_VARIANT),
                main_content
            ], expand=True)
        )
    
    def _create_status_indicator(self) -> ft.Container:
        """创建连接状态指示器"""
        self.engine_status_indicator = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.CIRCLE, size=12, color=ft.Colors.ERROR),
                ft.Text("未连接", size=12, color=ft.Colors.ERROR)
            ], spacing=4),
            padding=ft.Padding(12, 8, 12, 8),
            bgcolor=ft.Colors.ERROR_CONTAINER,
            border_radius=8
        )
        return self.engine_status_indicator
    
    def _update_status_indicator(self, connected: bool, engine_running: bool = False):
        """更新状态指示器"""
        if self.engine_status_indicator and self.page:
            if connected and engine_running:
                status_icon = ft.Icons.CIRCLE
                status_color = ft.Colors.PRIMARY
                status_text = "引擎运行中"
                bg_color = ft.Colors.PRIMARY_CONTAINER
            elif connected:
                status_icon = ft.Icons.CIRCLE
                status_color = ft.Colors.WARNING
                status_text = "已连接（待启动）"
                bg_color = ft.Colors.WARNING_CONTAINER
            else:
                status_icon = ft.Icons.CIRCLE
                status_color = ft.Colors.ERROR
                status_text = "未连接"
                bg_color = ft.Colors.ERROR_CONTAINER
            
            self.engine_status_indicator.content = ft.Row([
                ft.Icon(status_icon, size=12, color=status_color),
                ft.Text(status_text, size=12, color=status_color)
            ], spacing=4)
            self.engine_status_indicator.bgcolor = bg_color
            
            self.page.update()
    
    def _build_dashboard_tab(self) -> ft.Container:
        """构建仪表盘标签页"""
        return ft.Container(
            content=ft.Column([
                ft.Text("后台引擎控制", size=24, weight=ft.FontWeight.BOLD),
                ft.Container(height=16),
                
                ft.Card(
                    variant=ft.CardVariant.ELEVATED,
                    content=ft.Container(
                        padding=24,
                        content=ft.Column([
                            ft.Text("引擎状态", size=18, weight=ft.FontWeight.W_600),
                            ft.Container(height=8),
                            ft.Text("等待连接中...", size=14, color=ft.Colors.ON_SURFACE_VARIANT),
                            ft.Container(height=24),
                            ft.Row([
                                ft.FilledButton(
                                    "启动引擎",
                                    icon=ft.Icons.PLAY_ARROW,
                                    on_click=self._on_start_engine
                                ),
                                ft.FilledButton(
                                    "停止引擎",
                                    icon=ft.Icons.STOP,
                                    on_click=self._on_stop_engine
                                ),
                                ft.FilledTonalButton(
                                    "刷新状态",
                                    icon=ft.Icons.REFRESH,
                                    on_click=self._on_refresh_status
                                )
                            ], spacing=12)
                        ])
                    )
                ),
                
                ft.Container(height=24),
                
                ft.Card(
                    variant=ft.CardVariant.OUTLINED,
                    content=ft.Container(
                        padding=24,
                        content=ft.Column([
                            ft.Text("连接信息", size=16, weight=ft.FontWeight.W_600),
                            ft.Container(height=12),
                            ft.Text("服务器地址: localhost:19198", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                            ft.Text("客户端 ID: 等待连接...", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
                        ])
                    )
                )
            ], scroll=ft.ScrollMode.AUTO, expand=True),
            padding=24
        )
    
    def _build_rules_tab(self) -> ft.Container:
        """构建规则管理标签页"""
        return ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Text("规则列表", size=24, weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    ft.FilledButton("新建规则", icon=ft.Icons.ADD)
                ]),
                ft.Container(height=16),
                ft.Text("连接到引擎后即可管理规则", size=14, color=ft.Colors.ON_SURFACE_VARIANT)
            ], expand=True),
            padding=24
        )
    
    def _build_logs_tab(self) -> ft.Container:
        """构建日志标签页"""
        self.log_display = ft.Text(
            "系统日志：等待连接后显示\n",
            size=11,
            font_family="Courier New",
            color=ft.Colors.ON_SURFACE
        )
        
        return ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Text("系统日志", size=24, weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    ft.IconButton(ft.Icons.DELETE, on_click=self._clear_logs)
                ]),
                ft.Container(height=12),
                ft.Container(
                    content=self.log_display,
                    bgcolor=ft.Colors.SURFACE_DIM,
                    padding=16,
                    border_radius=8,
                    expand=True
                )
            ], expand=True),
            padding=24
        )
    
    def _build_info_tab(self) -> ft.Container:
        """构建连接信息标签页"""
        return ft.Container(
            content=ft.Column([
                ft.Text("连接信息", size=24, weight=ft.FontWeight.BOLD),
                ft.Container(height=24),
                
                ft.Card(
                    variant=ft.CardVariant.OUTLINED,
                    content=ft.Container(
                        padding=24,
                        content=ft.Column([
                            self._make_info_row("服务器地址", "localhost:19198"),
                            ft.Divider(height=16),
                            self._make_info_row("客户端 ID", "等待连接..."),
                            ft.Divider(height=16),
                            self._make_info_row("连接状态", "未连接"),
                            ft.Divider(height=16),
                            self._make_info_row("通信协议", "IPC (Inter-Process Communication)"),
                            ft.Divider(height=16),
                            self._make_info_row("版本", "1.0 Enhanced with IPC")
                        ])
                    )
                )
            ], expand=True),
            padding=24
        )
    
    def _make_info_row(self, label: str, value: str) -> ft.Row:
        """创建信息行"""
        return ft.Row([
            ft.Text(label, size=14, width=150, weight=ft.FontWeight.W_600),
            ft.Text(value, size=14, color=ft.Colors.ON_SURFACE_VARIANT)
        ], spacing=16)
    
    def _init_remote_controller(self):
        """初始化远程控制器"""
        self.remote = UIRemoteController()
        
        # 注册事件回调
        self.remote.on(Channels.ENGINE_STATE_CHANGED, self._on_engine_state_changed)
        self.remote.on(Channels.RULE_TRIGGERED, self._on_rule_triggered)
        self.remote.on(Channels.ACTION_EXECUTED, self._on_action_executed)
        self.remote.on(Channels.ERROR_OCCURRED, self._on_error_occurred)
        
        # 尝试连接
        import threading
        threading.Thread(target=self._try_connect, daemon=True).start()
    
    def _try_connect(self):
        """尝试连接到后台引擎"""
        if self.remote.connect(timeout=10.0):
            self._update_status_indicator(True, False)
            self._log("✅ 已连接到后台引擎")
            self.remote.request_engine_status()
        else:
            self._update_status_indicator(False)
            self._log("❌ 无法连接到后台引擎，请确保已启动后台")
    
    def _on_start_engine(self, e):
        """启动引擎按钮处理"""
        if self.remote and self.remote.is_connected():
            self.remote.start_engine()
            self._log("🚀 已发送启动引擎命令")
        else:
            self._log("❌ 未连接到引擎")
    
    def _on_stop_engine(self, e):
        """停止引擎按钮处理"""
        if self.remote and self.remote.is_connected():
            self.remote.stop_engine()
            self._log("⏸️  已发送停止引擎命令")
        else:
            self._log("❌ 未连接到引擎")
    
    def _on_refresh_status(self, e):
        """刷新状态按钮处理"""
        if self.remote and self.remote.is_connected():
            self.remote.request_engine_status()
            self._log("🔄 已请求刷新状态")
        else:
            self._log("❌ 未连接到引擎")
    
    def _on_engine_state_changed(self, message):
        """引擎状态变化回调"""
        state = message.data.get("state", "unknown")
        is_running = state == "running"
        self._update_status_indicator(self.remote.is_connected(), is_running)
        self._log(f"🔄 引擎状态变化: {state}")
    
    def _on_rule_triggered(self, message):
        """规则触发回调"""
        rule_name = message.data.get("rule_name", "unknown")
        self._log(f"⚡ 规则被触发: {rule_name}")
    
    def _on_action_executed(self, message):
        """动作执行回调"""
        action_name = message.data.get("action_name", "unknown")
        result = message.data.get("result", "unknown")
        self._log(f"✅ 动作执行: {action_name} -> {result}")
    
    def _on_error_occurred(self, message):
        """错误发生回调"""
        error = message.data.get("error", "Unknown error")
        self._log(f"❌ 错误: {error}")
    
    def _clear_logs(self, e):
        """清除日志"""
        if self.log_display:
            self.log_display.value = "日志已清除\n"
            self.page.update()
    
    def _log(self, message: str):
        """输出日志"""
        if self.log_display and self.page:
            timestamp = __import__('datetime').datetime.now().strftime("%H:%M:%S")
            self.log_display.value += f"[{timestamp}] {message}\n"
            self.page.update()


def main():
    """主函数"""
    app = ControlCenterUI()
    app.run()


if __name__ == "__main__":
    main()
