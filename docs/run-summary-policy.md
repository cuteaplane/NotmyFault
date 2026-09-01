# 动作运行摘要

运行中心不保存动作的原始参数和返回值。插件可以在 `params` 和 `outputs` 字段上声明
摘要方式。

`summary` 支持三个值。

| 值 | 运行中心显示 |
|---|---|
| `shape` | 默认值。只显示文本长度、数组项数、对象字段数或数据类型 |
| `value` | 显示标量值。文本最多保留 120 个字符 |
| `hidden` | 不显示这个字段 |

字段声明 `sensitive: true` 后始终显示“敏感值已隐藏”。`summary: value`
不能覆盖敏感标记。

```json
{
  "params": [
    {
      "name": "token",
      "type": "string",
      "label": "令牌",
      "sensitive": true,
      "summary": "hidden"
    }
  ],
  "outputs": [
    {
      "name": "count",
      "type": "number",
      "label": "处理数量",
      "summary": "value"
    }
  ]
}
```

没有 `summary` 时按 `shape` 处理。没有在插件元数据中声明的字段不会进入摘要。
每侧最多保存八个字段。标签最多 80 个字符，显示内容最多 120 个字符。

`notmyfault/core/run_summary.py` 生成摘要。
`notmyfault/core/run_history.py` 再次按字段白名单和长度上限过滤后写入 JSONL。
动作原始 `params` 和 `result` 仍不会写入运行账本。
