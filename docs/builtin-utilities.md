# 文件、数据处理与状态触发插件

这 15 个内置插件位于 `notmyfault/actions/` 和 `notmyfault/triggers/`。
在规则编辑器中选择对应的动作或触发条件，填写参数即可使用。数据动作返回的
字段可以通过 `$ref` 传给后续动作，敏感文本不会显示在执行摘要中。

## 文件与剪贴板

| 插件 ID | 名称 | 行为和主要输出 |
| --- | --- | --- |
| `read_text` | 读取文本文件 | 保留原始换行，返回 `text`、`bytes`、`characters` 和 `file`。默认 UTF-8 去 BOM、最多 1 MiB，超限报错。 |
| `write_text` | 写入文本文件 | 保存文本，包括空字符串。返回 `file`、`bytes` 和 `characters`；默认不覆盖已有文件，可选择创建父目录。 |
| `list_files` | 列出文件 | 通过 `directory`、`pattern` 和 `recursive` 筛选文件，返回 `files`、`count` 和 `truncated`；默认最多 1000 项，支持取消。 |
| `file_info` | 读取文件信息 | 返回是否存在、是否为文件或目录、扩展名、字节数和 UTC 修改时间。路径不存在时 `exists=false`。 |
| `create_directory` | 创建目录 | 创建缺少的父目录，返回 `path` 和 `created`；已有目录返回 `created=false`，同名文件报错。 |
| `clipboard_read` | 读取剪贴板 | 返回 `text`、`length`、`has_text` 和 `truncated`。只读取文本，默认最多 1000000 字符。 |
| `download_file` | 下载文件 | 从公网 HTTP/HTTPS 下载，返回 `file`、`bytes` 和 `status`。默认不覆盖，最多 100 MiB，网络超时 15 秒。 |

文本文件读写支持 UTF-8、带 BOM 的 UTF-8、GBK 和 UTF-16。
`list_files` 的通配符匹配相对路径，`*` 可以跨过目录分隔符；递归时不进入符号链接
目录，Windows 目录联接仍可被遍历。

写文件、创建目录和下载动作声明了 `admin_key` 规则审批，沿用项目严格模式下的
私钥确认规则。它们的目标路径及覆盖选项限定为规则中的字面量，文本内容可以
引用前序结果。下载地址也限定为字面量。

下载最多跟随 5 次重定向，每个地址都执行公网检查，使用已检查的 IP 建立连接。
本机、内网和保留地址会被拒绝，包括代理使用的 `198.18.0.0/15` Fake-IP。
目标父目录必须已存在，内容下载完整后才写入目标路径。网络或大小检查失败时
删除临时文件，保留原目标文件。Linux 下默认不覆盖模式需要目标文件系统支持硬链接。
网络连接和单次读取受 `timeout_seconds` 限制，规则取消会在每次读取之间检查。

## 数据处理

| 插件 ID | 名称 | 操作与主要输出 |
| --- | --- | --- |
| `text_transform` | 文本处理 | 去首尾空白、大小写转换、替换和分行；返回 `text`、`lines`、`length` 和 `line_count`。 |
| `json_data` | JSON 数据 | 解析、按 JSON Pointer 提取或序列化；返回保留原始类型的 `value`、JSON 字符串 `json` 和类型名 `type`。 |
| `csv_data` | CSV 数据 | CSV 文本和记录数组互转；返回 `records`、`columns`、`text` 和 `row_count`。 |
| `datetime_format` | 日期时间格式化 | 当前时间或指定 ISO 8601 时间的格式化与偏移；返回 `text`、`iso`、`timestamp`、`date` 和 `time`。 |
| `url_codec` | URL 编解码 | 百分号编码、解码、URL 解析和查询参数生成；返回 `text`、主机、端口、路径、查询对象和片段。 |

`json_data.pointer` 使用 `/items/0/name` 格式，键中的 `/` 写成 `~1`，`~` 写成
`~0`。路径不存在时报错，JSON `null` 作为正常值返回。`stringify` 接受绑定的
数字、布尔、数组或对象；字符串输入会被序列化成 JSON 字符串。

`csv_data` 有表头时返回对象数组，无表头时返回二维数组。读取的单元格保留文本
类型，重复表头或列数不一致时报错。生成 CSV 时 `columns` 可以指定列顺序，
遗漏记录中已有的列会报错。单元格不能包含嵌套对象或数组。

日期插件的 `timezone` 可选 `local`、`utc` 或 `preserve`；偏移单位为秒、分钟、
小时或天，一天按 24 小时计算。空时间取当前时间。URL 查询对象保留同名参数的
所有值，`form_mode` 控制空格与 `+` 的转换；URL 插件本身不发网络请求。

## 状态触发

| 插件 ID | 名称 | 配置 |
| --- | --- | --- |
| `path_exists` | 路径出现或消失 | `path` 指定路径；`kind` 可选文件、目录或任意类型；`state` 可选 `exists`、`missing`、`changed`。 |
| `file_content` | 文件文本状态变化 | `path` 指定文本文件，`text` 指定查找内容；`state` 可选 `contains`、`absent`、`changed`，可设置大小写和编码。 |
| `tcp_port` | TCP 端口状态变化 | `host` 支持 IPv4、IPv6 和 `localhost`，`port` 指定端口；`state` 可选 `reachable`、`unreachable`、`changed`。 |

三个触发器首次成功采样只记录状态，之后在所选状态变化时触发，相同状态不重复
触发。`interval` 控制检查间隔，可设为 0.2–3600 秒。TCP 不支持域名，只建立
连接，不发送应用数据，连接超时可设为 0.1–10 秒。

文件内容触发器默认最多读 1024 KiB，支持 UTF-8、带 BOM 的 UTF-16 和 GB18030。
文件缺失、不可读、超限或编码错误时保留上次成功读取的状态；需要检测文件消失
时使用 `path_exists`。

## 动作结果引用

假设读取文本动作的 `binding_id` 为 `a_readtext`，CSV 解析动作的 `value` 填入：

```json
{
  "$ref": {
    "scope": "step",
    "node": "a_readtext",
    "path": ["text"]
  }
}
```

将 CSV 动作的 `records` 传给 `json_data.value`，选择 `extract`，路径设置为
`/0/name`，即可提取首行的 `name`。返回的 `value` 可以接到文本处理动作，
最后将文本动作的 `text` 交给 `write_text` 或现有的通知动作。

文件出现后读取并处理内容，可以用 `path_exists` 接上述动作；日志写入指定文字
后发送通知，可以用 `file_content` 接现有 `notify`；服务恢复后执行动作，可以
用 `tcp_port` 的 `reachable` 状态。
