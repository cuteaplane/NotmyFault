# NotmyFault 开发文档（Alpha）

> 面向参与开发的人。本文只记录当前可用的做法和下一步方向；代码变了就同步改本文。

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
npm exec vite build -- --emptyOutDir
```

## 2. 代码放哪里

| 位置 | 用途 |
| --- | --- |
| `notmyfault/engine.py` | 引擎生命周期、事件分发、动作流水线、热加载 |
| `notmyfault/rules.py` | 条件树、规则匹配、规则校验；尽量保持纯函数 |
| `notmyfault/workflow.py` | 动作上下文、`{{ ... }}` 参数引用、新旧动作 API 兼容 |
| `notmyfault/actions/` | 内置动作插件 |
| `notmyfault/triggers/` | 内置触发器插件 |
| `user_plugins/` | 用户/第三方插件源码；不能把第三方服务塞进内置插件 |
| `dashboard/src/` | Dashboard 的 Vue 前端 |
| `notmyfault/tests/` | Python 测试 |

## 3. 规则现在怎么工作

一条规则分成三段：

```text
触发条件  →  执行前检查  →  动作流水线
```

### 触发条件

单事件仍可使用旧格式：

```json
{
  "event": {"type": "time_schedule", "params": {"time": "17:00"}}
}
```

多个条件使用 `condition`。`any` 是“满足任一项”，`all` 是“全部满足”；两者可以嵌套。

```json
{
  "condition": {
    "op": "any",
    "children": [
      {"type": "time_schedule", "params": {"time": "17:00"}},
      {"type": "usb_insert", "params": {"drive_letter": "ANY"}}
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

检查没通过时，引擎不会上传半截文件；它会延后后再检查。前置检查插件必须声明
`"precondition_api": "context-v1"`，并提供：

```python
def check_precondition(meta, params, context):
    return {"ok": False, "reason": "文件仍在变化", "retry_after_seconds": 60}
```

也可以只返回 `True` 或 `False`。

### 动作流水线和上一步结果

动作按顺序执行。系统自动用“动作类型 + 第几个动作”命名步骤，后续动作可以引用它的返回值：

```json
{
  "actions": [
    {
      "type": "tianyi_drive_sync",
      "params": {"source_folder": "D:\\待归档"}
    },
    {
      "type": "excel_append_rows",
      "params": {
        "excel_path": "D:\\归档记录.xlsx",
        "records": "{{ steps.tianyi_drive_sync_1.result.uploaded_files }}"
      }
    }
  ]
}
```

例如第一步的类型是 `tianyi_drive_sync`，它就是 `tianyi_drive_sync_1`；同类型的第二步则是 `tianyi_drive_sync_2`。完整占位符会保留原本的数据类型：上例中的 `records` 会得到列表，不会变成一段字符串。

动作返回的结果会写入：

```text
context.steps.<动作类型>_<序号>.result
```

## 4. 写动作插件

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

插件声明的权限要与实际能力对应：网络用 `network`，插件目录外读写文件用 `filesystem`，Windows API 用 `native_api`。第三方云盘、邮件、IM 等服务一律做成 `user_plugins/` 下的用户插件。

打包用户插件：

```powershell
python pack_plugin.py user_plugins/my_action
```

## 5. 写触发器

内置触发器统一使用 `event-v1`。入口固定为：

```python
def run(meta, config_list, emit_event, shutdown_event):
    # config_list：所有使用这个触发器的规则参数
    # emit_event("my_trigger", {"key": "value"})：发出事件
    # shutdown_event.is_set()：循环中及时退出
    ...
```

内置触发器的 `trigger.json` 必须声明：

```json
{"trigger_api": "event-v1"}
```

加载时会检查这四个参数是否齐全；不合格的触发器不会启动后台线程。旧用户触发器没有声明 `trigger_api` 时仍按旧方式兼容。

## 6. 当前的归档示例

“每天 17:00 或插入 U 盘后，将文件夹中新文件上传并登记 Excel”的当前配置思路：

```text
时间触发 / U盘触发（任一）
  → 文档可安全归档检查
  → 天翼云盘增量上传（步骤 ID：upload）
  → Excel 追加记录（records 引用 upload 的 uploaded_files）
```

天翼插件只读取环境变量中保存的授权信息，规则文件中只写环境变量名。它目前是一次性上传实现：超过设定大小的文件不会假装支持断点续传。

## 7. 规则组：下一阶段要做什么

规则组还没有实现。目标不是“把规则放进一个文件夹”，而是让一个规则的结果能驱动另一个规则。

预期流程：

```text
规则 A 完成
  → 产生内部事件（成功、失败、跳过、延后等状态）
  → 规则 B 或规则组中的下一节点按状态继续
```

实现时必须解决四件事：

1. 每次运行都有独立运行编号，不能让昨天的规则 A 触发今天的规则 B。
2. 规则组是有向图，不允许保存循环依赖。
3. 要区分“动作失败”“前置检查延后”“用户取消”“超时”。
4. 组内步骤的重试、取消和日志必须能单独查看。

第一步会先把规则完成事件作为内部触发器接入现有条件树；然后再加规则组编辑器和运行记录。不要在现阶段用普通触发器拼凑假规则组。

## 8. 改代码时的底线

- 旧格式 `event` / `trigger` 规则不能被新条件树改坏。
- 旧两参数动作 `run(meta, params)` 必须继续可用。
- 热加载和关闭时要取消已延后的工作流。
- 测试中模拟触发器崩溃时必须 mock 通知，不能真的向 Windows 通知中心刷错误消息。
- 改动插件元数据、规则格式或 Dashboard 表单时，同时补测试和兼容迁移。
