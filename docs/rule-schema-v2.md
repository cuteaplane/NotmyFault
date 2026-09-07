# 规则格式 v2

规则格式 v2 为触发条件和动作分配稳定的 `binding_id`，并使用
结构化 `$ref` 在一次规则运行中传递数据。

## 节点身份

- 触发条件：`t_...`
- 动作：`a_...`

节点移动时 ID 不变，复制节点时必须生成新 ID。

## 数据引用

读取指定触发条件的输出：

```json
{
  "$ref": {
    "scope": "trigger",
    "node": "t_folder01",
    "path": ["path"]
  }
}
```

读取指定触发器的配置值。v2 规则中，事件节点的 `params` 就是该实例的配置，
例如热键字符串或监控路径：

```json
{
  "$ref": {
    "scope": "trigger_config",
    "node": "t_hotkey01",
    "path": ["hotkey"]
  }
}
```

读取本次促成规则成立的事件：

```json
{
  "$ref": {
    "scope": "event",
    "path": ["path"]
  }
}
```

读取前序动作结果：

```json
{
  "$ref": {
    "scope": "step",
    "node": "a_upload01",
    "path": ["uploaded_files"]
  }
}
```

完整引用保留数字、布尔、数组和对象等原始类型。旧的
`{{ event.payload.path }}` 与 `{{ steps.action_1.result.value }}` 继续解析，
但 Dashboard 不再生成新的字符串引用。

## 输出字段

触发器和动作通过插件清单的 `outputs` 描述可供后续节点使用的数据：

```json
{
  "name": "path",
  "label": "变化文件",
  "type": "string",
  "value_type": "path",
  "required": true,
  "sensitive": false
}
```

支持基本标量、时间、路径、网址、结构化集合和插件共享类型，完整声明见
[data-types.md](data-types.md)。旧动作插件的
字符串简写 `["files", "count"]` 等价于类型为 `any` 的必需输出。

动作通过 `params` 声明输入端口。`type` 继续描述 Dashboard 使用的表单控件；
当运行数据类型与控件类型不同时，使用 `value_type` 单独声明：

```json
{
  "name": "records",
  "label": "记录",
  "type": "textarea",
  "value_type": "array"
}
```

## 条件可用性

对于普通事件条件，`all` 组合中的所有事件都已发生；`any` 组合只保证实际命中的分支已发生。
`all.within_seconds` 限制各事件命中时间的最大间隔，接受大于 0 的有限数字。
没有设置时，未被消费的事件不会按固定时长过期。
动作可以引用 `any` 中的单个分支。该分支未命中时，引用它的动作状态为
`skipped`。引用被跳过步骤输出的后续动作也会跳过。
NOT 的被监视事件不提供本次运行的数据，IF 引用缺失数据时失败，不进入任一分支。

若所有可能触发事件都声明了同名同类型输出，也可以使用 `scope: "event"`。

## NOT 事件条件

普通事件条件在事件发生时命中。`op: "not"` 在指定事件持续未发生时命中，
只接受一个事件叶子，`within_seconds` 必须是大于 0 的有限数字。

```json
{
  "op": "not",
  "within_seconds": 60,
  "children": [{
    "binding_id": "t_folder01",
    "type": "folder_monitor",
    "params": {"folder_path": "D:\\watch", "event_type": "all"}
  }]
}
```

引擎开始监视规则时启动计时。收到匹配事件后重新计时；其他类型或其他实例的
事件不会重置等待。超时产生一次 `absence` 事件，之后保持安静，直到再次收到
匹配事件。引擎每秒检查到期时间，因此触发可能比等待时长略晚。规则重载时重新计时。

NOT 判断事件是否发生，不查询进程、窗口或文件的当前状态。NOT 可作为 AND / OR
的子条件；AND 仍需等其他分支命中。超时事件没有被监视事件的 payload，因此
含 NOT 的规则不接受 `scope: "event"` 数据引用；应指定实际发生的触发节点。

## IF / ELSE 动作分支

动作列表可放入 `type: "if"` 节点。`condition` 判断本次运行数据，成立执行 `then`，
否则执行 `else`。`else` 可省略或留空，两个分支至少有一个动作。
分支可继续嵌套 IF，执行完后继续外层动作。

```json
{
  "binding_id": "a_branch01",
  "type": "if",
  "condition": {
    "op": "eq",
    "left": {"$ref": {"scope": "step", "node": "a_append01", "path": ["file"]}},
    "right": "D:\\NotmyFault\\events.txt"
  },
  "then": [{
    "binding_id": "a_notify01",
    "type": "notify",
    "params": {"title": "NotmyFault", "message": "文件已写入"}
  }],
  "else": []
}
```

此例应放在 `binding_id: "a_append01"` 的 `append_text` 动作之后。
比较支持 `eq`、`ne`、`gt`、`gte`、`lt`、`lte`、`contains`、`is_true`、`is_false`。
比较节点使用 `left` 和 `right`，布尔判断只使用 `left`。`all`、`any`、`not` 使用
`children` 组合判断，判断中的 NOT 是逻辑取反，必须只有一个子条件，不计时。

