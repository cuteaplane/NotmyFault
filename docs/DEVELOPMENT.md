# NotMyFault 开发文档（Alpha）

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

源码版 `dashboard.pyw` 会比较源码、字体、构建配置和版本源码的修改时间；
已有 `dist/index.html` 过期时重新运行 `npm run build`，构建失败不继续打开旧页面。
发行包直接使用随包产物。静态页面禁止缓存，文件名带 hash 的资产允许长期缓存。

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
| `notmyfault/core/` | 引擎核心：engine / rules / workflow / workflow_executor / bindings / trigger_host / runtime_controller / diagnostics / logging |
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

## 3. 规则结构

规则顶层字段见 `rule-schema-v2.md`，工作流为平面图：`workflow.nodes` 放全部节点，`workflow.edges` 放全部连线，`workflow.entry` 为 `null`。编译器从无入边的节点推导根节点，执行不依赖顶层动作数组。

### 身份

图节点 `binding_id` 以 `a_` 开头，触发器叶子 `trigger.binding_id` 以 `t_` 开头，符合 `^[ta]_[a-z0-9_]{6,64}$`。分支身份以 `b_` 开头，例如 `b_then0001`、`b_elif000001`、`b_else0001`。规则文件中身份必填且唯一，API 为缺失的新身份补值，保持已有身份和连线不变。

### 边

```json
{
  "source": "a_condition_000001",
  "port": "result",
  "target": "a_logic_000002",
  "target_port": "input",
  "channel": "condition"
}
```

`channel` 为 `condition` 或 `control`。`target_port` 省略时默认 `input`，连入 `event_group` 且 `group_type` 为 `if` 时必须指向分支 ID。

### 节点与端口

| 类型 | 字段 | 输出端口 |
| --- | --- | --- |
| `trigger` | `trigger.binding_id`/`type`/`params`、`occurrence` (`occurred`/`absent`)、`absence_scope` (`run`/`window`)、`duration_seconds`、`repeat` | `event` |
| `condition` | `predicate` 单一比较，`op` 如 `eq`/`ne`/`gt`/`lt`/`contains`/`run_status` | `result` |
| `logic` | `op: all/any/not`，输入全部来自条件边，`not` 只接受一条 | `result` |
| `event_group` | `group_type: if/all/any/not`、`branches: [{id, else?}]`、`mode: immediate/wait`、`timeout_seconds` 仅在 `wait` 时有效 | `if` 时输出分支 ID，其余输出 `result` |
| 插件动作 | `type` 为插件 ID，`params` 放参数，`retry`/`retry_delay_seconds`/`on_error` (`stop`/`continue`) 等 | `next` / `failure` |
| `action_group` | `exports: {name: $ref}`，用 `entry` 进入内部图 | `entry` 进组，`next` 返回，`failure` 不在该组类型上 |
| `end` / `merge` | 终止或汇合 | `end` 无出口，`merge` 输出 `next` |

组归属通过节点的 `parent` 指向 `action_group` 的 `binding_id` 保存，支持多层嵌套和折叠。`action_group` 的 `entry` 边指向组内首节点，组内图执行完后通过 `next` 边返回父图。`exports` 在组成功且非补救停止时计算，映射导出名到 `$ref` 表达式，供外部通过 `{"$ref": {"scope": "step", "node": "<group_id>", "path": [name]}}` 读取。

### 条件连接

多触发条件通过 AND/OR/NOT 图块和条件边组合，条件边来源是触发器的 `event` 端口或 `logic`/`condition`/`event_group` 的 `result` 端口，目标是 `logic` 或 `event_group` 的 IF/ELIF 端口。规则示例不保存 `op`/`children` 条件树，画布支持事件组嵌套和折叠。

```json
{
  "workflow": {
    "entry": null,
    "nodes": [
      {"binding_id": "a_trigger01", "type": "trigger", "trigger": {"binding_id": "t_trigger01", "type": "time_schedule", "params": {"time": "17:00"}}},
      {"binding_id": "a_append01", "type": "append_text", "params": {"file_path": "D:\\NotmyFault\\events.txt", "text": "记录一次事件", "add_timestamp": true, "encoding": "utf-8"}},
      {"binding_id": "a_notify01", "type": "notify", "params": {"title": "NotmyFault", "message": {"$ref": {"scope": "step", "node": "a_append01", "path": ["file"]}}}},
      {"binding_id": "a_end01", "type": "end"}
    ],
    "edges": [
      {"source": "a_trigger01", "port": "event", "target": "a_append01", "channel": "control"},
      {"source": "a_append01", "port": "next", "target": "a_notify01", "channel": "control"},
      {"source": "a_notify01", "port": "next", "target": "a_end01", "channel": "control"}
    ]
  }
}
```

