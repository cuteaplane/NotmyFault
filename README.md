# NotmyFault

**NotmyFault** 是一款面向 Windows 的草台班子自动化工具。

---

## 特性

- **规则驱动**：每条规则 = 一个触发条件 + 多个动作，简单直白
- **插件化**：触发器和动作各自独立，会 Python 就能自己写
- **HTTP API**：后台暴露 REST 接口，SSE 实时推送事件，任意浏览器打开 Dashboard 就能管理
- **双进程架构**：引擎后台常驻，UI 只是管理面板，关了也不影响规则运行
- **日志写盘**：所有输出自动写入 `engine.log`，出问题好排查

---

## 快速开始

### 1. 安装依赖

Python 3.11+ 推荐。

```bash
pip install fastapi uvicorn psutil pywin32
```

部分插件需要额外依赖：

```bash
pip install windows-toasts pycaw
```

### 2. 启动后台引擎

```bash
python NOTMYFAULT.pyw
```

引擎启动后会在 `http://127.0.0.1:19198` 监听 HTTP 请求，日志写入 `%APPDATA%\NotmyFault\engine.log`。

### 3. 打开管理面板

浏览器打开 `dashboard.html` 即可配置规则、启停引擎、查看实时事件。

或者直接调 API：

```bash
curl http://127.0.0.1:19198/api/engine/status
curl http://127.0.0.1:19198/api/rules
# 在线 API 文档：http://127.0.0.1:19198/docs
```

---

## API 一览

| Method | Path | 说明 |
|---|---|---|
| `POST` | `/api/engine/start` | 启动引擎 |
| `POST` | `/api/engine/stop` | 停止引擎 |
| `GET` | `/api/engine/status` | 引擎状态 |
| `GET` | `/api/rules` | 获取所有规则 |
| `PUT` | `/api/rules` | 保存规则 |
| `GET` | `/api/plugins` | 插件列表（含参数定义） |
| `GET` | `/api/events` | SSE 事件流（实时推送） |

启动引擎后访问 `http://127.0.0.1:19198/docs` 可查看 Swagger 交互式文档。

---

## 配置格式

配置文件位于 `%APPDATA%\NotmyFault\config.json`，首次运行自动生成默认配置。

```json
{
  "rules": [
    {
      "name": "微信音量规则",
      "event": {
        "type": "process_state",
        "params": { "process_name": "WeChat.exe", "state": "running" }
      },
      "actions": [
        { "type": "set_volume", "params": { "action": "max" } },
        { "type": "notify", "params": { "title": "微信正在运行", "message": "音量已设为100%" } }
      ]
    }
  ]
}
```

---

## 目录结构

```text
NotmyFault/
├── NOTMYFAULT.pyw              # 后台引擎入口
├── dashboard.html              # Web 管理面板
├── README.md
├── notmyfault/                 # 核心代码
│   ├── api_server.py           # HTTP API + SSE 事件流
│   ├── app.py                  # 引擎工厂入口
│   ├── engine.py               # 规则匹配与动作分发
│   ├── config.py               # 配置读写与迁移
│   ├── volume.py               # 系统音量控制
│   ├── monitor.py              # 进程监控工具
│   ├── triggers/               # 触发器插件
│   │   ├── process_state/      #   进程状态检测
│   │   ├── idle_detect/        #   系统空闲检测
│   │   ├── time_schedule/      #   定时触发
│   │   ├── usb_insert/         #   U盘插入检测
│   │   └── window_title/       #   窗口标题检测
│   └── actions/                # 动作插件
│       ├── set_volume/         #   设置系统音量
│       ├── notify/             #   显示 Windows 通知
│       ├── launch_program/     #   启动程序
│       ├── kill_process/       #   终止进程
│       ├── run_powershell/     #   执行 PowerShell
│       └── lock_screen/        #   锁定屏幕
├── Win_toaster/                # Windows 通知 AUMID 注册
└── tests/                      # 测试
```

---

## 插件开发

### 触发器

在 `notmyfault/triggers/<id>/` 下放两个文件：

- `trigger.json` — 元数据
- `trigger.py` — 必须导出 `run(meta, config_list, emit_event)` 函数

```json
{
  "id": "my_trigger",
  "name": "我的触发器",
  "semantic": "state",
  "params": [
    { "name": "keyword", "label": "关键词", "type": "string", "default": "" }
  ]
}
```

`semantic` 描述事件的语义类型：
- `"state"` — 持续状态上报（如进程运行/停止、窗口开关），事件携带状态值
- `"oneshot"` — 单次触发（如定时到时、USB插入），事件只表示"发生了"

### 动作

在 `notmyfault/actions/<id>/` 下放两个文件：

- `action.json` — 元数据
- `action.py` — 必须导出 `run(meta, params)` 函数

```json
{
  "id": "my_action",
  "name": "我的动作",
  "params": [
    { "name": "message", "label": "消息", "type": "string", "default": "" }
  ]
}
```

---

## 架构

```
浏览器 / pywebview  ←──fetch()──→  http://127.0.0.1:19198/api/*
                    ←──SSE──────  http://127.0.0.1:19198/api/events

引擎进程 (NOTMYFAULT.pyw)
  ├── FastAPI + uvicorn (HTTP 服务)
  ├── EngineAPI (路由 + SSE 事件队列)
  └── AutomationEngine (规则引擎线程)
```

双进程：引擎独立运行，UI 随时开关。通信全程 HTTP，`curl` 直接调试。

---

## 许可证

GPL-3
