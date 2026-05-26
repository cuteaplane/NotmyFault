# NotmyFault

**NotmyFault** 是一款面向 Windows 的草台班子自动化工具。

本项目的特色是：

- 支持插件化的触发器与执行器！只要会 Python ，你可以以极高的自由度轻松自己写你所需要的触发器和执行器！

---

## 特性

- **规则驱动**：以 `rules` 方式定义触发条件和动作。规则可包含多个动作。
- **进程检测触发器**：当前内置 `process_state` 触发器，可监听进程启动/停止。
- **动作执行器**：内置 `set_volume` 和 `notify`，可扩展更多动作插件。
- **图形化配置**：通过 `ControlCenter.pyw` 提供桌面配置编辑与启动界面。
- **统一配置路径**：默认配置存储在 `%APPDATA%\NotmyFault\config.json`。

---

## 目录结构

```text
NotmyFault/
├── ControlCenter.pyw           # GUI 控制中心入口
├── dashboard.html              # GUI 界面模板
├── NOTMYFAULT.pyw              # 后台检测引擎入口
├── README.md                   # 项目说明文档
├── notmyfault/                 # 核心引擎代码
│   ├── __init__.py
│   ├── __main__.py
│   ├── app.py
│   ├── config.py
│   ├── engine.py
│   ├── monitor.py
│   ├── volume.py
│   ├── actions/
│   │   ├── notify/
│   │   │   ├── action.json
│   │   │   └── action.py
│   │   └── set_volume/
│   │       ├── action.json
│   │       └── action.py
│   └── triggers/
│       └── process_state/
│           ├── trigger.json
│           └── trigger.py
├── Win_toaster/                # Windows 通知相关支持库
└── config.json                 # 项目默认模板配置（仅用于项目初始化）
```

---

## 安装依赖

推荐使用 Python 3.11+。

```bash
pip install psutil pywebview pywin32
```

> 需要确保 `pywebview` 能正常运行来使用 Web UI ；如果只运行命令行引擎，则 `pywebview` 不是必须依赖。

> 内置的触发器和执行器需要其他依赖:

```bash
pip install windows-toasts pycaw 
# 可能需要更多依赖。
```


---

## 运行方式

### 1. 启动控制中心（推荐）

```bash
python ControlCenter.pyw
```

点击界面上的“启动 NotmyFault”按钮即可启动后台引擎；也可以在界面中编辑规则并保存。

### 2. 直接运行后台引擎

```bash
python NOTMYFAULT.pyw
```

### 3. 通过包入口运行

```bash
python -m notmyfault
```

---

## 配置说明

配置文件位于：

```text
%APPDATA%\NotmyFault\config.json
```

如果该文件不存在，程序会自动创建默认配置。

### 配置格式

配置文件为 JSON 格式，主要由 `rules` 列表组成：

```json(example)
{
  "rules": [
    {
      "name": "微信音量规则",
      "event": {
        "type": "process_state",
        "params": {
          "process_name": "WeChat.exe",
          "state": "running"
        }
      },
      "actions": [
        {"type": "set_volume", "params": {"action": "max"}},
        {"type": "notify", "params": {"title": "微信正在运行", "message": "音量已设置为100%"}}
      ]
    }
  ]
}
```


## 插件结构

### 触发器

每个触发器位于 `notmyfault/triggers/<id>/`，包含：

- `trigger.json`：插件元数据
- `trigger.py`：实际运行逻辑

### 执行器

每个执行器位于 `notmyfault/actions/<id>/`，包含：

- `action.json`：插件元数据
- `action.py`：执行实现

这使得你可以按同样格式扩展更多触发器和动作。

---

## 开发提示

- 入口函数在 `notmyfault/app.py`
- 配置读取与自动迁移在 `notmyfault/config.py`
- 事件广播与规则匹配在 `notmyfault/engine.py`

---

## 许可证

本项目采用 **GPL-3** 许可协议。欢迎阅读并遵守 GPL 的相关条款。

