# 变量与数据类型

规则可以声明常量和运行变量。常量在规则启动前求值，只读，可用于触发器配置。
运行变量在每次执行开始时独立初始化，通过“变量赋值”动作修改。不同运行之间
不共享值；变量不跨规则共享，也不在重启后保留。

## 在规则编辑器中使用

1. 打开“常量与变量”，添加名称、数据类型和值。变量可以不设初始值。
2. 在参数的绑定入口选择常量、变量、触发器或前序动作输出。
3. 对象可以继续提取字段，数组可以输入 `[0]` 等下标路径。
4. 可选输出需要选择缺失时“报错”“跳过本动作”或“使用默认值”。
5. 添加“变量赋值”动作，选择变量和新值。它可以放在 IF / ELSE 或失败后动作中。
6. 手动测试可以覆盖本次变量初始值，也可以检查输出的子字段。

名称用于显示，引用使用稳定 ID。重命名不会改变引用；删除仍被使用的定义后，
需要修改对应引用。敏感值在运行摘要中隐藏，测试数据和草稿恢复不保存标记为
敏感的值。规则文件仍包含执行所需的原值，`sensitive` 不提供加密存储。

## 类型声明

参数的 `type` 描述表单控件，`value_type` 描述传递的数据。输出也可以通过
`value_type` 精化原有 `type`。显式声明的 `value_type` 均在运行时校验，包含
`bool`、`array`、`object` 等原有类型名称。字符串简写与结构声明均可使用：

```json
{"name": "source", "label": "源路径", "type": "string", "value_type": "path"}
```

```json
{"name": "files", "label": "文件列表", "type": "array", "value_type": {"type": "array", "items": "path"}}
```

| 类型 | 值和约束 |
| --- | --- |
| `text` | Unicode 文本；`string` 是旧别名 |
| `int` | 整数，拒绝布尔和小数；`integer` 是别名 |
| `float` | 有限浮点数，拒绝 NaN 和无穷值 |
| `number` | 兼容整数与浮点数的旧数值类型 |
| `decimal` | 精确十进制，Python 使用 `Decimal` |
| `bool`、`null` | 布尔与明确的空值；`boolean` 是布尔别名 |
| `date` | ISO 日历日期，如 `2024-02-29` |
| `time` | ISO 时间，如 `15:30` 或 `15:30:00+08:00` |
| `datetime` | ISO 日期时间，必须包含时间部分 |
| `timestamp` | Unix 时间戳，必须明确 `unit: "seconds"` 或 `"milliseconds"` |
| `duration` | 固定时长，单位为秒或毫秒，默认秒 |
| `path` | 路径文本，不检查存在、不改写分隔符；拒绝 NUL |
| `url` | 带协议的地址，支持 HTTP 之外的协议 |
| `uuid` | UUID；校验后使用标准文本格式 |
| `bytes` | 二进制数据，Python 使用 `bytes` |
| `array` | 数组，`items` 声明元素类型；`list` 是别名 |
| `object` | 对象，允许字段和字典值类型声明 |
| `union` | `variants` 中任意一种类型 |
| `any` | 动态数据，接入具体类型时检查实际值 |

所有类型可以附加 `nullable: true`。`enum` 列出允许的标量值；数字类型可以声明
`min`、`max`。`decimal` 的范围还可以使用十进制文本。

`time` 和 `datetime` 的 `timezone` 可以是 `aware`、`naive` 或默认 `any`。
`path.flavor` 可以是 `windows`、`posix` 或默认 `any`，描述生产者和消费者的路径
约定，不对路径内容进行平台转换。路径默认不接受空文本；允许留空表示使用插件
默认位置的参数，可声明 `{"type":"path","allow_empty":true}`。
`url.schemes` 可以限制协议，如 `["http","https"]`。

```json
{
  "type": "object",
  "properties": {"name": "text", "files": {"type": "array", "items": "path"}},
  "required": ["name"],
  "additional_properties": false
}
```

