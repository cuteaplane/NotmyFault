import sys
import os
import json
import psutil
import subprocess
import time
import ctypes
from PyQt5.QtGui import QPixmap, QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QHBoxLayout, QVBoxLayout,
    QPushButton, QDialog, QLineEdit, QComboBox, QFormLayout, QListWidget,
    QMessageBox
)
from PyQt5.QtCore import Qt, QTimer

# ----------------- 配置文件路径相关函数 -----------------
def get_config_file():
    
    # 获取配置文件的完整路径，存放在当前用户的 AppData/Roaming 目录下。
    # 具体路径为：%APPDATA%\NotmyFault\config.json
    # 如果 NotmyFault 目录不存在，则自动创建。
    
    appdata = os.getenv("APPDATA")
    config_dir = os.path.join(appdata, "NotmyFault")
    if not os.path.exists(config_dir):
        os.makedirs(config_dir)
    return os.path.join(config_dir, "config.json")

CONFIG_FILE = get_config_file()

# ----------------- 默认配置 -----------------
DEFAULT_CONFIG = {
    "processes": [
        {
            "process_name": "WeChat",
            "software_name": "微信",
            "volume_action": "max",
            "notification": {
                "title": "微信正在运行",
                "message": "音量已设置为100%"
            }
        },
        {
            "process_name": "POWERPNT",
            "software_name": "PowerPoint",
            "volume_action": "max",
            "notification": {
                "title": "PowerPoint正在运行",
                "message": "音量已设置为100%"
            }
        },
        {
            "process_name": "wmplayer",
            "software_name": "Windows Media Player",
            "volume_action": "max",
            "notification": {
                "title": "Windows Media Player运行中",
                "message": "音量已设置为100%"
            }
        }
    ]
}

# ----------------- 检测进程相关函数 -----------------
def is_detection_running():
    
    # 检查是否有进程在运行，并且其命令行中包含 "NOTMYFAULT.pyw" 字符串，
    # 用以判断检测主脚本是否已经启动。
    
    for proc in psutil.process_iter(['cmdline']):
        try:
            cmdline = proc.info.get('cmdline', [])
            if any("NOTMYFAULT.pyw" in part for part in cmdline):
                return True
        except Exception:
            continue
    return False

def start_detection():

    # 启动检测主脚本“NOTMYFAULT.pyw”，调用 pythonw.exe 运行该脚本。
    # 注意：请确保 NOTMYFAULT.pyw 与本程序在同一目录下。

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "NOTMYFAULT.pyw")
    # 启动检测脚本，使用 pythonw.exe 运行以保证无窗口
    subprocess.Popen(["pythonw.exe", script_path])
    print("[DEBUG] Detection started.")

def stop_detection():
    """
    查找所有运行中且命令行中包含“NOTMYFAULT.pyw”的进程并终止它们。
    """
    for proc in psutil.process_iter(['pid', 'cmdline']):
        try:
            cmdline = proc.info.get('cmdline', [])
            if any("NOTMYFAULT.pyw" in part for part in cmdline):
                proc.kill()
                print(f"[DEBUG] Killed process {proc.pid} running NOTMYFAULT.pyw")
        except Exception:
            continue

# ----------------- 配置加载与编辑器 -----------------
class ConfigDialog(QDialog):
    """配置项编辑对话框"""
    def __init__(self, parent=None, data=None):
        super().__init__(parent)
        self.setWindowTitle("进程配置")
        self.setFixedSize(500, 400)
        
        self.process_name = QLineEdit()
        self.software_name = QLineEdit()
        self.volume_action = QComboBox()
        self.volume_action.addItems(["max", "half", "min"])
        self.notification_title = QLineEdit()
        self.notification_message = QLineEdit()

        form_layout = QFormLayout()
        form_layout.addRow("进程名称*:", self.process_name)
        form_layout.addRow("显示名称*:", self.software_name)
        form_layout.addRow("音量操作:", self.volume_action)
        form_layout.addRow("通知标题*:", self.notification_title)
        form_layout.addRow("通知内容*:", self.notification_message)

        btn_layout = QHBoxLayout()
        self.save_btn = QPushButton("保存")
        self.cancel_btn = QPushButton("取消")
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.cancel_btn)

        main_layout = QVBoxLayout()
        main_layout.addLayout(form_layout)
        main_layout.addLayout(btn_layout)
        self.setLayout(main_layout)

        if data:
            self.load_data(data)

        self.save_btn.clicked.connect(self.validate_and_accept)
        self.cancel_btn.clicked.connect(self.reject)

    def load_data(self, data):
        self.process_name.setText(data.get('process_name', ''))
        self.software_name.setText(data.get('software_name', ''))
        self.volume_action.setCurrentText(data.get('volume_action', 'max'))
        self.notification_title.setText(data.get('notification', {}).get('title', ''))
        self.notification_message.setText(data.get('notification', {}).get('message', ''))

    def get_data(self):
        return {
            "process_name": self.process_name.text().strip(),
            "software_name": self.software_name.text().strip(),
            "volume_action": self.volume_action.currentText(),
            "notification": {
                "title": self.notification_title.text().strip(),
                "message": self.notification_message.text().strip()
            }
        }

    def validate_and_accept(self):
        data = self.get_data()
        if not all([
            data['process_name'],
            data['software_name'],
            data['notification']['title'],
            data['notification']['message']
        ]):
            QMessageBox.warning(self, "输入错误", "所有带*号的字段必须填写")
            return
        self.accept()

