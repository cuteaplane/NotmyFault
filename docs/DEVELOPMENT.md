# NotmyFault 开发文档（Alpha）

这里写当前可以使用和验证的开发接口。尚未实现的规划不放在这里。

## 1. 本地启动与验证

### 环境

- Windows / Linux
- Python 3.11+（当前开发环境可用 Python 3.14）
- Node.js 18+

安装当前常用依赖：

```powershell
pip install fastapi uvicorn psutil pywin32 py7zr openpyxl
cd dashboard
npm install
```

Linux 不需要 `pywin32`，Dashboard 可使用 `pywebview[qt]`。
推荐统一使用 `pip install -r requirements-dev.txt`，平台专属依赖由环境标记自动选择。

### 插件平台兼容性

默认的 `action.py` / `trigger.py` 入口视为跨平台。若插件只支持部分系统，
在清单中声明：

```json
{
  "platforms": ["windows"]
}
```

只有当不同系统的实现差异很大时才拆分入口：

```json
{
  "platforms": ["windows", "linux"],
  "entrypoints": {
    "windows": "windows/action.py",
    "linux": "linux/action.py"
  }
}
```

入口必须是插件目录内的相对 `.py` 路径。内置插件和用户插件使用同一套
平台选择、签名、权限扫描和完整性校验；当前平台没有入口时会在执行代码前跳过。

启动引擎：

```powershell
python NOTMYFAULT.pyw
```

开发 Dashboard：

```powershell
cd dashboard
npm run dev
```

提交前至少执行：

```powershell
python -m pytest notmyfault/tests -q
cd dashboard
npm test
npm exec vite build -- --emptyOutDir
```

## 2. 代码放哪里

| 位置 | 用途 |
| --- | --- |
| `notmyfault/core/` | 引擎核心：engine / rules / workflow / workflow_executor / bindings / trigger_supervisor / runtime_controller / diagnostics / logging |
| `notmyfault/security/` | 安全：security / sudo / signing / signing_keys / plugins / plugin_loader / plugin_schema |
| `notmyfault/host/` | 桌面端、托盘、HTTP 装配和 API 路由 |
| `notmyfault/host/api/` | 六组 HTTP 路由、认证、事件和对应业务对象 |
| `notmyfault/platform/` | 平台适配：platform_support / linux_support / portal_screenshot |
| `notmyfault/native/` | 原生调用安全原语（NATIVE_LOCK） |
| `notmyfault/actions/` | 内置动作插件 |
| `notmyfault/triggers/` | 内置触发器插件（含 `base.py` 轮询基类） |
| `dashboard/src/` | Dashboard 的 Vue 前端 |
| `notmyfault/tests/` | Python 测试 |

用户/第三方插件不存放在仓库内，部署位置是平台配置目录下的 `plugins/`（Windows：`%APPDATA%\NotmyFault\plugins\`）。

## 3. 规则现在怎么工作

一条规则分成三段：

```text
触发条件  →  执行前检查  →  动作流水线
```

### 触发条件

单事件仍可使用旧格式：

```json
{
  "event": {
    "binding_id": "t_schedule01",
    "type": "time_schedule",
    "params": {"time": "17:00"}
  }
}
```

多个条件使用 `condition`。`any` 是“满足任一项”，`all` 是“全部满足”；两者可以嵌套。

```json
{
  "condition": {
    "op": "any",
    "children": [
      {
        "binding_id": "t_schedule01",
        "type": "time_schedule",
        "params": {"time": "17:00"}
      },
      {
        "binding_id": "t_usb01",
        "type": "usb_insert",
        "params": {"drive_letter": "ANY"}
      }
    ]
  }
}
```

`all` 可以设置 `within_seconds`，表示这些事件必须在这段时间内都出现。

### 执行前检查

“Word 是否还在编辑”“文件是否还在写入”不是触发条件，而是动作开始前的安全检查。

```json
{
  "preconditions": [
    {
      "binding_id": "p_quiescent01",
      "type": "document_quiescent",
      "params": {
        "source_folder": "D:\\待归档",
        "quiet_seconds": 120,
        "check_file_locks": true,
        "check_document_windows": true
      }
    }
  ]
}
```

检查没通过时，引擎不会继续执行动作；它会稍后再检查。前置检查插件必须声明
`"precondition_api": "context-v1"`，并提供：

```python
def check_precondition(meta, params, context):
    return {"ok": False, "reason": "文件仍在变化", "retry_after_seconds": 60}
