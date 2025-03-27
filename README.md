

# NotmyFault

**NotmyFault** 是一款用于监控特定进程并自动调整系统音量的 Windows 工具，同时通过 Windows Toast 通知实时反馈进程状态。 
其初衷为解决某些尴尬的英语听力问题。。。。。。
开源，采用 GPL-3 许可协议

## 功能简介

- **进程监控**：自动扫描指定进程（如微信、PowerPoint、Windows Media Player）的运行状态。
- **音量控制**：当指定进程启动时，根据配置自动设置系统音量（最大、最小或中等）。
- **通知提示**：进程状态变化时，弹出 Windows Toast 通知提示用户。
- **配置管理**：提供图形化配置编辑器，可自定义监控的进程和相关操作。
- **检测脚本状态显示**：在主界面实时显示检测主脚本（NOTMYFAULT.pyw）的运行状态，并可通过按钮启动/重启或关闭检测。

## 目录结构

```
NotmyFault/
├── NOTMYFAULT.pyw         # 进程检测主脚本（无窗口运行）
├── main.py                # 欢迎界面和配置编辑器入口（带 GUI 界面）
├── app_icon.ico           # 应用图标（欢迎界面左侧大图标）
├── README.md              # 项目说明文档
└── (其他依赖文件)
```

## 安装依赖

本项目基于 Python 开发，需要以下依赖库：

- [psutil](https://pypi.org/project/psutil/)
- [PyQt5](https://pypi.org/project/PyQt5/)
- [pywin32](https://pypi.org/project/pywin32/)
- [windows_toasts](https://pypi.org/project/windows-toasts/)

你可以使用 pip 安装：

```bash
pip install psutil PyQt5 pywin32 windows-toasts
```

## 使用方法

1. **配置文件存储**  
   默认配置文件存储在当前用户的 AppData/Roaming 目录下：  
   `%APPDATA%\NotmyFault\config.json`  
   如果配置文件不存在，程序会自动创建并写入默认设置。

2. **启动程序**  
   - **GUI 界面**：运行 `main.py`（或者编译为可执行文件）启动欢迎页面。  
     在欢迎页面中，你可以：
   - 查看检测主脚本的运行状态（例如 “检测运行中 (pythonw.exe running)”）。
   - 通过 “启动检测” 按钮启动或重启检测主脚本。
   - 通过 “关闭检测” 按钮终止检测主脚本。
   - 点击 “打开编辑器” 进入配置编辑器，对监控进程及相关操作进行修改。

3. **检测主脚本**  
   进程检测主脚本位于同一目录下的 `NOTMYFAULT.pyw`，用于后台无窗口运行检测目标进程并执行音量调节与通知提示。  
   欢迎界面提供了实时监控该脚本状态的功能，并支持启动/重启及关闭操作。

## 系统要求

- Windows 系统（由于依赖 win32 API 和 Windows Toast 通知）。
- 安装了 OPPO Sans 字体（可根据需要修改为其他字体）。

## 开发与贡献

如果你有任何问题、建议或希望贡献代码，欢迎提交 Issue 或 Pull Request。

## 许可证

本项目采用 **GPL-3** 开源许可协议。在完善后，完整的许可证文本将包含在项目中

祝你有美好的一天~
