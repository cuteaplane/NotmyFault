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

## 条件可用性

`all` 组合中的所有叶子都保证存在；`any` 组合只保证实际命中的分支存在。
v2 第一版禁止动作直接引用不能保证命中的分支。若所有可能触发事件都声明了
同名同类型输出，可以改用 `scope: "event"`。

## 运行上下文

引擎为每次规则运行创建独立快照：

```json
{
  "context_version": 2,
  "event": {"type": "folder_monitor", "payload": {}},
  "triggers": {
    "t_folder01": {"type": "folder_monitor", "payload": {}}
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

普通动作插件仍只接收解析后的 `params`，不需要声明 `context-v1`。