```

也可以只返回 `True` 或 `False`。

### 动作流水线和上一步结果

动作按顺序执行。每个动作使用稳定的 `binding_id`，后续动作通过结构化 `$ref`
引用前面的结果：

```json
{
  "actions": [
    {
      "binding_id": "a_append01",
      "type": "append_text",
      "params": {
        "file_path": "D:\\NotmyFault\\events.txt",
        "text": "记录一次事件",
        "add_timestamp": true,
        "encoding": "utf-8"
      }
    },
    {
      "binding_id": "a_notify01",
      "type": "notify",
      "params": {
        "title": "NotmyFault",
        "message": {
          "$ref": {
            "scope": "step",
            "node": "a_append01",
            "path": ["file"]
          }
        }
      }
    }
  ]
}
```

上例中，`append_text` 返回的 `file` 是字符串。完整 `$ref` 会保留原本的数据类型。
动作返回结果会写入：

```text
context.steps.<binding_id>.result
```

旧规则的 `{{ event.payload.x }}` 和 `{{ steps.action_1.result.value }}` 仍会解析，
但 Dashboard 不再生成新的字符串引用。完整格式见 `docs/rule-schema-v2.md`。

## 4. 写动作插件

新增插件前先查一下现有插件，避免重复造轮子；已有插件覆盖不了的需求
才值得新建。

动作插件目录至少包含：

```text
my_action/
  action.json
  action.py
```

旧动作 API：

```python
def run(meta, params):
    return {"ok": True}
```

需要读取事件、条件命中信息或前序步骤结果时，使用新 API：

```json
{
  "execution_api": "context-v1",
  "outputs": ["result_name"]
}
```

```python
def run_with_context(meta, params, context):
    event = context["event"]
    previous = context["steps"]
    return {"result_name": "value"}
```

`outputs` 用于让 Dashboard 显示可引用的结果名；真正的返回值必须是 JSON 可表示的数据。

插件声明的权限要与实际能力对应：网络用 `network`，插件目录外读写文件用 `filesystem`，Windows API 用 `native_api`。第三方云盘、邮件、IM 等服务一律做成用户插件，安装在平台配置目录的 `plugins/` 下，不进仓库。

打包用户插件：

```powershell
python nmf.py plugin check <插件目录路径>
python nmf.py plugin test <插件目录路径> -q
python nmf.py plugin pack <插件目录路径>
```

## 5. 写触发器

内置触发器使用 `event-v2`。每条规则配置独立运行，入口固定为：

```python
def run(meta, config, emit_event, shutdown_event):
    # config：当前规则配置的参数对象
    # emit_event({"key": "value"})：发出事件，Engine 已绑定 trigger ID
    # shutdown_event.is_set()：循环中及时退出
    ...
