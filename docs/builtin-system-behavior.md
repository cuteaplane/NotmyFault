# 内置系统动作与状态触发器

这些行为对应 `notmyfault/actions/`、`notmyfault/triggers/` 和
`notmyfault/platform/` 的当前实现。

## 执行期限与取消

`run_powershell`、`run_shell`、`http_request`、`bluetooth_toggle`、`kill_process`、`wait` 声明
`execution_api: context-v1` 和 `cancellation_api: runtime-v1`，可以接收规则的
协作取消信号。规则超时仍需等待插件退出正在执行的系统调用。

| 动作 | 执行限制 | 取消行为 |
| --- | --- | --- |
| `run_powershell` | Windows；命令执行最多 60 秒；stdout、stderr 各保留最多 100 KiB，超出部分丢弃并标注截断。 | 检查间隔最多约 0.1 秒，超时或取消时终止直接启动的 PowerShell 进程；正常退出后排空可用的尾部输出，未读完时标注截断。不会自动终止所有后代进程。 |
| `run_shell` | Linux；通过 bash 或 sh `-c` 执行，限制与 `run_powershell` 相同。 | 与 `run_powershell` 相同，终止的是直接启动的 shell 进程。 |
| `http_request` | `timeout_seconds` 默认 30 秒，范围 1–300 秒。候选 IP 连接、请求和正文读取共用期限；正文最多 1 MiB。 | 取消或期限到达时中断活动 socket。同步 DNS 调用不能中途终止，返回后才检查期限。 |
| `bluetooth_toggle` | Linux 的状态查询、修改和确认共用 30 秒期限，单次 bluetoothctl 命令最多 8 秒；Windows helper 最多 30 秒。 | 等待命令时每次最多约 0.1 秒检查取消，取消后终止并回收直接启动的辅助进程；状态确认等待也可取消。 |
| `kill_process` | 全部匹配进程共用 10 秒期限，不会给每个进程重新分配 10 秒。 | 进程等待分成最多 0.1 秒的小段，段间检查取消。已经终止的进程不会恢复。 |
| `wait` | `seconds` 与 `milliseconds` 叠加，合计 0–86400 秒；跨平台。 | 使用取消对象分段等待，规则停止或超时可中途结束。 |

表中除 `wait`（`idempotent: true`）外均声明 `idempotent: false`。请求取消或超时不会撤销此前已经发生的命令、
HTTP 请求、蓝牙状态变更或进程终止。强杀后未在期限内确认退出的进程会明确报错。

`run_powershell.encoding` 用于解码标准输出和标准错误，默认 `oem`，对应 Windows
系统 OEM 代码页，不随 Python 的 UTF-8 模式改变。命令主动使用其他输出编码时，
填写对应编码，例如 `utf-8` 或 `utf-16-le`。无效编码和非文本编码在启动命令前报错。
编码参数不修改命令内容或 PowerShell 的输出编码设置。
`run_powershell` 和 `run_shell` 拒绝空字符串及只含空白字符的命令，其他命令原样
交给对应解释器，保留首尾空格、制表符和换行。

`http_request` 支持 GET、POST、PUT、DELETE、PATCH 和 HEAD，不跟随重定向，
3xx 响应作为失败返回。无效或超出范围的超时参数会报错。
请求保留 URL 路径中的分号参数和查询参数，不发送 `#` 后的片段。
正文声明了 Content-Length 却在收足之前断流时，动作报错，不返回残缺的成功结果。
HEAD 和 204 响应按协议没有正文；超过 1 MiB 的响应仍只返回前 1 MiB 并标记
`truncated=true`，不继续读取剩余正文。
DNS 查询期间收到取消信号时，查询返回后检查取消，不再建立请求连接。
HTTPS 在 TLS 握手前记录正在使用的 socket；握手期间收到取消信号或超过总时间限制时，也会关闭连接。

## 等待