大小比较要求两侧为整数、浮点数或精确小数，布尔值不当作数字。`is_true` / `is_false` 要求布尔值。
引用缺失、未执行或失败步骤的数据会使 IF 失败，不能通过 NOT 将缺失数据变为真。
IF 默认失败后停止；`on_error: "continue"` 可继续外层动作。重试、超时和失败后动作
配置放在分支内的普通动作上，IF 本身不接受这些配置。

分支可以引用外层前序动作和当前分支内的前序动作。不能引用另一个分支、后续动作
或从分支外引用分支内部的结果。复制 IF 时生成新节点 ID，并同步修改分支内部引用。
保存时两个分支的插件、参数引用和管理员权限均会检查。
重新签名前的安全摘要也展开两个分支及失败后动作，外层节点汇总内部高风险标记。

## 旧的运行前检查

规则不再执行 `preconditions`，也不再因为检查未通过而自动延后工作流。
非空 `preconditions` 会被规则校验拒绝。编辑器可移除旧检查，随后使用 NOT 监视
事件未发生，或使用 IF 判断运行数据。旧检查不会自动转换成 NOT。
已签名的旧规则仍可读取供编辑；运行入口继续拒绝非空运行前检查，保存前必须移除。

## 运行上下文

引擎为每次规则运行创建独立快照：

```json
{
  "context_version": 2,
  "constants": {},
  "variables": {},
  "event": {"type": "folder_monitor", "payload": {}},
  "triggers": {
    "t_folder01": {
      "type": "folder_monitor",
      "payload": {},
      "config": {"folder_path": "D:\\watch"}
    }
  },
  "steps": {
    "a_upload01": {
      "type": "upload",
      "status": "ok",
      "result": {},
      "error": null
    }
  }
}
```

步骤状态包括 `ok`、`failed`、`skipped`、`timed_out` 和 `cancelled`。
`config` 是该触发器实例的配置快照，供 `scope: "trigger_config"` 引用。

## 事件匹配（event-v2）

- `event-v2` 触发器为每种配置创建一个实例。引擎用 `config_fingerprint()`
  将实例配置和规则事件节点的 `params` 通过 `typed-v1` 编码转成统一格式的 JSON 字符串，
  字符串相同即匹配。`payload` 只提供输出数据，不参与匹配。
- v1/legacy 触发器继续逐项比较事件节点 `params` 与 `payload` 中的键值。
- 引擎按插件的 `outputs` 声明检查 `emit_event(payload)`。缺少 `required`
  输出、包含未声明字段或基本类型不符时，引擎拦截事件并告警，不再分发。
- 旧规则中的纯模板字符串（`{{ event.payload.x }}`、
  `{{ steps.action_1.result.value }}`）在配置加载时自动升级为结构化 `$ref`；
  带前后缀的混合模板保留，在运行时解析。
- 转成 JSON 字符串前，整数值的浮点数会先转成整数。例如 `90.0` 转成 `90`，
  两种写法视为同一配置，实例去重和规则重载后的匹配结果保持一致。
  布尔值仍与 `0` / `1` 区分。
- 枚举型参数（state / resource / direction / event_type 等）取值非法时
  触发器直接抛异常并告警，不会静默空转。

普通动作插件仍只接收解析后的 `params`，不需要声明 `context-v1`。

## 常量、变量与显式转换

规则的 `constants` 声明只读常量，`variables` 声明每次运行独立初始化的变量。
`set_variable` 动作完成赋值，值通过 `constant` 和 `variable` 引用范围读取。
`$convert` 负责显式转换，`$template` 拼接文本，`$literal` 保留原始数据。
可选字段可以使用 `on_missing` 指定报错、跳过或默认值。
完整格式、单位、精度和传输协议见 [data-types.md](data-types.md)。

## 运行并发与结束

规则的 `concurrency.mode` 支持 `single`、`queue`、`replace` 和 `parallel`，默认 `parallel`。
所有模式共用 8 个工作线程和默认 32 条等待记录的总上限。
线程已满时，新运行进入有界队列；队列已满时产生 `run_dropped`。

`single` 在同一规则已有运行或排队记录时丢弃新触发。
`replace` 请求取消旧运行并替换该规则尚未执行的排队记录。
旧动作实际退出前仍占用工作线程；取消是否立即生效取决于动作的协作取消支持。
`queue` 默认每条规则运行一个实例，`parallel` 默认可使用全部空闲线程。
这两种模式可以通过 `max_concurrency` 和 `queue_limit` 限制单条规则的并发与等待数量。

动作绑定失败和参数校验失败先产生动作错误，按配置执行 `failure_actions`。
工作流在失败后动作结束时发送一次终态，失败动作按 `on_error` 决定是否继续。
停止超时且仍有触发器或动作运行时，引擎保持 `stopping`，保留插件资源并拒绝启动新实例。
剩余线程退出后再次停止，才能完成插件清理。
