# 规则格式 v2.1

规则以 `workflow.nodes` 和 `workflow.edges` 保存平面图。节点、组、分支和端口具有稳定身份。
编辑器、API、文件读取、编译、事件匹配和执行使用同一张图。
对象和数组嵌套最多 64 层；超出上限时返回结构错误。

## 文件与规则字段

`rules.json` 包含 `schema_version: "2.1"`、`value_encoding: "typed-v1"` 和 `rules`。
读取时先验签，再检查格式和编码标记，随后解码并校验图。
规则备份恢复执行相同检查。普通 `config.json` 保存程序配置，其校验不要求规则编码标记。

规则顶层允许 `rule_id`、`name`、`folder`、`workflow`、`constants`、`variables`、`concurrency`。
其他字段返回“包含未知字段”错误。`workflow.entry` 可以省略或为 `null`；入口由连线确定。
执行顺序由边决定，与节点数组中的排列无关。

## 身份与所属组

- `rule_id` 标识规则。
- 图块的 `binding_id` 使用 `a_` 前缀，包括触发器图块。
- 触发器事件身份是图块内的 `trigger.binding_id`，使用 `t_` 前缀。
- IF／ELIF／ELSE 的端口身份保存在事件组 `branches` 中，使用 `b_` 前缀。
- `parent` 引用所属事件组或动作组的图块身份。组内节点仍保存在同一个 `nodes` 数组中。

API 校验已有身份后为缺少身份的新节点分配值。非法或重复身份不能用自动分配掩盖。
规则文件中的身份必须有效且唯一。移动节点保持身份；复制节点生成新身份，并更新内部引用与连线。
所属组不能形成循环。

## 节点与端口

| 节点 | 主要字段 | 来源端口 |
| --- | --- | --- |
| `trigger` | `trigger.type`、`trigger.params`、`trigger.binding_id` | `event` |
| `condition` | `predicate` 保存单个比较或自动化状态判断 | `result` |
| `logic` | `op: all / any / not` | `result` |
| `event_group` | `group_type`、`mode`、`branches` | IF 组使用分支 ID；其他组使用 `result` |
| `action_group` | `exports` | `entry` 进入组内，`next` 接组外后继 |
| `set_variable` | `variable`、`value` | `next`、`failure` |
| 插件动作 | `type`、`params` | `next`、`failure` |
| `merge` | 接收汇合后的控制流 | `next` |
| `end` | 显式结束当前路径 | 无 |

连线保存 `source`、`port`、`target`、`channel`，条件输入可以用 `target_port` 指定端口。
`channel` 为 `condition` 或 `control`。条件线传递判定结果，控制线决定执行路线。
数据线由结构化引用和组导出生成，不单独保存数据绑定。
无效端口、循环、重复连线、同一执行出口连接多个目标和跨动作组边界的控制线会被拒绝。

## IF／ELIF／ELSE

事件组使用 `type: "event_group"`、`group_type: "if"`。
`branches` 的第一项是 IF，中间项是 ELIF，最后一项是唯一的 `else: true` 分支。
IF 和 ELIF 的 `target_port` 使用对应分支 ID，各接一个条件结果。
多个条件先用 AND／OR／NOT 图块组合，再接入分支。ELSE 没有条件输入。

分支按数组顺序判断，选择首个成立出口。有条件而未连接动作的分支也可以被选中并结束路径。
未接条件的备用 ELIF 不参与判断；IF／ELIF 已连接执行目标但没有条件时，结构校验失败。

入口收到相关事件后立即选择路线。ELIF 先发生且 IF 未成立时，可以直接选择 ELIF。
入队前固定选中分支和入口事件快照；随后收到的事件不会改写该次运行的入口路线。

以下完整图来自 `notmyfault/tests/test_canvas_workflow.py` 的 `example()`；
`event_aaaaaa`、`event_bbbbbb` 和 `record` 是测试插件，分别验证两个入口与分支输出：