class ConfigEditor(QMainWindow):
    """配置编辑器窗口"""
    def __init__(self, config_file=None):
        super().__init__()
        self.config_file = config_file if config_file else get_config_file()
        self.config_data = {"processes": []}
        self.init_ui()
        self.load_config()

    def init_ui(self):
        self.setWindowTitle("进程监控配置编辑器")
        self.setMinimumSize(700, 500)

        self.list_widget = QListWidget()
        self.add_btn = QPushButton("添加配置")
        self.edit_btn = QPushButton("编辑配置")
        self.delete_btn = QPushButton("删除配置")
        self.save_btn = QPushButton("保存配置")

        btn_layout = QVBoxLayout()
        btn_layout.addWidget(self.add_btn)
        btn_layout.addWidget(self.edit_btn)
        btn_layout.addWidget(self.delete_btn)
        btn_layout.addWidget(self.save_btn)
        btn_layout.addStretch()

        main_widget = QWidget()
        main_layout = QHBoxLayout()
        main_layout.addWidget(self.list_widget, 1)
        main_layout.addLayout(btn_layout)
        main_widget.setLayout(main_layout)
        self.setCentralWidget(main_widget)

        self.add_btn.clicked.connect(self.add_config)
        self.edit_btn.clicked.connect(self.edit_config)
        self.delete_btn.clicked.connect(self.delete_config)
        self.save_btn.clicked.connect(self.save_config)
        self.list_widget.itemDoubleClicked.connect(self.edit_config)

    def load_config(self):
        if not os.path.exists(self.config_file):
            self.config_data = DEFAULT_CONFIG
            try:
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    json.dump(self.config_data, f, ensure_ascii=False, indent=4)
            except Exception as e:
                QMessageBox.critical(self, "错误", f"创建配置文件失败:\n{str(e)}")
        else:
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    self.config_data = json.load(f)
            except Exception as e:
                QMessageBox.critical(self, "错误", f"加载配置文件失败:\n{str(e)}")
        self.update_list()

    def update_list(self):
        self.list_widget.clear()
        for item in self.config_data['processes']:
            text = f"{item['software_name']} ({item['process_name']})"
            self.list_widget.addItem(text)

    def add_config(self):
        dialog = ConfigDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            new_item = dialog.get_data()
            self.config_data['processes'].append(new_item)
            self.update_list()

    def edit_config(self):
        index = self.list_widget.currentRow()
        if index == -1:
            QMessageBox.warning(self, "提示", "请先选择一个配置项")
            return
        old_data = self.config_data['processes'][index]
        dialog = ConfigDialog(self, old_data)
        if dialog.exec_() == QDialog.Accepted:
            new_data = dialog.get_data()
            self.config_data['processes'][index] = new_data
            self.update_list()

    def delete_config(self):
        index = self.list_widget.currentRow()
        if index == -1:
            QMessageBox.warning(self, "提示", "请先选择一个配置项")
            return
        reply = QMessageBox.question(
            self, "确认删除", 
            "确定要删除这个配置项吗？",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            del self.config_data['processes'][index]
            self.update_list()

    def save_config(self):
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config_data, f, ensure_ascii=False, indent=4)
            QMessageBox.information(self, "成功", f"配置文件已保存到\n{self.config_file}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存失败:\n{str(e)}")

