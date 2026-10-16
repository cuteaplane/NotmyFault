# 变量与数据类型

规则可以声明常量和运行变量。常量在规则启动前求值,只读,可用于触发器配置。
运行变量在每次执行开始时独立初始化,通过“变量赋值”动作修改。不同运行之间
不共享值;变量不跨规则共享,也不在重启后保留。

## 在规则编辑器中使用

1. 打开“常量与变量”,添加名称、数据类型和值。变量可以不设初始值。
2. 在参数的绑定入口选择常量、变量、触发器或前序动作输出。
3. 对象可以继续提取字段,数组可以输入 `[0]` 等下标路径。
4. 可选输出需要选择缺失时“报错”“跳过本动作”或“使用默认值”。
5. 添加“变量赋值”动作,选择变量和新值。它可以放在 IF / ELSE 或失败后动作中。
6. 手动测试可以覆盖本次变量初始值,也可以检查输出的子字段。

名称用于显示,引用使用稳定 ID。重命名不会改变引用;删除仍被使用的定义后,
需要修改对应引用。敏感值在运行摘要中隐藏,测试数据和草稿恢复不保存标记为
敏感的值。规则文件仍包含执行所需的原值,`sensitive` 不提供加密存储。

## 测试结果检查

“等于”比较完整的输出值或所选子字段,区分布尔值与数字,数组和对象中的嵌套值
也适用。数字之间按数值比较,例如整数 `1` 与浮点数 `1.0` 相等。

“包含”检查文本子串、数组中的一个元素或对象的键名。数组元素可以是数组或
对象,例如 `[[1, 2]]` 包含元素 `[1, 2]`,`[1, 2]` 不包含这个数组元素。
数组元素比较与“等于”使用相同规则。API 测试请求以对象作为期望值时,检查
所给字段是否都存在且值相等,输出中的其他字段不影响结果。

检查未通过时,动作执行成功也会将本次测试标为失败。结果事件报告通过数量和
各项检查结果,不携带实际值或期望值。
API 请求的检查项中,`step_id` 和 `operator` 必须是字符串;字段类型无效时
返回 HTTP 400,不启动动作。普通 JSON 和 `typed-v1` 请求使用相同校验。
期望值须符合数据值约束,数字必须是有限值,拒绝 NaN、无穷值和超出 64 层
嵌套上限的数据。无效期望值返回 HTTP 400,不启动动作。`typed-v1` 还会校验
整个请求的编码和嵌套深度;有效的精确整数和十进制期望值保留原值。
JSON 请求中的整数文本超过 Python 的解析位数上限时,返回 HTTP 400,不启动
动作。该限制适用于普通 JSON 和 `typed-v1` 请求中的 JSON 数字。

## 类型声明

规则保存和启动校验检查参数类型。未声明 `value_type` 的参数,类型由 `field_type`
从控件类型推导。`select` 的固定值仍须属于 `options`。`plugin_data` 由插件自有
数据格式校验处理,不允许绑定运行数据。

参数的 `type` 描述表单控件,`value_type` 描述传递的数据。输出也可以通过
`value_type` 精化 `type`。显式声明的 `value_type` 在运行时校验。字符串简写与结构声明均可使用:

```json
{"name": "source", "label": "源路径", "type": "string", "value_type": "path"}
```

```json
{"name": "files", "label": "文件列表", "type": "array", "value_type": {"type": "array", "items": "path"}}
```

| 类型 | 值和约束 |
| --- | --- |
| `text` | Unicode 文本;参数控件名 `string` 对应 `text` |
| `int` | 整数,拒绝布尔和小数 |
| `float` | 有限浮点数,拒绝 NaN 和无穷值 |
| `number` | 整数或浮点数 |
| `decimal` | 精确十进制,Python 使用 `Decimal` |
| `bool`、`null` | 布尔与明确的空值 |
| `date` | ISO 日历日期,如 `2024-02-29` |
| `time` | ISO 时间,如 `15:30` 或 `15:30:00+08:00` |
| `datetime` | ISO 日期时间,必须包含时间部分 |
| `timestamp` | Unix 时间戳,必须明确 `unit: "seconds"` 或 `"milliseconds"` |
| `duration` | 固定时长,单位为秒或毫秒,默认秒 |
| `path` | 路径文本,不检查存在、不改写分隔符;拒绝 NUL |
| `url` | 带协议的地址,支持 HTTP 之外的协议 |
| `uuid` | UUID;校验后使用标准文本格式 |
| `bytes` | 二进制数据,Python 使用 `bytes` |
| `array` | 数组,`items` 声明元素类型 |
| `object` | 对象,允许字段和字典值类型声明 |
| `union` | `variants` 中任意一种类型 |
| `any` | 动态数据,接入具体类型时检查实际值 |

所有类型可以附加 `nullable: true`。`enum` 列出允许的标量值;数字类型可以声明
`min`、`max`。`decimal` 的范围还可以使用十进制文本。
带枚举的来源绑定到枚举目标时,来源的每个枚举值都须属于目标枚举。
仪表板按整数的完整十进制值比较整数编码;布尔值、文本和数字分别判断。