上例中 `a_append01` 是真实动作 `append_text`，参数名以 `action.json` 为准，输出 `file` 为字符串。完整 `$ref` 保留原本的数据类型，结果写入 `context.steps.<binding_id>.result`，跨组可通过组导出引用：

```json
{"$ref": {"scope": "step", "node": "a_append01", "path": ["file"]}}
```

示例节点 ID 与本文一致。

### IF/ELIF/ELSE 与逻辑块

`event_group` 的 `group_type` 为 `if` 时，`branches` 保存稳定有序的分支身份 `b_`，第一个成立的分支胜出，`ELSE` 唯一且无输入。分支连线使用条件边：`source` 为逻辑/条件/触发器节点，`port` 为其输出端口，`target` 为事件组，`target_port` 为分支 ID。控制边使用 `port` 为分支 ID、`target` 为该分支首节点的图后继。

`logic` 的 `op` 为 `all`/`any`/`not`，通过条件边组合条件输入，`not` 只能连一条输入。`condition` 的 `predicate` 只包含一次比较。

### 入口持续未发生、流程内未发生和显式等待

区分三种未发生语义：

- 入口持续未发生：在规则入口路径上，`trigger` 设 `occurrence: absent`，配合 `duration_seconds` 和 `repeat: once_until_seen/every_window`。条件运行时在入队前通过定时器固定入口选择，持续未发生定时到达才触发。
- 流程内截至判定点未发生：`trigger` 在非入口位置或跟随控制前序，`occurrence: absent`。`absence_scope: run` 表示截至当前判定点未发生过该 `trigger.binding_id`，`absence_scope: window` 表示通过 `EventObservation.absent_for(binding_id, duration_seconds)` 判断窗口内持续未发生。
- 显式等待：`event_group` 的 `mode: wait`，`timeout_seconds` 仅在此模式下合法。执行到该组时返回 `RunSuspension`，并设置等待回调，收到通知时恢复执行。事件未发生不查询进程、窗口或文件的当前状态。

### 补救与运行结束状态

插件动作的 `failure` 控制出口连接同一执行范围中的普通 `action_group`，该组内可放动作、IF 和嵌套组。作为补救入口的动作组不同时接受主流程控制入口或对外 `next` 连线，由连线校验拒绝冲突。

- `on_error: stop`：补救动作组成功后结束运行，状态为 `recovered`，界面显示已补救，原动作步骤保留失败记录。
- `on_error: continue`：补救成功后返回原动作的正常 `next` 后继，流程随后完成则运行为 `succeeded`。
- 无补救路线且 `continue`：明确忽略本次错误，流程完成则记成功。
- 补救本身失败：停止并记失败，不自动返回主流程。
- 取消和执行超时直接结束，不进入补救图。插件执行超时表现为步骤 `timed_out`，触发器等待超时表现为 `event_wait_timeout`，均绕过补救。

结构与示例见 [规则格式](rule-schema-v2.md)。

## 4. 写动作插件

新增插件前先查一下现有插件，避免重复造轮子；已有插件覆盖不了的需求
才值得新建。

动作插件目录至少包含：

```text
my_action/
  action.json
  action.py
```

### 动作入口

插件清单声明 `execution_api: "context-v1"`。

```json
{
  "execution_api": "context-v1",
  "outputs": ["result_name"]
}
```