```json
{
  "rule_id": "r_canvas_000001",
  "name": "双事件分支",
  "workflow": {
    "entry": null,
    "nodes": [
      {"binding_id": "a_source_aaaaaa", "type": "trigger", "trigger": {"binding_id": "t_source_aaaaaa", "type": "event_aaaaaa", "params": {}}},
      {"binding_id": "a_source_bbbbbb", "type": "trigger", "trigger": {"binding_id": "t_source_bbbbbb", "type": "event_bbbbbb", "params": {}}},
      {"binding_id": "a_choice_000001", "type": "event_group", "group_type": "if", "branches": [{"id": "b_first001"}, {"id": "b_second01"}, {"id": "b_spare001"}, {"id": "b_other001", "else": true}]},
      {"binding_id": "a_action_aaaaaa", "type": "record", "params": {"message": "aaaaaa"}},
      {"binding_id": "a_action_bbbbbb", "type": "record", "params": {"message": "bbbbbb"}}
    ],
    "edges": [
      {"source": "a_source_aaaaaa", "port": "event", "target": "a_choice_000001", "target_port": "b_first001", "channel": "condition"},
      {"source": "a_source_bbbbbb", "port": "event", "target": "a_choice_000001", "target_port": "b_second01", "channel": "condition"},
      {"source": "a_choice_000001", "port": "b_first001", "target": "a_action_aaaaaa", "channel": "control"},
      {"source": "a_choice_000001", "port": "b_second01", "target": "a_action_bbbbbb", "channel": "control"}
    ]
  }
}
```

## 条件、未发生与等待

AND 使用 `op: "all"`，OR 使用 `op: "any"`，NOT 使用 `op: "not"`。
依赖由条件线连接，共享上游节点保持同一身份。AND 支持 `within_seconds` 时间窗口、
`ttl_seconds` 保留期限和 `consume: matched / all / none`；OR 支持 `pick: latest / first`。

触发器节点的 `occurrence: "absent"` 表示未发生。
入口持续未发生使用 `duration_seconds` 计时；匹配事件到达后重新计时。
`repeat: "once_until_seen"` 每轮只触发一次，`every_window` 按窗口重复。
流程内 `absence_scope: "run"` 判断本次运行至今未发生；`window` 判断连续指定秒数未发生。
窗口尚未达到时立即返回 false，不自动等待。未发生不查询进程、窗口或文件的当前状态，
也不生成被监视事件的输出数据。

需要等待后续事件时，使用 `mode: "wait"` 的事件组，按需设置 `timeout_seconds`。
执行到该组时挂起运行，事件或期限通知后恢复；等待不占用动作工作线程，也不重做等待前的动作。
每次运行独立记录收到的事件、匹配消费和等待状态。
取消、规则删除、替换和关闭能够结束等待。

比较图块的 `predicate` 保存 `op`、`left` 和按需使用的 `right`。
支持相等、不等、大小比较、包含、布尔、存在性与自动化运行状态判断。
布尔值与数字区分；数组和对象中的嵌套值也按此规则比较。
大小比较接受整数、浮点数或精确小数，不把布尔值当作数字。
逻辑组合通过图块和条件线表达。

## 动作组、补救与运行结束状态

动作组通过 `entry` 控制线进入内部图。内部节点的 `parent` 指向组身份。
组内执行完成后，由运行栈返回组的 `next` 后继；组不创建另一条规则运行。
组可以嵌套，向组外提供的数据由 `exports` 声明。

插件动作的 `failure` 控制出口只能连接同一执行范围内的普通动作组。
该补救组不能同时接受主流程入口，也不能对外连接 `next`；返回位置属于运行状态。
补救组内可以放插件动作、事件组和嵌套动作组。

动作重试耗尽后才进入补救。取消和运行整体超时直接结束运行。
`on_error` 决定原动作失败后的停止或继续行为：

| 设置与执行结果 | 后续路线与运行结束状态 |
| --- | --- |
| `stop`，补救成功 | 结束运行，状态为 `recovered`，界面显示“已补救” |
| `continue`，补救成功 | 返回原动作正常后继；后续完成时为 `succeeded` |
| `continue`，未连接补救 | 明确忽略本次错误，后续完成时为 `succeeded` |
| 未处理失败或补救失败 | 结束运行，状态为 `failed` |
| 取消或运行整体超时 | 分别为 `cancelled` 或 `timed_out` |