`time` 和 `datetime` 的 `timezone` 可以是 `aware`、`naive` 或默认 `any`。
`path.flavor` 可以是 `windows`、`posix` 或默认 `any`,描述生产者和消费者的路径
约定,不对路径内容进行平台转换。路径默认不接受空文本;允许留空表示使用插件
默认位置的参数,可声明 `{"type":"path","allow_empty":true}`。
`url.schemes` 可以限制协议,如 `["http","https"]`。

```json
{
  "type": "object",
  "properties": {"name": "text", "files": {"type": "array", "items": "path"}},
  "required": ["name"],
  "additional_properties": false
}
```

对象未列入 `required` 的字段可能缺失。`additional_properties` 默认为 `true`,
也可以是 `false` 或一个数据类型。数组元素类型用 `items` 声明。`format` 中的路径、
网址、日期、时间、日期时间和 UUID 提示参与端口类型推导。

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

上例是规则中的字段片段。常量 ID 以 `c_` 开头,变量 ID 以 `v_` 开头。引用格式:

```json
{"$ref": {"scope": "constant", "node": "c_folder01", "path": []}}
```

```json
{"$ref": {"scope": "variable", "node": "v_count001", "path": []}}
```

常量可以引用其他常量;循环依赖和运行数据引用会被拒绝。变量初始值可以引用
常量。赋值先解析并校验新值,再替换旧值。失败时原值保留;`on_error` 可设为
`stop` 或 `continue`。只有实际执行的分支会赋值。赋值动作输出 `value`。

## 引用、缺失值与转换

`path` 中字符串是对象键,非负整数是数组下标,如 `["records", 0, "名称"]`。
含空格或点号的键保持原样。引用不访问 Python 属性、宿主内部字段、原型链字段
或插件私有数据。完整对象仍保留原始业务字段。
任意类型和嵌套任意类型也使用相同的路径规则。布尔值、小数、负数、null、对象
和数组不能作为路径段;以下划线开头的键、`constructor` 和 `prototype` 不可引用。
绑定选择器遇到这些路径时提示错误,并禁用“使用”。
对象字段的类型按 `properties` 声明推导,未声明字段按 `additional_properties`
处理;`toString`、`valueOf` 等业务键名也遵循这条规则。

```json
{
  "$ref": {
    "scope": "step", "node": "a_list0001", "path": ["files", 0],
    "on_missing": "default", "default": "D:/fallback.txt"
  }
}
```

缺失与 `null`、空文本、0、false 分开。默认值只处理缺失,不吞掉插件执行失败或
类型错误。未设置 `on_missing` 时,引用的运行数据不存在会直接报错
(`missing_binding_value`),不跳过动作,也不使用默认值。

绑定选择器切换来源或提取路径后,按提取结果的类型重新初始化缺失默认值。
整数使用 0,布尔值使用 false,文本使用空文本,数组使用空数组。
字段声明含枚举、范围或其他约束时,默认值仍须满足这些约束。

文本进入路径、网址、日期等端口时校验实际值。`number` 进入更具体的数值端口
时也要通过实际值校验。浮点转整数、文本解析数字和时间戳单位变换应显式转换:

```json
{"$convert": {"value": "3.75", "to": "int", "options": {"rounding": "floor"}}}
```

```json
{"$convert": {"value": 1709220600000, "from": {"type": "timestamp", "unit": "milliseconds"}, "to": {"type": "timestamp", "unit": "seconds"}}}
```

引用通常能推导来源类型,固定数字表示时间戳时需要 `from` 指明单位。
时间戳和时长在秒、毫秒之间换算时,整数结果保留完整精度,换算不受调用线程的
Decimal 精度和舍入设置影响。非整数结果仍使用浮点数。
转换选项包括 `rounding`、`timezone`、`encoding` 和 `allow_lossy`。
整数舍入支持 `exact`、`truncate`、`floor`、`ceil`、`round`,默认 `exact`。
`round` 在半值处向远离零的方向取整,`2.5` 得到 `3`,`-2.5` 得到 `-3`。
时区支持 `preserve`、`utc`、`local` 和可用的 IANA 时区名。
从时间戳或日期时间取 `time` 时,结果保留该日期在所选时区的 UTC 偏移,包括夏令时偏移。
二进制与文本之间可指定文本编码或 `base64`。
对象与文本默认使用 JSON;包含 `Decimal`、二进制等值时,往返双方指定
`encoding: "typed-v1"`。联合类型不能作为显式转换目标,需选择具体成员类型。

结构化文本拼接只接受文本项,数字等类型先转换为文本:

```json
{"$template": ["数量:", {"$convert": {"value": {"$ref": {"scope": "variable", "node": "v_count001", "path": []}}, "to": "text"}}]}
```

