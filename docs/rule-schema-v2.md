# Rule schema v2

规则格式 v2 为触发条件、开始前确认和动作分配稳定的 `binding_id`，并使用
结构化 `$ref` 在一次规则运行中传递数据。

## 节点身份

- 触发条件：`t_...`
- 开始前确认：`p_...`
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

读取指定触发器的配置值（v2 语义下事件叶子 params 即实例配置，如热键
字符串、监控路径）：

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

## 输出契约

触发器和动作通过插件清单的 `outputs` 描述可供后续节点使用的数据：

```json
{
  "name": "path",
  "label": "变化文件",
  "type": "string",
  "format": "path",
  "required": true,
  "sensitive": false
}
```

支持 `string`、`number`、`bool`、`array`、`object` 和 `any`。旧动作插件的
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

`all` 组合中的所有叶子都保证存在；`any` 组合只保证实际命中的分支存在。
动作可以引用 `any` 中的单个分支，此数据引用同时构成控制依赖：该分支未命中
时动作状态为 `skipped`。引用被跳过步骤输出的后续动作也会跳过。开始前确认
仍只能引用每次运行都保证存在的数据。

若所有可能触发事件都声明了同名同类型输出，也可以使用 `scope: "event"`。

## 运行上下文

引擎为每次规则运行创建独立快照：

```json
{
  "context_version": 2,
  "event": {"type": "folder_monitor", "payload": {}},
  "triggers": {
    "t_folder01": {
      "type": "folder_monitor",
      "payload": {},
      "config": {"folder": "D:\\watch"}
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

## 事件匹配语义（event-v2）

- `event-v2` 触发器每个配置一个实例，规则命中改为**配置匹配**：事件携带
  实例配置指纹，与规则事件叶子的 `params` 指纹相等即命中，payload 不参与
  命中判断（payload 只是输出数据）。
- v1/legacy 触发器保持 payload 过滤语义（叶子 `params` 与 payload 键值比较）。
- `emit_event(payload)` 受插件 `outputs` 契约约束：缺 required 输出、
  未声明字段或简单类型不符的事件会被引擎拦截并告警，不进入分发。
- 旧规则中的纯模板字符串（`{{ event.payload.x }}`、
  `{{ steps.action_1.result.value }}`）在配置加载时自动升级为结构化 `$ref`；
  带前后缀的混合模板保留，由绑定解析兜底。
- 配置指纹先做规范化：整数值 90 与 90.0 视为相同（避免同一配置因写法
  不同而去重失败或热重载后不命中）；布尔与 0/1 保持区分。
- 枚举型参数（state / resource / direction / event_type 等）取值非法时
  触发器直接抛异常并告警，不会静默空转。

普通动作插件仍只接收解析后的 `params`，不需要声明 `context-v1`。