# -------------------------- 欢迎页面 --------------------------
class WelcomeWindow(QMainWindow):
    """欢迎页面，包含图标、标题、软件介绍、编辑器入口以及检测状态控制"""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NotmyFault 欢迎页面")
        self.setMinimumSize(800, 500)
        self.init_ui()
        # 启动定时器，周期性更新检测状态
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_detection_status)
        self.timer.start(2000)  # 每2秒更新一次

    def init_ui(self):
        main_widget = QWidget()
        main_layout = QHBoxLayout()
        main_widget.setLayout(main_layout)
        self.setCentralWidget(main_widget)

        # 左侧图标
        icon_label = QLabel()
        icon_path = "logo.png"  # 请确保该图标文件存在
        if os.path.exists(icon_path):
            pixmap = QPixmap(icon_path)
            pixmap = pixmap.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_label.setPixmap(pixmap)
        else:
            icon_label.setText("图标")
            icon_label.setAlignment(Qt.AlignCenter)
            icon_label.setFixedSize(200, 200)

        # 右侧内容
        right_widget = QWidget()
        right_layout = QVBoxLayout()
        right_widget.setLayout(right_layout)

        title_label = QLabel("NotmyFault")
        title_font = QFont("OPPO Sans", 20, QFont.Bold)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        intro_label = QLabel("NotmyFault 是一个用于监控指定进程，\n并执行自动化操作的小工具。")
        intro_font = QFont("OPPO Sans", 12)
        intro_label.setFont(intro_font)
        intro_label.setWordWrap(True)
        intro_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        # 编辑器入口按钮
        open_btn = QPushButton("打开编辑器")
        open_btn.setFixedWidth(150)
        open_btn.clicked.connect(self.open_editor)

        # 检测状态区域
        self.detectionStatusLabel = QLabel("运行状态：未知")
        self.detectionStatusLabel.setFont(QFont("OPPO Sans", 12))
        # 启动/重启检测按钮
        self.btnStart = QPushButton("启动")
        self.btnStart.setFixedWidth(120)
        self.btnStart.clicked.connect(self.handle_start_detection)
        # 关闭检测按钮
        self.btnStop = QPushButton("关闭")
        self.btnStop.setFixedWidth(120)
        self.btnStop.clicked.connect(self.handle_stop_detection)

        # 将检测状态和按钮放在一行
        detection_layout = QHBoxLayout()
        detection_layout.addWidget(self.detectionStatusLabel)
        detection_layout.addStretch()
        detection_layout.addWidget(self.btnStart)
        detection_layout.addWidget(self.btnStop)

        right_layout.addWidget(title_label)
        right_layout.addSpacing(20)
        right_layout.addWidget(intro_label)
        right_layout.addSpacing(20)
        right_layout.addLayout(detection_layout)
        right_layout.addStretch()
        right_layout.addWidget(open_btn, alignment=Qt.AlignRight)

        main_layout.addWidget(icon_label)
        main_layout.addWidget(right_widget, 1)

    def update_detection_status(self):
        """周期性更新检测主脚本的运行状态，并调整按钮文本"""
        running = is_detection_running()
        if running:
            self.detectionStatusLabel.setText("运行状态：运行中")
            self.btnStart.setText("重启")
        else:
            self.detectionStatusLabel.setText("运行状态：未启动")
            self.btnStart.setText("启动")

    def handle_start_detection(self):
        """启动或重启检测脚本"""
        start_detection()
        # 延时更新状态
        QTimer.singleShot(1000, self.update_detection_status)

    def handle_stop_detection(self):
        """关闭检测脚本"""
        stop_detection()
        QTimer.singleShot(1000, self.update_detection_status)

    def open_editor(self):
        """打开配置编辑器窗口"""
        self.editor = ConfigEditor()
        self.editor.show()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    # 全局字体设置为 OPPO Sans
    app.setFont(QFont("OPPO Sans", 10))
    app.setStyle("Fusion")
    style = """
    QMainWindow, QDialog {
        background-color: #ffffff;
        font-family: "OPPO Sans";
    }
    QPushButton {
        background-color: #0078d7;
        color: #ffffff;
        border: none;
        border-radius: 4px;
        padding: 6px 12px;
        font-family: "OPPO Sans";
    }
    QPushButton:hover {
        background-color: #005a9e;
    }
    QPushButton:pressed {
        background-color: #003f6f;
    }
    QLineEdit, QComboBox {
        border: 1px solid #cccccc;
        border-radius: 4px;
        padding: 4px;
        font-family: "OPPO Sans";
    }
    QListWidget {
        border: 1px solid #cccccc;
        border-radius: 4px;
        font-family: "OPPO Sans";
    }
    QMessageBox {
        background-color: #ffffff;
        font-family: "OPPO Sans";
    }
    """
    app.setStyleSheet(style)
    
    welcome = WelcomeWindow()
    welcome.show()
    sys.exit(app.exec_())
