# 插件 API v1

这里列出插件可以依赖的 v1 接口。没列出来的内部模块可能调整，插件不要直接
import。参数编辑器和插件页面另见 `plugin-extension-api.md`。

NotmyFault 插件 API 版本是整数，当前是 1（`notmyfault/plugin_api.py` 的
`HOST_API_VERSION`）。清单可以声明依赖：

```json
"engines": {"notmyfault_api": 1}
```

版本不同时，引擎会跳过插件并把原因写进诊断页。不声明则不检查版本。

## 清单（action.json / trigger.json）

公共必填：id、name、description、enabled、version_code、version、package_name。

常用可选字段：

| 字段 | 说明 |
| --- | --- |
| params | 参数定义，type 见 plugin_schema._ALLOWED_PARAM_TYPES |
| permissions | native_api / external_binary / admin 等 |
| platforms / entrypoints | 平台限制与分平台入口 |
| requires_capabilities | 依赖的系统能力，id 见 notmyfault/platform/capabilities.py |
| engines | NotmyFault 插件 API 版本声明 |
| contributes | commands / views / parameter_editors / data_types 四种贡献 |

action 专属：

| 字段 | 说明 |
| --- | --- |
| execution_api | 不写走 run()；写 "context-v1" 走 run_with_context() |
| precondition_api | 旧清单字段，运行前检查已移除，宿主不再调用 check_precondition() |
| cancellation_api | 写 "runtime-v1" 才允许规则配 timeout_seconds |
| idempotent | true 表示可安全重试，false 表示重复执行可能产生额外结果 |
| execution_mode | "isolated" 是实验字段，动作在子进程里跑（见下文） |

trigger 专属：trigger_api（event-v1 / event-v2）、semantic（state / oneshot）。

结构校验都在 `notmyfault/security/plugin_schema.py`，校验失败的原因会进诊断页。

内置动作显式声明 `idempotent`。同一插件包含切换、追加等操作时，按整个插件的
行为声明为 `false`。该字段用于重试提示，不阻止用户配置重试。触发器不使用此字段。

## 动作

### action legacy（稳定）

```python
def run(action_info, params):
    return {"ok": True}
```

返回值会进规则上下文，供后续步骤 $ref 引用。抛异常表示失败。
仓库例子：`notmyfault/actions/set_volume/action.py`。

引擎在调用动作前解析常量、变量与其他步骤引用。两种动作入口都接收解析后的
`params`，不需要在插件内再次解析表达式。参数和输出的 `value_type`、显式转换与
自定义类型使用方法见 `data-types.md`；插件通过 `notmyfault.plugin_api.data_types_api()`
访问类型校验和转换。

### execution_api=context-v1（稳定）

```python
def run_with_context(action_info, params, context):
    return {"ok": True}
```

context 是这次 run 独享的上下文，能读到 event payload、前面步骤的结果和
规则信息。结构见 `notmyfault/core/workflow.py` 的 build_context。
声明了 execution_api=context-v1 却没定义 run_with_context 会在执行时报
TypeError，加载期不拦。仓库例子：`notmyfault/actions/append_text/action.py`。

### 旧版 precondition_api（已移除）

清单校验仍接受 `precondition_api: "context-v1"`，宿主不再调用
`check_precondition()`，也不再根据 `retry_after_seconds` 延后工作流。
新插件无需声明此字段或实现此入口。

非空 `preconditions` 会被规则校验拒绝。旧规则必须移除该字段中的检查；
需要判断运行数据时使用 IF，需要监视事件未发生时使用 NOT 触发条件。
两者的结构和适用条件见 [规则格式](rule-schema-v2.md)。

### cancellation_api=runtime-v1（稳定）

声明后规则可以给这一步配 timeout_seconds。引擎把取消事件放进 context，
动作里长循环要周期检查：

```python
cancel = context.get("runtime", {}).get("cancellation")
if cancel is not None:
    cancel.raise_if_cancelled()
```

辅助类在 `notmyfault/core/workflow.py`（`ActionCancelled`、`ActionCancellation`）。
没有这个声明的动作配了超时，规则校验会报错（rules.py）。

## 触发器

### trigger event-v2（稳定）

```python
from notmyfault.triggers.base import PollingTrigger

class MyTrigger(PollingTrigger):
    interval = 5.0

    def validate(self): ...      # 配置非法抛 ValueError
    def setup(self): ...         # 一次性初始化
    def poll(self): ...          # 每轮采样，self.emit({...}) 发事件
    def teardown(self): ...      # 退出时释放热键、连接等资源

def run(meta, config, emit_event, shutdown_event):
    MyTrigger(meta, config, emit_event, shutdown_event).run()
```