```python
def run_with_context(action_info, params, context):
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

触发器清单声明 trigger_api 为 event-v2，每个配置实例独立运行，入口固定为：

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

加载时检查上述四个参数；不合格的触发器不会启动后台线程。

### 轮询型触发器用基类

轮询型触发器（clipboard / window_title / hotkey / power_state 等）应继承
`notmyfault/triggers/base.PollingTrigger`，统一"配置校验 → 原生段自动持锁 →
间隔轮询 → 退出清理"的骨架。
详见 `docs/native-safety.md`。

### event-v2 怎么启动

- **每个不同配置一个实例**：同一触发器有 N 个不同配置时启动 N 个实例，
  实例 ID 为 `<触发器 id>:<配置指纹的 SHA-256 哈希>`。
- **相同配置去重**：多条规则使用完全相同的配置（如同一热键、同一监控目录）
  时只启动一个实例；事件仍按配置指纹匹配所有规则。
- **配置指纹**：JSON 规范化后比较（整数值 90 与 90.0 视为相同）。事件命中
  = 实例配置指纹与规则叶子 `params` 指纹相等，payload 不参与命中判断。
- **无效配置抛异常**：缺失字段、非法枚举值（hotkey 为空、state 取值不在
  清单内、threshold 非数字等）一律抛 `ValueError`，引擎会标记该触发器崩溃
  并告警。
- 触发器必须用 `shutdown_event` 轮询退出，并在退出时清理原生资源
  （如 power_state 的隐藏窗口）。

## 5.1 导入与管理员执行

`notmyfault` 包在 strict 模式下拒绝外部代码直接导入，允许真实 pytest 测试调用、
项目目录内脚本及 `python -m notmyfault` 入口。往 `sys.modules` 放入名为
`pytest` 的空模块不会获得测试豁免。包入口创建 `ApplicationPaths` 和
`SignedConfigStore`，再调用 Host 的 `run(store=...)`。

插件通过 `notmyfault.security.sudo.run_as_admin` 请求管理员执行，元数据需要
声明 `admin` 权限和 `security.admin_executables` 中的可执行文件名。检查发生在
调用时；`sudo` 模块没有单独的导入守卫。Windows 每次请求都需要通知确认和 UAC，
Linux 使用 pkexec。源码与运行进程属于当前用户，导入检查和权限扫描不是 Python
沙箱，也不提供同一用户进程之间的隔离。

`nmf.py plugin create trigger` 生成 event-v2 入口，每个实例接收单个 `config`，
等待 `shutdown_event` 后退出。插件清单可用字符串 `author` 填写作者。

安装、打包和完整性检查见 [插件 API](plugin-api-v1.md#插件包签名与安装)。

## 6. 改代码时的检查项

- 热加载和关闭时要取消已延后的工作流。
- 测试中模拟触发器崩溃时必须 mock 通知，不能真的向 Windows 通知中心刷错误消息。
- 改动插件元数据、规则格式或 Dashboard 表单时，同时补测试。
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
python -m pytest notmyfault/tests/test_api_server.py notmyfault/tests/test_api_plugins.py notmyfault/tests/test_api_primitives.py notmyfault/tests/test_api_assembly_smoke.py -q
```

API 测试使用 `notmyfault/tests/api_support.py` 创建临时
`ApplicationPaths`、`SignedConfigStore` 和固定 token。`FakeRunner` 提供引擎
启停和状态；`FakeKeyStore` 保存测试密钥。文件替换失败测试传入 `PluginFileSystem` 的故障实现，预览过期
测试给 `PendingPreviewStore` 传入固定时钟。测试不修改 `api_server` 或
`config` 的模块属性。

## 数据类型与运行变量

`notmyfault/core/data_types.py` 处理类型和值，`type_registry.py` 从已验证清单建立
类型快照，`value_codec.py` 统一保存和跨进程编码，`value_conversion.py` 处理
显式转换。`bindings.py` 解析表达式，`binding_schema.py` 检查来源类型和可用性，
`variables.py` 初始化常量与变量并执行赋值。
Dashboard 的 `valueTypes.js` 保留编码值并提供端口标签、编辑和类型提示。
类型与规则格式见 [data-types.md](data-types.md)。

## 8. Windows 安装升级恢复

升级在安装目录旁写入 `.<目录名>.upgrade-state`，记录原安装的备份路径和卸载注册表值。记录刷新到磁盘后，旧目录才会移动到 `.<目录名>.upgrade-<随机值>`。

升级进程中断后，重新运行安装包仍可选择原安装目录。目录检测会读取恢复记录，并在升级尚未提交时显示旧版本。开始安装时先恢复旧目录及原注册表值，再执行本次升级。

升级提交会先刷新记录中的提交状态，然后清理备份。如果进程在清理期间中断，下次只继续清理备份，已经安装的新版本会保留。恢复记录与备份处在安装目录的同一父目录，读取时检查目录与随机后缀格式。

安装失败日志会复制到临时目录。失败异常通过 `InstallFailure.LogPath` 提供本次日志，失败页面使用此路径。升级回滚恢复的旧 `install.log` 不作为本次失败日志展示。

卸载在删除每个文件和清理注册项前检查取消信号。目录已经移动时取消会保留清理清单，卸载未完成页可继续清理剩余文件。关闭窗口发起取消时，会等待当前文件操作结束再退出。

### 构建与检查

安装器构建在输出目录内创建临时 `work-*` 目录，完成或失败后清理该目录。构建日志保存在输出目录的 `work-*.log`，临时目录清理后仍可读取。Dashboard 从安装包暂存目录读取版本号和图标；安装包只保留编译后的 `dashboard/dist/`。

`installer/windows/tests/run_smoke.ps1` 使用 Windows PowerShell 5.1 可读取的 UTF-8 BOM 编码。测试失败时，`smoke.log` 保留标准输出和标准错误，脚本按测试进程的退出码报告失败。

`-MaintenanceOnly` 包含窗口样式初始化、快捷方式归属、首次启动参数、卸载器临时复制交接，以及升级和卸载恢复检查；不会释放完整离线安装内容。维护测试仍需要创建隔离的 HKCU 测试项。