原动作的失败步骤一直保留。补救后又发生未处理失败、超时或取消时，按实际结果记录失败、超时或取消。
`run.recovered` 单独通知运行因补救完成而结束。规则结束触发器的“任意”包含该状态，
“仅成功”和“仅失败”不包含它；可以单独筛选“已补救”。

普通动作 `retry` 范围为 0 到 3，`retry_delay_seconds` 为 0 到 3600 秒，
`timeout_seconds` 为 1 到 86400 秒。重试与超时还需满足动作的协议声明和权限要求。

## 数据引用与类型

`$ref` 保留来源值的类型。`scope: "trigger"` 按事件身份读取触发器输出，
`trigger_config` 读取实例配置；`event` 读取入口匹配事件；`step` 按图块身份读取动作输出。
例如 `append_text` 的文件路径输出：

```json
{"$ref": {"scope": "step", "node": "a_append01", "path": ["file"]}}
```

`a_append01` 必须是本图中可达的 `append_text` 动作身份。
引用检查覆盖参数、变量初值、比较操作数、组导出和补救图块。
失败动作没有成功输出，不能把失败步骤当作已经产生结果。
组外只能读取声明的导出；不同分支上可能未执行的结果按缺失值策略处理。
未发生条件不提供匹配事件输出。入口条件不能读取尚未执行的动作输出。

触发器最新输出和入口匹配快照分别保存：前者按判定点已收到的最后一次事件读取，
后者固定本次运行的入口匹配集合。二者不会相互覆盖。
错误字段与成功输出分开处理，是否可引用以对应节点的类型和可达性检查为准。

参数控件使用 `type`，数据合同使用 `value_type`。输出可以声明类型、`required` 和 `sensitive`。
数组元素类型在 `value_type.items` 或输出类型声明的 `items` 中给出。
自有插件数据必须是带有效 `$type` 且归属匹配的信封，空对象不能通过该校验。

`constants` 为只读常量，`variables` 每次运行独立初始化，`set_variable` 执行赋值。
`$convert` 显式转换，`$template` 拼接文本，`$literal` 保留原始数据。
普通字符串按字面值传递。完整类型、缺失值策略与编码见 [data-types.md](data-types.md)。

动作清单声明 `execution_api: "context-v1"`，入口为
`run_with_context(action_info, params, context)`。触发器清单声明 `trigger_api: "event-v2"`，
入口为 `run(meta, config, emit_event, shutdown_event)`。
相同实例配置按 `config_fingerprint()` 去重并匹配；`payload` 提供输出数据。
输出缺少必填字段、包含未声明字段或类型不符时，引擎拦截事件并告警。

## 节点画布与运行记录

编辑器提供节点画布、节点设置侧栏、分组折叠、排列、相机缩放和撤销／重做。
补救图使用同一套节点创建、复制、删除、拖动和引用操作。
拖入或拖出组时保存实际断线，编译器不推测补线。
局部测试以原图节点与可达路径选择范围，并收集必需的前序数据；敏感数据不保存为测试草稿。
运行记录使用原图节点身份，显示选中分支、条件结果、等待与恢复、组进入与退出、补救和最终状态。
未连入执行路线的图块可以保存，但不计入实际执行动作数。
插件与管理员权限检查覆盖整个有效图，包括补救路线、嵌套组和未连入主流程的图块。

## 并发与热重载

`concurrency.mode` 支持 `single`、`queue`、`replace` 和 `parallel`。
`single` 在同规则已有运行或排队时丢弃新触发；`replace` 请求取消前次运行并替换待执行记录。
`queue` 和 `parallel` 可用 `max_concurrency`、`queue_limit` 限制每条规则的并发和等待数。
动作实际退出前仍占用工作线程；协作取消决定其何时能退出。

每次运行有独立的常量、变量、取消信号和事件观察记录。
文件加载严格检查身份，API 保存允许补齐新身份，随后检查插件引用、参数与数据绑定。
插件缺失、参数或引用无效时，启动和热重载跳过对应规则并记录原因，其余有效规则继续运行。
全部规则不可用时，引擎仍可等待规则更新。文件签名或结构校验失败时，热重载保留现行运行规则。
停止超时且仍有线程运行时，引擎保持 `stopping` 并保留资源；线程退出后才能完成清理。