基类负责轮询、异常隔离和退出清理，文档写在
`notmyfault/triggers/base.py` 的模块 docstring。仓库例子：
`notmyfault/triggers/window_title/trigger.py`。

### trigger event-v1（稳定，不推荐新插件用）

入口签名 run(meta, configs, emit_event, shutdown_event)，configs 是配置列表，
轮询循环要自己写。老触发器都长这样，新插件请用 event-v2。

## 贡献（contributes，稳定）

四种贡献都登记在 `notmyfault/extensions/registry.py`：

- commands：由参数编辑器或页面调用的处理函数
- views：插件自带的 HTML 页面，路径必须位于插件目录内
- parameter_editors：为普通参数或 `plugin_data` 参数提供编辑入口
- data_types：插件私有或共享数据的结构、类型和版本

普通参数编辑器声明 `value_type`，命令用 `context.commit_value()` 提交新值。自有数据
编辑器声明 `data_type`，命令用 `context.commit()` 提交带归属信息的值。完整协议见
`plugin-extension-api.md`。数据类型的版本属于值的 `$type` 标识；当前没有自动迁移钩子。
更改类型 id 或版本前，插件必须自行处理已经保存的值。

## 平台服务（稳定）

第三方插件从 `notmyfault.plugin_api` 取得平台服务，不要直接 import
`notmyfault.platform.linux_support` 或 `notmyfault.platform.backends`：

```python
from notmyfault.plugin_api import platform_services


def run(action_info, params):
    services = platform_services()
    services.write_clipboard(str(params.get("text", "")))
    return {"backend": services.capability("clipboard.write").backend}
```

`capability()` 返回 `CapabilityStatus`，字段是 `id`、`available`、`backend`、
`reason` 和 `degraded`。Linux 平台服务直接提供这些方法：

- clipboard.read：`read_clipboard()`
- clipboard.write：`write_clipboard(text)`
- input.send：`type_text(text)`、`send_hotkey(parts)`
- audio.control：`set_volume(percent)`、`set_mute(muted)`
- audio.device_query：`default_audio_devices()`
- window.pin：`set_window_pinned(action, target, title)`
- display.brightness：`set_brightness(percent)`
- screen.capture：`capture_screen(output_path, mode, fmt)`

能力缺失、能力 id 不存在、权限拒绝或命令失败都会抛 `PlatformServiceError`。
`kind` 会给出 `unavailable`、`unsupported`、`backend_missing`、`permission_denied`
或 `backend_failed`。插件不需要 import 内部 backend 的类或异常类型。

## 插件私有数据（plugin_data，稳定）

参数 `type` 可以写 `plugin_data`。值由 `parameter_editor` 的命令提交。规则校验默认只接受
`package_name`、数据类型 id 和版本都匹配的自有数据信封；编辑器声明 `accepts_legacy: true`
时，也可以读取旧的非空对象。插件升级没有迁移钩子；`version_code` 变化不会使旧值失效，
但编辑器应能处理或重新生成旧值。

`plugin_data` 参数允许声明 `sensitive: true`。运行记录会显示“敏感值已隐藏”；
规则编辑器也不会展示摘要。未标记敏感时，编辑器会显示信封中的 `summary`。用户打开
参数编辑器时，NotmyFault 会把完整值交回拥有该数据类型的插件；规则不能将这项数据绑定到其他
触发器或动作输出。

## 隔离执行（execution_mode: isolated，experimental）

manifest 写 `"execution_mode": "isolated"` 的动作不会在引擎进程中导入。每次执行会启动
子进程调用 `run()` 或 `run_with_context()`；父子进程之间使用 JSON 标准输入和输出，
新宿主使用 `typed-v1` 编解码保留大整数、Decimal 和二进制等值。
子进程先发 `{"type":"ready","protocol":1}`，父进程再发送 entry、action_info、
params、最小 context 和整棵插件目录的文件快照。worker 每次运行前复核文件快照，
入口、兄弟模块和扩展命令使用校验时读取的源码；目录内容变化时拒绝执行。
执行结果的 `type` 是 `result`，无法通过数据编解码的返回值按动作失败处理。
这只隔离崩溃，不是安全沙箱，也不限制文件、网络、进程或系统调用权限。worker 异常退出时，引擎发布
`plugin_worker_crashed` 事件，规则中的该步骤失败。默认启动等待为 10 秒，动作执行等待为
120 秒，进程退出等待为 5 秒。
实现位于 `notmyfault/core/plugin_worker.py`。内置和用户动作均可声明 `isolated`；
隔离模式不能声明 `cancellation_api` 或 `admin` 权限。调用原生库或 `ctypes` 的第三方动作优先使用此模式，
原生代码仍拥有当前用户的文件、网络和系统调用权限。