`wait` 暂停当前规则运行指定时长，再继续后续动作。参数 `seconds` 与
`milliseconds` 可同时填写并叠加。Dashboard 在工具栏提供独立的「等待」块，
不出现在通用动作插件选择器中；执行仍走普通内置动作。

## 环境变量

`environment_variable` 只读写当前 NotmyFault 进程的环境变量，会影响本程序以及
之后由本程序启动的子进程；不会写入系统或用户级永久配置。界面名称是“环境变量”。
跨平台，声明 `native_api` 权限，`idempotent` 为 false。
Windows 设置和删除时同步 Python 环境映射与进程环境块；空值会被新子进程继承为
空字符串，删除后新子进程不再包含该变量。

| 操作 | `mode` | 说明 |
| --- | --- | --- |
| 读取 | `get` | 输出 `name`、`value`、`existed`。`missing` 为 `empty`（默认）时，不存在则 `value` 为空且 `existed` 为 false；为 `error` 时报“环境变量不存在”。 |
| 设置 | `set` | 参数 `value` 可为空字符串。输出 `name`、`value`、`existed`（原先是否已有）。 |
| 删除 | `unset` | 输出 `name`、`existed`（原先是否存在）。不存在时不报错。 |

变量名不能为空，首尾不能有空格，也不能包含等号或空字符。
并行规则删除同一变量时，读取返回已取得的值或按 `missing` 处理不存在的变量；
删除操作在变量已不存在时返回 `existed=false`。

## Windows 系统资源

`screenshot` 的全屏模式覆盖整个虚拟桌面，包括主屏左侧或上方显示器的负坐标；
活动窗口模式使用所选窗口的矩形。Windows 格式支持 png、jpg、jpeg 和 bmp，
始终写入指定路径，不按扩展名删除或改名。非 BMP 格式需要 Pillow，缺失时在
写入前报错。

剪贴板动作从打开到关闭剪贴板全程持有原生调用锁。显示器电源操作使用异步窗口
广播，发送成功表示广播已提交，不表示每个显示器都已经完成切换。