```

内置触发器的 `trigger.json` 必须声明：

```json
{"trigger_api": "event-v2"}
```

加载时会检查这四个参数是否齐全；不合格的触发器不会启动后台线程。已有
`event-v1` 和未声明 `trigger_api` 的用户触发器仍按旧方式兼容。

### 轮询型触发器用基类

轮询型触发器（clipboard / window_title / hotkey / power_state 等）应继承
`notmyfault/triggers/base.PollingTrigger`，统一"配置校验 → 原生段自动持锁 →
间隔轮询 → 退出清理"的骨架，避免手写 while 循环导致原生加锁纪律不一致。
详见 `docs/native-safety.md`。

### event-v2 怎么启动

- **每配置一个实例**：同一条触发器被 N 条规则使用时启动 N 个隔离线程，
  实例 ID 为 `trigger_id:序号`（只有一个配置时就是 `trigger_id`）。
- **相同配置去重**：多条规则使用完全相同的配置（如同一热键、同一监控目录）
  时只启动一个实例；事件仍按配置指纹匹配所有规则，避免重复执行。
- **配置指纹**：JSON 规范化后比较（整数值 90 与 90.0 视为相同）。事件命中
  = 实例配置指纹与规则叶子 `params` 指纹相等，payload 不参与命中判断。
- **无效配置抛异常**：缺失字段、非法枚举值（hotkey 为空、state 取值不在
  清单内、threshold 非数字等）一律抛 `ValueError`，引擎会标记该触发器崩溃
  并告警，而不是让线程空转、规则永远不触发。
- 触发器必须用 `shutdown_event` 轮询退出，并在退出时清理原生资源
  （如 power_state 的隐藏窗口）。

## 5.1 导入安全限制

`notmyfault` 包在 **strict** 安全模式下拒绝外部代码直接 `import notmyfault`
（pytest 与项目根目录下的官方脚本除外），防止第三方进程把引擎组件当库随意
加载。需要以库方式使用引擎时，请将安全模式设为 `normal`/`permissive`，或从
`NOTMYFAULT.pyw` 启动。

`notmyfault.security.sudo` 的导入守卫更严：只允许插件命名空间
（`notmyfault.action_*` / `notmyfault.trigger_*`）与引擎核心
（`notmyfault.core.engine`）导入，strict 模式下其他一切导入都会触发
`ImportError`。插件需要管理员权限时，请在元数据声明
`"permissions": ["admin"]` 并通过 `notmyfault.security.sudo.run_as_admin`
走受控通道。

## 6. 改代码时的检查项

- 旧格式 `event` / `trigger` 规则不能被新条件树改坏。
- 旧两参数动作 `run(meta, params)` 必须继续可用。
- 热加载和关闭时要取消已延后的工作流。
- 测试中模拟触发器崩溃时必须 mock 通知，不能真的向 Windows 通知中心刷错误消息。
- 改动插件元数据、规则格式或 Dashboard 表单时，同时补测试和兼容迁移。
- 原生调用必须声明 `ctypes argtypes/restype`；跨线程的原生段必须包
  `notmyfault.native.NATIVE_LOCK`；不可信的 COM/音频等原生代码放
  子进程隔离。详见 `docs/native-safety.md`。

## 7. API 后端

桌面后台从 `NOTMYFAULT.pyw` 启动。入口先创建 `ApplicationPaths` 和
`SignedConfigStore`，再把同一个存储实例交给 `EngineRunner`、
`create_engine()` 和 `create_api_server()`。引擎热重载使用这个实例读取
`rules.json`。API 不保存配置路径或规则路径的模块全局变量。

`notmyfault/host/api_server.py` 只装配应用并启动 Uvicorn。端点在六个
`routes_*.py` 中，HTTP 以外的处理在 `services/` 中。业务对象不导入
FastAPI 或 Starlette。

只改 API 后端时，可以先跑：

```powershell
python -m pytest notmyfault/tests/test_api_server.py notmyfault/tests/test_api_plugins.py notmyfault/tests/test_api_primitives.py notmyfault/tests/test_api_architecture.py notmyfault/tests/test_api_assembly_smoke.py -q
```

API 测试使用 `notmyfault/tests/api_support.py` 创建临时
`ApplicationPaths`、`SignedConfigStore` 和固定 token。`FakeRunner` 提供引擎
启停和状态；`FakeKeyStore` 保存测试密钥；`FakeDesktopElements` 返回固定的
桌面控件结果。文件替换失败测试传入 `PluginFileSystem` 的故障实现，预览过期
测试给 `PendingPreviewStore` 传入固定时钟。测试不修改 `api_server` 或
`config` 的模块属性。
