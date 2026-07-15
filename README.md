# NotmyFault

**NotmyFault** 是一款面向 Windows 的草台班子自动化工具。

---

## 特性

 - **规则驱动**：每条规则 = 一个触发条件 + 多个动作，简单直白
 - **插件化**：触发器和动作各自独立，会 Python 就能自己写
 - **HTTP API**：后台暴露 REST 接口，SSE 实时推送事件，任意浏览器打开 Dashboard 就能管理
 - **双进程架构**：引擎后台常驻，UI 只是管理面板，关了也不影响规则运行
 - **日志写盘**：session 级日志自动轮转，每行带时间戳，保留最近 7 个文件
 - **安全加固**：插件签名校验、权限声明 + 防越权、防重入、防注入
 - **规则条件**：支持 AND / OR 组合，匹配更灵活

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

引擎启动后会在 `http://127.0.0.1:19198` 监听 HTTP 请求，日志按 session 写入 `%APPDATA%\NotmyFault\logs\engine-YYYYMMDD-HHMMSS.log`，自动保留最近 7 个。

### 3. 打开管理面板

浏览器打开 `dashboard.html` 即可配置规则、启停引擎、查看实时事件。

 或者直接调 API：
 
 ```bash
 # 开发辅助：python build.py dev  启动监听模式，python build.py init-keys  生成签名密钥
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
 | `POST` | `/api/plugins/{id}` | 添加 / 更新单个插件 |
 | `DELETE` | `/api/plugins/{id}` | 删除单个插件 |
 | `GET` | `/api/events` | SSE 事件流（实时推送） |

启动引擎后访问 `http://127.0.0.1:19198/docs` 可查看 Swagger 交互式文档。

---

## 配置格式

 配置文件位于 `%APPDATA%\NotmyFault\config.json`，首次运行自动生成默认配置。
 条件支持 `"condition_mode": "or"`（默认 `"and"`），可组合多个触发条件。
 
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
 ├── build.py                   # 构建 / 签名 / 打包脚本
 ├── dashboard.html              # Web 管理面板
 ├── README.md
 ├── notmyfault/                 # 核心代码
 │   ├── api_server.py           # HTTP API + SSE 事件流
 │   ├── app.py                  # 引擎工厂入口
 │   ├── engine.py               # 规则匹配与动作分发
 │   ├── config.py               # 配置读写与迁移
 │   ├── logging.py              # session 日志系统
 │   ├── alert.py                # 引擎异常弹窗告警
 │   ├── plugin_schema.py        # 插件元数据 schema 校验
 │   ├── signing.py              # 插件签名校验
 │   ├── signing_keys.py         # 签名密钥生成
 │   ├── sudo.py                 # 管理员权限辅助模块
 │   ├── simulator/              # 触发器模拟测试环境
 │   │   ├── __init__.py
 │   │   ├── environment.py      #   模拟环境配置
 │   │   └── runner.py           #   模拟运行器
 │   ├── triggers/               # 触发器插件
 │   │   ├── bluetooth_device/   #   蓝牙设备检测
│   │   ├── process_state/      #   进程状态检测
│   │   ├── idle_detect/        #   系统空闲检测
│   │   ├── time_schedule/      #   定时触发
│   │   ├── usb_insert/         #   U盘插入检测
│   │   └── window_title/       #   窗口标题检测
│   └── actions/                # 动作插件
│       ├── bluetooth_toggle/   #   开关蓝牙
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

每个插件 = 一个目录，放在 `notmyfault/triggers/<id>/` 或 `notmyfault/actions/<id>/` 下，包含：

- `<type>.json` — 元数据（触发器和动作都适用以下 schema）
- `<type>.py` — 必须导出 `run()` 函数

### 元数据 schema

```json
{
  "id": "my_plugin",
  "name": "我的插件",
  "description": "插件功能描述",
  "version_code": 1,
  "enabled": true,
  "semantic": "state",
  "permissions": [],
  "params": [
    { "name": "keyword", "label": "关键词", "type": "string", "default": "" }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | ✅ | 唯一标识符，规则通过它引用插件 |
| `name` | string | ✅ | 显示名称 |
| `description` | string | ✅ | 功能描述 |
| `enabled` | bool | ✅ | 是否启用，`false` 则跳过加载 |
| `version_code` | int | ✅ | 版本号（递增整数），供后续插件管理使用 |
| `semantic` | string | — | 仅触发器：`"state"`（持续状态）或 `"oneshot"`（单次触发） |
| `permissions` | list | — | 权限声明，目前支持 `"admin"` |
| `params` | list | — | 参数定义，见下表 |

**params 条目字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | ✅ | 参数名 |
| `type` | string | ✅ | 类型：`string` / `number` / `select` / `bool` |
| `label` | string | ✅ | 参数显示名称 |
| `default` | any | — | 默认值 |
| `options` | list | — | 当 type=`select` 时的可选值列表 |
| `placeholder` | string | — | 输入框占位文本 |

### 生命周期钩子（可选）

插件模块可以导出以下函数：

| 函数 | 签名 | 调用时机 |
|------|------|----------|
| `setup(meta)` | 返回 `None` 或 `False`（`False` 中止加载） | 插件加载后 |
| `teardown()` | 无参数 | 引擎关闭时 |
| `validate_params(meta, params)` | 返回 `list[str]` 错误列表 | 动作执行前 |

### 权限声明

如果插件需要管理员权限（如操作蓝牙适配器、修改系统设置），应在元数据中声明：

```json
{ "permissions": ["admin"] }
```

并在代码中使用引擎提供的提权辅助模块，而不是自己拼 PowerShell：

```python
from notmyfault.sudo import run_as_admin
result = run_as_admin(["net", "start", "MyService"])
```

引擎加载时会检查：如果插件 import 了 `notmyfault.sudo` 但未声明 `admin` 权限，会打印警告。

### 触发器

必须导出 `run(meta, config_list, emit_event)` 函数。

`semantic` 描述事件的语义类型：
- `"state"` — 持续状态上报（如进程运行/停止、窗口开关），事件携带状态值
- `"oneshot"` — 单次触发（如定时到时、USB插入），事件只表示"发生了"

### 动作

必须导出 `run(meta, params)` 函数。

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