对象未列入 `required` 的字段可能缺失。`additional_properties` 默认为 `true`，
也可以是 `false` 或一个数据类型。旧 `item_type` 继续描述数组元素，旧 `format`
中的路径、网址、日期、时间、日期时间和 UUID 提示也参与端口类型推导。

## 常量和变量格式

```json
{
  "constants": [
    {"id": "c_folder01", "name": "归档目录", "value_type": "path", "value": "D:/archive"}
  ],
  "variables": [
    {"id": "v_count001", "name": "处理数量", "value_type": "int", "initial": 0}
  ],
  "actions": [
    {"type": "set_variable", "binding_id": "a_count001", "variable": "v_count001", "value": 3}
  ]
}
```

上例是规则中的字段片段。常量 ID 以 `c_` 开头，变量 ID 以 `v_` 开头。引用格式：

```json
{"$ref": {"scope": "constant", "node": "c_folder01", "path": []}}
```

```json
{"$ref": {"scope": "variable", "node": "v_count001", "path": []}}
```

常量可以引用其他常量；循环依赖和运行数据引用会被拒绝。变量初始值可以引用
常量。赋值先解析并校验新值，再替换旧值。失败时原值保留；`on_error` 可设为
`stop` 或 `continue`。只有实际执行的分支会赋值。赋值动作输出 `value`。

## 引用、缺失值与转换

`path` 中字符串是对象键，非负整数是数组下标，如 `["records", 0, "名称"]`。
含空格或点号的键保持原样。引用不访问 Python 属性、宿主内部字段、原型链字段
或插件私有数据。完整对象仍保留原始业务字段。

```json
{
  "$ref": {
    "scope": "step", "node": "a_list0001", "path": ["files", 0],
    "on_missing": "default", "default": "D:/fallback.txt"
  }
}
```

缺失与 `null`、空文本、0、false 分开。默认值只处理缺失，不吞掉插件执行失败或
类型错误。没有显式缺失策略的旧引用继续使用原有分支跳过规则。

文本进入路径、网址、日期等端口时校验实际值。`number` 进入更具体的数值端口
时也要通过实际值校验。浮点转整数、文本解析数字和时间戳单位变换应显式转换：

```json
{"$convert": {"value": "3.75", "to": "int", "options": {"rounding": "floor"}}}
```

```json
{"$convert": {"value": 1709220600000, "from": {"type": "timestamp", "unit": "milliseconds"}, "to": {"type": "timestamp", "unit": "seconds"}}}
```

引用通常能推导来源类型，固定数字表示时间戳时需要 `from` 指明单位。
转换选项包括 `rounding`、`timezone`、`encoding` 和 `allow_lossy`。
整数舍入支持 `exact`、`truncate`、`floor`、`ceil`、`round`，默认 `exact`。
时区支持 `preserve`、`utc`、`local` 和可用的 IANA 时区名。
二进制与文本之间可指定文本编码或 `base64`。
对象与文本默认使用 JSON；包含 `Decimal`、二进制等值时，往返双方指定
`encoding: "typed-v1"`。联合类型不能作为显式转换目标，需选择具体成员类型。

结构化文本拼接只接受文本项，数字等类型先转换为文本：

```json
{"$template": ["数量：", {"$convert": {"value": {"$ref": {"scope": "variable", "node": "v_count001", "path": []}}, "to": "text"}}]}
```

恰好只有 `$literal` 键的 `{"$literal": value}` 将 `value` 按固定值传递，其中的
`$ref`、`$convert` 和旧模板不会执行。同时含其他键的对象按普通对象解析。
旧字符串模板仍可读取。自定义类型信封也不会被递归解释成表达式。

## 插件共享类型

插件在 `contributes.data_types` 中声明 `id`、正整数 `version`、`binding` 和
`schema`。默认 `binding: "private"`，跨插件传递必须显式声明 `shared`，并提供
有效的结构声明。现有 UIA 选择器与操作宏保持私有。

