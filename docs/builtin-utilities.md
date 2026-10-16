# 文件、数据处理与状态触发插件

这些内置插件位于 `notmyfault/actions/` 和 `notmyfault/triggers/`。
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
| `download_file` | 下载文件 | 从公网 HTTP/HTTPS 下载，返回 `file`、`bytes` 和 `status`。默认不覆盖，最多 100 MiB，下载总期限默认 15 秒。 |
| `archive` | 压缩与解压 | `zip` 压缩文件或文件夹，`unzip` 解压 ZIP；返回 `ok`、`archive`、`file_count` 等。路径无效时 `ok=false`。 |

文本文件读写支持 UTF-8、带 BOM 的 UTF-8、GBK 和 UTF-16。
文件动作和路径监视触发器保留路径首尾的空白字符，按实际路径读写或监视。
`archive` 压缩会保留空源目录。压缩包不能放在源目录内，源为盘符或文件系统根目录
时也遵循这条规则。已有目标若与任一源文件是同一文件，包括硬链接，压缩会在打开
输出前返回 `ok=false`，保留源内容。
压缩前收集文件时，目录遍历或文件状态读取失败会返回 `ok=false`，并保留已有压缩包。
压缩和解压在每个数据块读取前后检查规则取消或超时。正在执行的系统读写需等返回后
才能停止；已写出的内容保留，当前文件或压缩包可能不完整，覆盖过的内容不会恢复。
`archive` 解压默认保留已有文件并计入 `skipped`，选择 `overwrite` 后覆盖。
`overwrite` 必须是布尔值；字符串、数字和 `null` 会在解压前报错，不写入文件。
Windows 会把 ZIP 成员名中的 `:<>|"?*` 替换成下划线并去掉各路径段末尾的点；
是否覆盖和路径范围检查均使用改写后的路径，目录联接或符号链接指向解压目录外时拒绝解压。
`list_files` 的通配符匹配相对路径，`*` 可以跨过目录分隔符；递归时不进入符号链接
目录，Windows 目录联接仍可被遍历。子目录读取失败时整个查找报错，不返回不完整的成功结果。
`file_info.modified_time` 是带时区的 UTC 日期时间，路径不存在时为空。
路径中间不是目录时也返回 `exists=false`，权限不足则报错。

写文件、创建目录和下载动作声明了 `admin_key` 规则审批，沿用项目严格模式下的
私钥确认规则。读取、写入和创建目录的路径，以及下载地址和保存路径，都可以
引用触发数据或前序结果。`write_text` 的 `overwrite`、`create_parents` 和
`download_file` 的 `overwrite` 限定为规则中的字面量。参数是否固定以各插件
`security.literal_only_params` 为准，与是否要求规则审批分别判断。

下载最多跟随 5 次重定向，每个地址都执行公网检查，使用已检查的 IP 建立连接。
本机、内网和保留地址会被拒绝，包括代理使用的 `198.18.0.0/15` Fake-IP。
目标父目录必须已存在，内容下载完整后才写入目标路径。网络或大小检查失败时
删除临时文件，保留原目标文件。Linux 下默认不覆盖模式需要目标文件系统支持硬链接。
候选地址连接、重定向和响应体读取共用 `timeout_seconds` 的剩余时间；取消或
期限到达时中断活动连接。同步 DNS 查询不能中途结束，返回后才检查剩余时间。
DNS 查询期间收到取消信号时，返回后停止下载，不再建立连接。
HTTPS 的 TLS 握手也受总期限和取消控制，握手中断后关闭连接，不创建下载临时文件。
Content-Length 非整数、为负数或与实际下载长度不符时，下载失败。

## 数据处理

| 插件 ID | 名称 | 操作与主要输出 |
| --- | --- | --- |
| `text_transform` | 文本处理 | 去首尾空白、大小写转换、替换和分行；返回 `text`、`lines`、`length` 和 `line_count`。 |
| `json_data` | JSON 数据 | 解析、按 JSON Pointer 提取或序列化；返回保留原始类型的 `value`、JSON 字符串 `json` 和类型名 `type`。 |
| `csv_data` | CSV 数据 | CSV 文本和记录数组互转；返回 `records`、`columns`、`text` 和 `row_count`。 |
| `datetime_format` | 日期时间格式化 | 当前时间或指定 ISO 8601 时间的格式化与偏移；返回 `text`、`iso`、`timestamp`、`date` 和 `time`。 |
| `url_codec` | URL 编解码 | 百分号编码、解码、URL 解析和查询参数生成；返回 `text`、主机、端口、路径、查询对象和片段。 |