内置 `set_volume` 使用 `isolated`，Windows 的 pycaw COM 调用在子进程中执行。
该动作不声明 `cancellation_api`，规则不能为它配置协作取消超时。

## 允许 import 的模块

第三方插件可以 import：

- notmyfault.plugin_api（平台服务、公开错误、能力状态、HOST_API_VERSION）
- notmyfault.triggers.base（PollingTrigger）
- notmyfault.core.workflow（ActionCancelled、ActionCancellation）
- notmyfault.security.sudo（run_as_admin，需声明 admin 权限）
- notmyfault.security.plugin_resources（plugin_resource，读插件自带资源）
- notmyfault.platform.platform_support（show_notification 等 NotmyFault 服务）

除此之外的 notmyfault.* 都是引擎内部实现，不属于稳定插件 API；其中
notmyfault.core.engine、notmyfault.core.trigger_supervisor、
notmyfault.core.workflow_executor、notmyfault.host.*、notmyfault.platform.linux_support
和 notmyfault.platform.backends 明确不要引用。
安全扫描目前只拦危险调用和动态导入，不检查 import 了哪些内部模块，
越界引用不会被扫描器拦下来，但引擎改内部结构时插件会跟着坏。

插件内的 Python 兄弟模块在各自插件的模块名称下加载。`import helper` 和包内相对
导入可以使用；两个插件包含同名 `helper.py` 时分别取得自己的模块。插件目录不会
加入全局 `sys.path`，卸载时清理该插件创建的模块。

## 插件包签名与安装

`pack_plugin.py` 保留 `signature.sig`、`public_key.pem` 和 `public_key.sig`。
安装构建命令也保留这些文件，作者签名必须与构建后的文件内容相符。有构建命令时
先校验插件签名，签名无效则不执行命令。安装端在替换旧版本前使用与加载器相同的
签名规则校验暂存目录；strict 要求有效签名，作者自签的非管理员插件还需本地密钥
副签，管理员插件仅接受 `official` 签名。

签名覆盖插件源码、资源以及 `node_modules`、`__pypackages__` 内的文件，排除
Python 的 `__pycache__` 缓存与签名材料本身。修改依赖内容也需要重新签名。
动作与触发器共用插件 ID 空间，安装预览与安装会检查另一种类型中的同名 ID。

## 安全扫描的边界

`scan_plugin_security`、能力扫描和借壳提权扫描用于提示风险和检查权限声明，
不是 Python 安全沙箱。扫描器只看静态源码，字符串拼接、运行时生成代码和原生
模块都可能超出它的判断范围。安装插件仍等于信任插件以当前用户身份运行。
带 build 钩子且签名有效的插件才会在安装时执行清单里的命令，命令按参数列表执行，
不经过 cmd 或 sh。隔离动作进程只隔开崩溃，不限制文件、网络、进程或系统调用权限。

## 启动、执行和停止

- setup：trigger 加载后、启动前调一次，返回 False 拒绝启用（action 没有 setup）
- 执行：动作按规则触发；isolated 动作在子进程
- 参数校验：插件定义 `validate_params(action_info, params)` 时，返回错误列表或抛出异常都会拒绝执行本步
- teardown：引擎停止时调用，插件在这里注销热键、关连接
- 引擎停止：扩展会话一并关闭
- isolated worker：引擎 shutdown 时 shutdown_all() 终止所有活着的子进程

## 通用数据类型

`data_types_api()` 提供类型声明、校验、转换、共享类型注册和传输编解码。
参数通过 `value_type` 声明数据类型；输出也可使用此字段精化原有 `type`。
`run(meta, params)`、`run_with_context(meta, params, context)` 和原触发协议继续可用。
共享自定义类型由清单声明，宿主在执行前检查类型和依赖。运行上下文中的
`constants` 与 `variables` 是传给插件的值副本，修改它们不会给规则变量赋值。
隔离动作协议通过 `value_encoding: typed-v1` 保留大整数、精确小数和二进制等值。
类型、转换和旧清单兼容规则见 [data-types.md](data-types.md)。