```json
{"id": "record", "version": 1, "binding": "shared", "label": "记录", "schema": {"type": "object", "properties": {"count": "int"}, "required": ["count"]}}
```

完整身份为 `package_name/record@1`，值使用 `{"$type": "完整身份", "data": ...}`，
可带不超过 160 字符的 `summary`。消费者在 `value_type` 中引用完整身份，也可在
类型结构中嵌套其他共享类型。数据字段通过引用路径 `["data", "count"]` 访问。
`["$type"]` 可读取类型身份，`["summary"]` 可读取可选的文本摘要。
同名但不同包或不同版本的类型互不替代。

类型从已校验的插件清单创建快照，单次运行使用同一份类型定义。类型缺失、禁用、
依赖缺失或版本不匹配时报告错误。读取已保存规则不进行自动类型升级或降级。
编辑器保留不可用类型的原始声明和值。

插件通过公开 API 访问校验、转换、类型注册和编解码：

```python
from notmyfault.plugin_api import data_types_api

types = data_types_api()
value = types.convert_value("9007199254740993", "int")
assert types.normalize_value(value, "int") == 9007199254740993
```

`TypeRegistry.from_plugins(...)` 从清单集合构建注册表，`make_value(identity, data)`
校验并包装共享值。`normalize_value`、`convert_value` 可传入该注册表。注册共享
类型不要求提供专用编辑器，标准结构化输入即可使用。

## 保存与传输

`int` 超过 JavaScript 精确范围、`Decimal`、二进制和 Python 日期时间等值采用
`$nmf_value` 编码。`int` 和 `decimal` 使用十进制文本，`bytes` 使用 Base64。
普通对象恰好只有 `$nmf_value` 键时会转义为 `object` 编码，往返保留原对象。
通用 JSON 编辑器保留完整编码对象；精确小数和二进制等专用编辑器显示对应的
十进制文本或 Base64，输入后恢复编码对象。

规则文件使用 `schema_version: 2` 与 `value_encoding: "typed-v1"`，签名验证在解码
前进行。没有编码标记的旧文件按原格式读取。隔离动作与 HTTP 返回值也使用同一
编解码器。HTTP 请求含编码值时发送 `X-NMF-Value-Encoding: typed-v1`；没有该
请求头的旧 JSON 客户端仍按普通 JSON 解释。第三方客户端可通过公开 API 的
`encode_value`、`decode_value` 处理协议。

## 内置插件

文件相关插件声明路径与路径数组，计数和进程退出码声明整数，HTTP 输入声明网址。
USB 触发器的 `actual_drive` 是路径，`drive_letter` 是可含 `ANY` 的匹配文本。
文件操作目标和截图位置可留空；每日计划仍按本地小时和分钟执行。

文本读写、追加与内容监控接受 Python 文本编码名称。`text_transform` 提供
`split` / `join`；`url_codec` 提供 `compose` 并保留重复查询键；`datetime_format`
可选择 ISO 或明确为秒的时间戳输入。`file_info.modified_time` 为可空日期时间，
原 `modified_at` 字段继续保留。UIA 各操作的可选输出可以通过缺失策略绑定。

截图动作返回 `{"file": "截图路径"}`，`file` 的类型为路径。引用截图结果时选择
`file` 字段；旧规则若直接读取整个步骤结果作为路径，应改为读取 `file`。
窗口置顶动作按平台返回可选的 Windows `hwnd` 整数或 Linux `window_id` 文本。
UIA 等待控件的可选 `bounds` 对象提供 `left`、`top`、`width`、`height` 整数字段，
控件没有可读取范围时对象为空，引用子字段需要设置缺失策略。

显示器控制提供 `action`，亮度操作另有 `brightness`，Windows 的亮度结果还包含
`methods` 和 `warnings` 文本数组。蓝牙控制提供 `action`，Windows 下另有包含
适配器 `name` 和 `state` 的 `radios` 对象数组；结束进程
提供整数 `killed` 和 `denied`。这些输出均可在后续动作或变量赋值中引用。