恰好只有 `$literal` 键的 `{"$literal": value}` 将 `value` 按固定值传递,其中的
`$ref`、`$convert` 和 `$template` 不会执行。同时含其他键的对象按普通对象解析。
自定义类型信封也不会被递归解释成表达式。

## 插件共享类型

插件在 `contributes.data_types` 中声明 `id`、正整数 `version`、`binding` 和
`schema`。默认 `binding: "private"`,跨插件传递必须显式声明 `shared`,并提供
有效的结构声明。现有 UIA 选择器与操作宏保持私有。

```json
{"id": "record", "version": 1, "binding": "shared", "label": "记录", "schema": {"type": "object", "properties": {"count": "int"}, "required": ["count"]}}
```

完整身份为 `package_name/record@1`,值使用 `{"$type": "完整身份", "data": ...}`,
可带不超过 160 字符的 `summary`。消费者在 `value_type` 中引用完整身份,也可在
类型结构中嵌套其他共享类型。数据字段通过引用路径 `["data", "count"]` 访问。
`["$type"]` 可读取类型身份,`["summary"]` 可读取可选的文本摘要。
同名但不同包或不同版本的类型互不替代。

类型从已校验的插件清单创建快照,单次运行使用同一份类型定义。类型缺失、禁用、
依赖缺失或版本不匹配时报告错误。读取已保存规则不进行自动类型升级或降级。
编辑器保留不可用类型的原始声明和值。

插件通过公开 API 访问校验、转换、类型注册和编解码:

```python
from notmyfault.plugin_api import data_types_api

types = data_types_api()
value = types.convert_value("9007199254740993", "int")
assert types.normalize_value(value, "int") == 9007199254740993
```

`TypeRegistry.from_plugins(...)` 从清单集合构建注册表,`make_value(identity, data)`
校验并包装共享值。`normalize_value`、`convert_value` 可传入该注册表。注册共享
类型不要求提供专用编辑器,标准结构化输入即可使用。

## 保存与传输

`int` 超过 JavaScript 精确范围、`Decimal`、二进制和 Python 日期时间等值采用
`$nmf_value` 编码。`int` 和 `decimal` 使用十进制文本,`bytes` 使用 Base64。
普通对象恰好只有 `$nmf_value` 键时会转义为 `object` 编码,往返保留原对象。
通用 JSON 编辑器保留完整编码对象;精确小数和二进制等专用编辑器显示对应的
十进制文本或 Base64,输入后恢复编码对象。
通用 JSON 编辑器先检查原始 JSON 语法,合法的大整数使用完整十进制文本保存。
小数或指数数字超出浮点数范围时提示错误,阻止保存规则;不会将无穷值保存为
`null`。修改为有效输入后可以继续保存。
手动测试中的完整数据、对象和数组输入使用相同的 JSON 解析规则,大整数保留
完整十进制文本,溢出数字和非法 JSON 会在发起测试前被拒绝。
类型结构 JSON 也使用相同解析规则。`min`、`max` 和 `enum` 中的大整数经编码
传输,保存后保留原值;溢出数字会提示错误并阻止保存。

规则文件使用 `schema_version: "2.1"` 与 `value_encoding: "typed-v1"`,先验签后解码。
隔离动作与 HTTP 返回值也使用同一编解码器。HTTP 请求带有
`X-NMF-Value-Encoding: typed-v1` 时解码请求体;没有该请求头时按普通 JSON 解释,
其他编码值返回错误。使用 `typed-v1` 编码的响应固定带有该请求头。
第三方客户端可通过公开 API 的 `encode_value`、`decode_value` 处理协议。

## 内置插件

文件相关插件声明路径与路径数组,计数和进程退出码声明整数,HTTP 输入声明网址。
USB 触发器的 `actual_drive` 是路径,`drive_letter` 是可含 `ANY` 的匹配文本。
文件操作目标和截图位置可留空;每日计划仍按本地小时和分钟执行。

文本读写、追加与内容监控接受 Python 文本编码名称。`text_transform` 提供
`split` / `join`;`url_codec` 提供 `compose` 并保留重复查询键;`datetime_format`
可选择 ISO 或明确为秒的时间戳输入。`file_info.modified_time` 为可空日期时间。
UIA 各操作的可选输出可以通过缺失策略绑定。

截图动作返回 `{"file": "截图路径"}`,`file` 的类型为路径。引用截图结果时选择
`file` 字段。
窗口置顶动作按平台返回可选的 Windows `hwnd` 整数或 Linux `window_id` 文本。
UIA 等待控件的可选 `bounds` 对象提供 `left`、`top`、`width`、`height` 整数字段,
控件没有可读取范围时对象为空,引用子字段需要设置缺失策略。

显示器控制提供 `action`,亮度操作另有 `brightness`,Windows 的亮度结果还包含
`methods` 和 `warnings` 文本数组。蓝牙控制提供 `action`,Windows 下另有包含
适配器 `name` 和 `state` 的 `radios` 对象数组;结束进程
提供整数 `killed` 和 `denied`。这些输出均可在后续动作或变量赋值中引用。