`shutdown_system.force` 用于 Windows 关机、重启或注销时强制关闭应用。
睡眠和休眠不使用该选项：Windows 的 `SetSuspendState` 参数 bForce
[没有实际作用](https://learn.microsoft.com/en-us/windows/win32/api/powrprof/nf-powrprof-setsuspendstate)。
Linux 注销优先使用 `XDG_SESSION_ID`，缺失时通过 loginctl 解析当前用户会话，只终止该会话。Windows 关机权限启用失败会
直接报告原因。

`media_control` 的“播放/暂停”切换播放状态；“静音”将静音设为开启，已经静音
时保持静音。Linux 静音保留原音量。

## WiFi 连接与默认音频设备

`wifi_connect` 连接或断开指定 SSID。Windows 使用 `netsh wlan`，只能连接系统里
已经保存过的网络配置；Linux 使用 `nmcli`，已保存的连接走 `connection up`，新网络
可附带密码调用 `device wifi connect`。连接和断开后都会回读当前 SSID，超时未达
到目标状态则报错。密码参数在 Windows 上不会用来新建配置。
Wi-Fi 动作和触发器保留 SSID 中的首尾空格、全角空格和引号，非空的纯空格 SSID
也按原文匹配。Windows 当前连接状态通过 WLAN API 查询，按 SSID 字节长度读取
UTF-8 名称；查询失败返回错误，不视为断开。Windows 通过 WLAN API 读取已保存配置中的
实际 SSID，将匹配的配置名称和所属网卡名称传给 `netsh wlan connect`，并按网卡 GUID
回读连接前后的 SSID。查询不请求明文密钥，不导出配置文件。Linux 读取已保存无线配置
中的实际 SSID，匹配后按 UUID 激活。两端的配置名称都可以与 SSID 不同。
SSID 中的冒号和反斜杠按原文匹配；
与目标 SSID 同名但指向其他网络的配置不会被选中。配置列表或详情查询失败时直接
报错，不把查询失败当作没有已有配置。
Linux 的 Wi-Fi 动作和触发器调用 `nmcli` 时，将子进程的 `LC_ALL` 设为 `C`，
按固定的英文状态值解析连接状态。其余环境变量保留，宿主进程的语言设置不变。
Linux 断开动作操作所选 Wi-Fi 网卡，并按同一接口回读状态；结果中的
`connected=false` 表示该接口已断开，其他网卡可以继续连接。状态查询失败时动作报错。

`set_default_audio` 切换默认播放或录音设备。参数 `device` 可填设备 id 或友好名；
Windows 通过 PolicyConfig 设置端点，Linux 通过平台服务调用 wpctl `set-default`
或 pactl `set-default-sink` / `set-default-source`。设备 id 可与 `audio_device`
触发器输出对应。设置后回读默认设备，不一致则报错。

Windows 会同时设置通用、多媒体和通信三个角色。只有三个角色已经指向目标设备时，
才返回 `changed=false`；设置后逐一回读，任一角色不一致则报错并指出该角色。
`previous_device_id` 保留切换前通用角色的设备 id。

## 窗口控制

`window_control` 在 Windows 上通过窗口接口操作顶层窗口，支持 Windows 10 1703 及以上版本；
在 Linux 上通过 wmctrl、xprop、xdotool、xrandr 操作 X11 顶层窗口。Wayland 会话不可用，
会明确报错。界面名称为“窗口控制”。

| 操作 | `action` |
| --- | --- |
| 查询窗口和显示器 | `list`、`get_info`、`list_monitors` |
| 激活、最小化、最大化、还原 | `bring_to_front`、`minimize`、`maximize`、`restore` |
| 显示与隐藏 | `show`、`hide` |
| 移动、缩放、同时移动和缩放 | `move`、`resize`、`move_resize` |
| 居中、半屏或四分之一屏、跨显示器移动 | `center`、`snap`、`move_to_monitor` |
| 置顶、取消置顶、切换置顶 | `pin`、`unpin`、`toggle_pin` |
| 设置不透明度、正常关闭 | `set_opacity`、`close` |

`target` 默认 `active`，也可使用 `title`、`process`、`pid`、`class_name`、`hwnd`、
`all`。标题和窗口类名支持包含、完全匹配和正则表达式，均不区分大小写。
进程名始终按完整名称匹配，可以省略 `.exe`。
默认只查找可见窗口；`include_hidden: true` 包含隐藏窗口，按句柄查找也允许隐藏窗口。

`match` 默认 `unique`，多个窗口匹配时报错。`first` 选择窗口层级顺序中的首个结果，
`all` 操作全部匹配窗口。`list` 返回全部匹配结果，`get_info` 使用 `match` 规则。
前台激活只接受一个窗口。

`wait_seconds` 为窗口出现的等待时间，默认 0，范围 0–60 秒。
`timeout_seconds` 为查询后所有窗口共用的操作时限，默认 5 秒，范围 0.1–60 秒。
等待期间支持规则取消；已经发生的操作不会撤销。
某个窗口操作失败时停止后续窗口操作，此前已完成的窗口保持修改后的状态。

位置和尺寸使用物理像素，窗口矩形包含边框，允许多显示器的负坐标。
`snap` 按显示器工作区计算半屏或四分之一屏；`center` 和 `move_to_monitor`
在目标工作区内居中，窗口大于工作区时缩小到工作区尺寸。
最小化或最大化的窗口会先还原，再调整位置与尺寸。
显示器可选当前、主屏、上一台、下一台或指定序号。主屏序号为 1，其他显示器按
横坐标、纵坐标排序。显示器布局变化后应重新查询序号。

`opacity` 范围 0–100，100 表示完全不透明；完全透明的窗口可以通过句柄恢复。
`close` 发送正常关闭消息并等待窗口消失，应用可能弹出保存确认，此时需要先处理
确认框；插件不会强制终止进程。
[Windows 的前台激活限制](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)
同样适用于 `bring_to_front`，系统拒绝激活时动作报错。

返回值包含 `action`、`count`、`hwnd`、`windows`、`monitors`。
只有一个窗口结果时 `hwnd` 是该窗口句柄，否则为 0。
`windows` 包含标题、进程名、PID、窗口类、位置、尺寸、可见性、最小化、最大化、
置顶状态和不透明度；正常关闭后附带 `closed: true`，其余字段保留关闭前的值。
无法读取进程名时该字段为空字符串；无法读取分层窗口的不透明度时该字段为 null。
`monitors` 包含显示器序号、设备名、主屏标记、完整矩形和工作区矩形。

例如，按标题选择首个 Chrome 窗口并切到前台：

```json
{
  "type": "window_control",
  "params": {
    "action": "bring_to_front",
    "target": "title",
    "title": "Chrome",
    "match": "first"
  }
}
```

## Linux 桌面后端

Wayland 热键与鼠标通过 ydotool 发送，需要可工作的 ydotoold 服务及权限。未设置
`YDOTOOL_SOCKET` 时会尝试 `$XDG_RUNTIME_DIR/.ydotool_socket`、
`$XDG_RUNTIME_DIR/ydotool.socket` 和 `/tmp/.ydotool_socket`；连接失败的报错会带上当前套接字路径。
X11 仍优先使用 xdotool。未支持的键名会在发送前报错。F1–F24 可用于 Linux 按键发送。
鼠标绝对移动在部分 Wayland 合成器上可能受加速或缩放影响。

显示器开关在 GNOME 走 `org.gnome.ScreenSaver.SetActive`，在 KDE 走
`org.kde.screensaver` / `org.freedesktop.ScreenSaver.SetActive`，在 X11 其他桌面走
`xset dpms force`。非 GNOME/KDE 的 Wayland 会话没有可用接口，能力探测与执行都会明确报错。

空闲时间优先读 GNOME Mutter IdleMonitor，其次 KDE/freedesktop ScreenSaver 的
`GetSessionIdleTime`，再回退到 X11 的 xprintidle。能力 id 为 `idle.query`。
媒体控制能力 id 为 `media.control`，Linux 依赖 playerctl；静音仍走音频后端。

Linux 锁屏状态与注销在缺少 `XDG_SESSION_ID` 时，会用 loginctl 查找当前用户的活动会话。

`window_control` 在 Linux 上需要 wmctrl 和 xprop；列出显示器、居中和分屏还需要
xrandr。隐藏窗口优先使用 xdotool 的 windowunmap。不透明度在 Linux 上不可用。
进程名按 psutil 返回的名称匹配，不自动追加 `.exe`。显示器工作区目前等同于
xrandr 给出的整屏矩形。

`window_pin` 需要 wmctrl 和 xprop。活动窗口来自 `_NET_ACTIVE_WINDOW`；按标题
查找只匹配标题字段。操作后读回置顶状态，最多等待 1 秒。Windows 成功结果包含
`hwnd`，Linux 成功结果包含 `window_id`，规则应引用当前平台返回的字段。
`window_title` 与 `window_pin` / `window_control` 共用 `WindowBackend` 枚举：
优先 wmctrl，否则回退到 xdotool。

Wayland Portal 截图在请求前监听响应，等待最多 120 秒。未完成的请求在超时、
取消或错误时尝试关闭；用户拒绝授权会报告失败。能力报告中的 `degraded: true`
表示本地依赖存在，Portal 服务或截图授权仍需执行时检查。

GNOME 壁纸设置会先确认 `picture-uri-dark` 键是否存在；不支持该键的版本只
设置普通壁纸。Plasma 当前只接受 fill 样式，其他样式报错。未知样式在执行前拒绝。

Windows 的剪贴板、输入、窗口、截图和锁定能力检查所需 DLL 导出是否存在。
音量、语音和托盘检查相应 Python 模块；音频设备查询检查 pycaw 和 comtypes，
通过 CoreAudio 的 MMDeviceEnumerator 查询默认端点。蓝牙检查 Windows PowerShell
和 WinRT 入口。亮度可使用 WMI 或 DDC/CI，任一依赖可用即可。只有 WMI 可用时
标记 `degraded: true`，DDC/CI 可用时为 `false`。
设备状态和访问权限仍在执行时检查，相关说明保留在 `reason`；其他上述 Windows
能力通过依赖检查后标记 `degraded: false`。

## 语音播报

`text_to_speech` 在 Linux 上使用 `spd-say`。正文作为文本参数传递，
以 `-` 开头的正文也不会作为选项执行，例如 `--version`、`--help` 和 `-C`。
`rate` 转换为 `spd-say` 的语速百分比，`volume` 转换为相对默认音量的百分比。
`voice` 为语言代码时传给 `--language`，Windows 声音名称不传给 Linux 后端。
Windows 使用独立子进程调用 SAPI，通过 UTF-8 JSON 传递正文和声音参数。
`rate` 和 `volume` 必须是有限数字。解析引用后仍会校验类型，布尔值、字符串和
`null` 会在启动播报后端前报错。

## 文件与链接动作

文件动作、快捷方式目标、截图保存路径和壁纸路径保留首尾的空白字符。
Windows 快捷方式使用 Unicode ShellLink 接口，保留名称、目标路径、参数、工作目录
和图标路径中的 Unicode 字符。返回的 `shortcut_path` 使用 UTF-8 传回 Python，
创建或保存失败时动作报错。

`append_text` 展开路径中的 `~`，相对路径按进程工作目录转换为绝对路径。编码
可填写 Python 支持的名称，无效名称会报错。`add_timestamp` 必须是布尔值，
字符串 `"false"` 不能用作关闭开关。
使用 `utf-16` 或 `utf-32` 追加到非空文件时，按文件开头的 BOM 选择字节序，
保留原内容和 BOM。非空文件没有对应 BOM 时会报错；无 BOM 文件应填写明确的
`utf-16-le`、`utf-16-be`、`utf-32-le` 或 `utf-32-be`。使用通用 `utf-16` 或 `utf-32`
写入新文件或空文件时，使用 Python 编码器的默认字节序，只在开头写入一次 BOM。

`append_text` 保留空行。启用时间戳时，只把形如 `[2026-09-06 12:30:00]` 的
完整前缀视为已有时间戳，其他以 `[` 开头的非空行仍会添加时间戳。

Linux 快捷方式将 `working_directory` 写入 `.desktop` 文件的 `Path`，将
`icon_path` 写入 `Icon`；空格和控制字符按 Desktop Entry 字段规则转义，保留路径中的
前导空格。参数按 Exec 规则转义，保留引号内的制表符和字面百分号。

## 压缩与解压

`archive` 只处理 ZIP：`mode=zip` 把文件或文件夹压成 `.zip`，`mode=unzip` 解压到目录。
界面名称为“压缩与解压”。源路径、目标路径可用字面量或绑定；`mode` 与 `destination`
限定为规则中的字面量，并声明 `admin_key` 规则审批。

压缩时未填目标路径则在源路径后追加 `.zip`；解压未填目标则使用 `源路径_extracted`。
源不存在、不是 ZIP、成员路径越界或磁盘错误时返回 `ok=false` 与 `error` 说明，不抛给引擎
当作未捕获异常。解压拒绝符号链接式的 `..` 与绝对路径成员。只使用标准库 `zipfile`，
不含 tar。

压缩和解压在每个数据块读取前后检查规则取消或超时，并向引擎报告对应状态。
正在执行的系统读写需等返回后才能停止。取消不回滚已写出的内容，当前文件或
压缩包可能不完整，覆盖过的内容不会恢复。

与 `file_operation` 的 compress/extract 分工：`archive` 面向日常 ZIP 压缩解压并软失败；
`file_operation` 仍覆盖复制、移动、删除以及 zip/tar 解包。

`file_operation` 的 copy、move 必须提供非空目标路径，move 会创建缺少的父目录。
两者默认拒绝覆盖已有目标，覆盖需显式设置 `overwrite=true`；该开关必须是字面量。
move 的目标为已有目录时，将源文件或目录移到该目录下，并按同名子路径判断覆盖。
允许覆盖时，同名普通目录递归合并，同名文件替换，目标中不冲突的文件保留。
文件或链接与目标目录冲突时会报错。源目录在内容全部移走后删除；中途失败时，
已移走的内容保留在目标，其余内容留在源目录。
源路径为盘符或文件系统根目录时，同样拒绝向该根目录内复制、移动或写入压缩包。
`file_operation` 压缩前检查已有输出是否与源文件或源目录内将被压缩的文件指向同一
文件，包括硬链接；重合时报错，保留源内容。
ZIP 解包在写出前检查完整成员列表，同时检查原始成员名和实际解压路径。
Windows 文件名改写、移除路径中的 `..` 后指向目录外的联接或符号链接会被拒绝；
改写后没有文件名的普通文件成员也会被拒绝。
tar 解包先检查完整成员列表，仅接受普通文件和目录，拒绝链接、设备、FIFO 和
其他特殊成员。非法归档不写出成员文件，但可能保留已经创建的空目标目录。
磁盘读写失败不提供事务回滚。

`open_url` 只接受 HTTP(S)，先验证整个 URL 列表，再打开浏览器。无协议的域名
或主机端口地址补 `https://`，`mailto:` 等自定义协议会被拒绝。
`new_window` 必须是布尔值，非法参数在启动浏览器前报错。`launch_program`
保留程序路径和工作目录首尾的空白字符；工作目录为空时使用当前目录。
只在 Windows 返回“不是有效的可执行程序”错误 193 时尝试文件关联启动，并传递原有参数和
工作目录；路径不存在、权限不足等错误直接报告失败。

`document_quiescent` 的 `check_file_locks` 和 `check_document_windows` 必须是布尔值，
默认均为 `true`。字符串、数字和 `null` 会使前置检查返回 `ok=false`，作为动作运行时会报错。

## 状态触发器

`rule_finished` 在指定规则的一次运行结束时触发，可筛选任意结束、仅成功、仅已补救、
仅失败（含超时）、仅取消、仅丢弃或被替换。跨平台，不依赖系统原生接口。Dashboard 用规则列表选择监视目标，
并自动写入所属规则 id；默认不允许监视本规则自己。触发器记住最近 256 个成功发送的
`run_id`，忽略这些运行的重复通知。正在发送的运行另行记录，其并发通知和回调内的
重复通知也会被忽略。发送回调抛异常时不记为成功，之后再次收到该运行的通知可重新发送；
触发器不会自行重试。
`include_self` 必须是布尔值，只有显式 `true` 才允许响应自身结束事件。
从常量解析出的非法开关会在订阅引擎事件前报错。
规则 A 结束触发规则 B、B 再触发 A 仍可能形成环，配置时需要自行避免。


`network_status` 检查指定 TCP 端口的可达性，不表示所有互联网服务可用。
`host` 默认 `8.8.8.8`，`port` 默认 `53`，`timeout` 默认 2 秒，可设为
0.1–10 秒。企业网络可以设置能够代表所需服务的探测端点。
探测支持 IPv4、IPv6 和主机名；主机名解析出多个地址时依次尝试，任一地址连接成功
即视为可达。初次采样只记录状态，后续才报告所选方向的变化。
系统域名解析不受连接超时控制。每个实例在独立线程中等待一次采样，等待期间
仍检查停止信号，不启动更多采样。停止触发器时不等待系统解析完成，之后返回的
采样结果会被丢弃。

`folder_monitor` 默认只监听 created，按文件大小和纳秒修改时间比较完整快照；
实际时间戳精度由文件系统决定。目录缺失、不可读或成员 stat 失败会使整次采样
失败，保留已记录的文件状态。恢复读取后再比较变化，不把读取错误当成所有文件被删除。
事件逐条提交，成功后记录该文件的变化。后续提交抛异常时，下一轮重新比较未记录的
变化，已成功提交的同一次变化不会重复发送。

`directory_quiescent` 用同样的大小与纳秒修改时间快照监视目录；在设定的
`quiet_seconds` 内没有匹配文件增删改时进入静默并触发。静默被新的变化打断时可
再触发。`state` 可选 `quiescent`、`active`、`changed`，默认只报告进入静默。
`file_pattern` 与 `folder_monitor` 相同，Windows 下不区分大小写。检查间隔
`interval` 可设为 0.2–3600 秒，默认 2 秒。读取失败时保留上次完整快照与静默
进度。状态事件提交抛异常时保留上次状态；后续采样仍显示该变化时再次提交，成功后
才记录新状态与快照。与动作 `document_quiescent` 不同：本触发器不检查文件占用或文档编辑窗口。


Wi-Fi 的 disconnected 包括从指定网络直接切换到其他网络。电源状态查询失败
或返回未知哨兵时不会触发状态变化；resume 在 Windows 监听电源广播，在 Linux 监听 logind PrepareForSleep(False)。低电量在 20%
及以下触发后，需要接通电源或电量超过 25% 才恢复下一次触发。

Windows 默认音频设备使用 CoreAudio 查询，设置默认端点走 PolicyConfig；锁屏状态通过 psutil 读取 LockApp/logonui。
Linux 默认音频设备查询与切换走 wpctl 或 pactl；锁屏状态读取 loginctl LockedHint。锁屏需连续两次采样确认变化，采样间隔为五秒。

剪贴板事件的 `text` 是完整内容，空闲事件的 `idle_seconds` 是本次测得的秒数。
窗口标题事件保留配置原文，匹配忽略大小写；Linux 查询需要 X11，查询失败不会
被当作窗口关闭。

`battery_level.threshold` 和 `system_resource.threshold` 必须是有限数字；
常量中的布尔值、字符串和 `null` 会使规则准备失败。
`system_resource` 的磁盘监控可填写 `disk_path`，省略或留空时 Windows 使用系统盘
根目录，Linux 使用 `/`；非空路径保留首尾的空白字符。
采样失败保留上次触发状态，恢复采样不会重复上报同一状态。

`cron_schedule` 的 daily/weekly 和 `time_schedule` 不补发启动前错过的当天
计划；已运行的触发器跨过当天计划分钟时会触发。`cron_schedule` 的 interval 模式
从首次轮询起用单调时钟计算分钟间隔，系统校时不改变到期判断。到期后的首次轮询
发送一次事件，再开始下一段间隔；轮询长时间中断后不补发多次事件。
`triggered_at` 仍记录触发时的本地日期时间。`system_startup.started_at`
是触发器启动时间，重新启用或重启后重新计时；延迟使用单调时钟。

USB 触发器在 Windows 接受 E、E:、E:\ 和 E:/，在 Linux 接受区分大小写的
绝对挂载路径，并保留挂载路径首尾的空白字符；ANY 匹配全部挂载点。
首次采样只记录已有设备。后续采样记录设备移除，并逐条提交新设备的插入事件。
提交抛异常时保留已成功提交的设备记录；下一轮仍存在的未提交设备会重试，已移除
设备重新插入时仍会触发。设备查询失败时保留此前记录。

热键配置和录制至少包含一个修饰键；录制忽略 CapsLock、NumLock、ScrollLock，
Escape 取消。Linux 热键录制需要 X11。

轮询持续发生同类型、同消息错误时，最多每 60 秒记录一次堆栈；成功采样后恢复
错误记录计时。音频、锁屏、Wi-Fi 等查询失败会报告错误，不再静默保持现状。

轮询触发器在部分初始化失败后仍执行清理；状态变化在事件成功提交后才记录为
已触发。各配置仍单独采样，没有跨规则共享目录扫描或进程扫描。