`text_transform` 的 `join` 操作接受绑定的文本数组，也接受在 `parts` 中填写的
JSON 文本数组，例如 `["a", "b"]`。`split` 的拆分结果位于 `lines`，`text`
保留原始输入；`join` 的连接结果位于 `text`。

`json_data.pointer` 使用 `/items/0/name` 格式，键中的 `/` 写成 `~1`，`~` 写成
`~0`。路径不存在时报错，JSON `null` 作为正常值返回。`stringify` 接受绑定的
数字、布尔、数组或对象；字符串输入会被序列化成 JSON 字符串。

`csv_data` 有表头时返回对象数组，无表头时返回二维数组。读取的单元格保留文本
类型，重复表头或列数不一致时报错。生成 CSV 时 `columns` 可以指定列顺序，
遗漏记录中已有的列会报错。单元格不能包含嵌套对象或数组。
解析时只移除文件开头的一个 BOM。生成时，首个列名或单元格以 `U+FEFF` 开头，
首行会加引号；直接解析或先用 `read_text` 默认编码读取后解析，都会保留该字符。
以 `U+FEFF` 为分隔符且首个单元格为空时，首行同样加引号，空单元格会保留。
解析支持超过 131072 字符的单元格。CSV 标准库的字段上限是进程共享设置，
解析时提高到当前平台接口可接受的最大值，解析后保留该设置。文件读取字节上限
和隔离进程的数据传输上限仍分别适用。

日期插件的 `timezone` 可选 `local`、`utc` 或 `preserve`；偏移单位为秒、分钟、
小时或天，一天按 24 小时计算。空时间取当前时间。

URL 查询对象保留同名参数的所有值，`form_mode` 控制空格与 `+` 的转换。
`compose` 接受 `scheme`、`hostname`、`port`、`path`、`query` 和 `fragment`。
路径和片段中的中文、空格、控制字符会转换为 UTF-8 百分号编码，已有的百分号编码
保持不变。路径中的 `?` 和 `#`、片段中的 `#` 也会编码，避免改变组成部分的含义。
没有协议和主机时，相对路径首段中的 `:` 编码为 `%3A`，避免被当作协议分隔符。
协议名可留空；填写时须以英文字母开头，后续只允许英文字母、数字、`+`、`-` 和 `.`。
输出的 `path` 和 `fragment` 保留编码形式。URL 插件本身不发网络请求。

## 状态触发

| 插件 ID | 名称 | 配置 |
| --- | --- | --- |
| `path_exists` | 路径出现或消失 | `path` 指定路径；`kind` 可选文件、目录或任意类型；`state` 可选 `exists`、`missing`、`changed`。 |
| `file_content` | 文件文本状态变化 | `path` 指定文本文件，`text` 指定查找内容；`state` 可选 `contains`、`absent`、`changed`，可设置大小写和编码。 |
| `tcp_port` | TCP 端口状态变化 | `host` 支持主机名、IPv4 和 IPv6，`port` 指定端口；`state` 可选 `reachable`、`unreachable`、`changed`。 |
| `directory_quiescent` | 目录静默 | `folder_path` 指定目录；`quiet_seconds` 为无变化时长；`state` 可选 `quiescent`、`active`、`changed`；可用 `file_pattern` 过滤文件名。 |

前三个触发器首次成功采样只记录状态，之后在所选状态变化时触发，相同状态不重复
触发。所选变化的事件提交抛异常时保留上次状态；后续采样仍显示该变化时再次提交，
成功后才记录新状态。`directory_quiescent` 从启动起计时，目录在 `quiet_seconds` 内无匹配变化
后进入静默；被变化打断后可再次进入静默。`interval` 控制检查间隔，可设为
0.2–3600 秒。TCP 支持域名，只建立连接，不发送应用数据，连接超时可设为
0.1–10 秒。域名解析不受该连接超时限制，停止触发器后不再使用尚未返回的解析结果。

文件内容触发器默认最多读 1024 KiB，支持 UTF-8、带 BOM 的 UTF-16 和 GB18030。
文件缺失、不可读、超限或编码错误时保留上次成功读取的状态；需要检测文件消失
时使用 `path_exists`。下载完成或复制结束后再处理文件，可以用
`directory_quiescent`；需要同时确认文件未被占用和文档窗口已关闭时，用动作
`document_quiescent` 检查。

`document_quiescent` 作为普通动作执行，检查未通过时动作报错。首次执行记录目录
快照并报错；后续执行在快照未变且已达到 `quiet_seconds` 后，再按选项检查文件占用
和文档编辑窗口。宿主不因这个动作自动延后工作流。静默时长须为有限数字，负数按 0 处理。
目录遍历或文件状态读取失败时，检查未通过并保留上次完整快照，不把不完整结果当作静默。

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
