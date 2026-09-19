# 内置系统动作与状态触发器

这些行为对应 `notmyfault/actions/`、`notmyfault/triggers/` 和
`notmyfault/platform/` 的当前实现。

## 执行期限与取消

`run_powershell`、`http_request`、`bluetooth_toggle`、`kill_process` 声明
`execution_api: context-v1` 和 `cancellation_api: runtime-v1`，可以接收规则的
协作取消信号。规则超时仍需等待插件退出正在执行的系统调用。

| 动作 | 执行限制 | 取消行为 |
| --- | --- | --- |
| `run_powershell` | 命令执行最多 60 秒；stdout、stderr 各保留最多 100 KiB，超出部分丢弃并标注截断。 | 检查间隔最多约 0.1 秒，超时或取消时终止直接启动的 PowerShell 进程；正常退出后排空可用的尾部输出，未读完时标注截断。不会自动终止所有后代进程。 |
| `http_request` | `timeout_seconds` 默认 30 秒，范围 1–300 秒。候选 IP 连接、请求和正文读取共用期限；正文最多 1 MiB。 | 取消或期限到达时中断活动 socket。同步 DNS 调用不能中途终止，返回后才检查期限。 |
| `bluetooth_toggle` | Linux 的状态查询、修改和确认共用 30 秒期限，单次 bluetoothctl 命令最多 8 秒；Windows helper 最多 30 秒。 | 等待命令时每次最多约 0.1 秒检查取消，取消后终止并回收直接启动的辅助进程；状态确认等待也可取消。 |
| `kill_process` | 全部匹配进程共用 10 秒期限，不会给每个进程重新分配 10 秒。 | 进程等待分成最多 0.1 秒的小段，段间检查取消。已经终止的进程不会恢复。 |

这些动作均声明 `idempotent: false`。请求取消或超时不会撤销此前已经发生的命令、
HTTP 请求、蓝牙状态变更或进程终止。强杀后未在期限内确认退出的进程会明确报错。

`http_request` 支持 GET、POST、PUT、DELETE、PATCH 和 HEAD，不跟随重定向，
3xx 响应作为失败返回。无效或超出范围的超时参数会报错。

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
Linux 注销要求 `XDG_SESSION_ID`，只终止该会话。Windows 关机权限启用失败会
直接报告原因。

`media_control` 的“播放/暂停”切换播放状态；“静音”将静音设为开启，已经静音
时保持静音。Linux 静音保留原音量。

## Windows 窗口控制

`window_control` 通过 Windows 窗口接口操作顶层窗口，支持 Windows 10 1703 及以上版本。
界面名称为“Windows 窗口控制”。

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

Wayland 热键通过 ydotool 发送 Linux keycode 的按下和释放序列，需要可工作的
ydotool 服务及权限。X11 仍优先使用 xdotool。未支持的键名会在发送前报错。
F1–F24 可用于 Linux 按键发送。

`window_pin` 需要 wmctrl 和 xprop。活动窗口来自 `_NET_ACTIVE_WINDOW`；按标题
查找只匹配标题字段。操作后读回置顶状态，最多等待 1 秒。Windows 成功结果包含
`hwnd`，Linux 成功结果包含 `window_id`，规则应引用当前平台返回的字段。

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

## 文件与链接动作

`append_text` 展开路径中的 `~`，相对路径按进程工作目录转换为绝对路径。编码
可填写 Python 支持的名称，无效名称会报错。`add_timestamp` 必须是布尔值，
字符串 `"false"` 不能用作关闭开关。

`append_text` 保留空行。启用时间戳时，只把形如 `[2026-09-06 12:30:00]` 的
完整前缀视为已有时间戳，其他以 `[` 开头的非空行仍会添加时间戳。

Linux 快捷方式将 `working_directory` 写入 `.desktop` 文件的 `Path`，将
`icon_path` 写入 `Icon`；参数按 Desktop Entry 的 Exec 规则转义。

`file_operation` 的 copy、move 必须提供非空目标路径，move 会创建缺少的父目录。
两者默认拒绝覆盖已有目标，覆盖需显式设置 `overwrite=true`；该开关必须是字面量。
tar 解包先检查完整成员列表，仅接受普通文件和目录，拒绝链接、设备、FIFO 和
其他特殊成员。非法归档不写出成员文件，但可能保留已经创建的空目标目录。
磁盘读写失败不提供事务回滚。

`open_url` 只接受 HTTP(S)，先验证整个 URL 列表，再打开浏览器。无协议的域名
或主机端口地址补 `https://`，`mailto:` 等自定义协议会被拒绝。`launch_program`
只在 Windows 返回“不是有效的可执行程序”错误 193 时尝试文件关联启动，并传递原有参数和
工作目录；路径不存在、权限不足等错误直接报告失败。

## 状态触发器

`network_status` 检查指定 TCP 端口的可达性，不表示所有互联网服务可用。
`host` 默认 `8.8.8.8`，`port` 默认 `53`，`timeout` 默认 2 秒，可设为
0.1–10 秒。企业网络可以设置能够代表所需服务的探测端点。

`folder_monitor` 默认只监听 created，按文件大小和纳秒修改时间比较完整快照；
实际时间戳精度由文件系统决定。目录缺失、不可读或成员 stat 失败会使整次采样
失败，保留上次完整快照。恢复读取后再比较变化，不把读取错误当成所有文件被删除。

Wi-Fi 的 disconnected 包括从指定网络直接切换到其他网络。电源状态查询失败
或返回未知哨兵时不会触发状态变化；resume 仅接受 Windows 配置。低电量在 20%
及以下触发后，需要接通电源或电量超过 25% 才恢复下一次触发。

Windows 默认音频设备使用 CoreAudio 查询，锁屏状态通过 psutil 读取 LockApp/logonui。
锁屏需连续两次采样确认变化，采样间隔为五秒。

剪贴板事件的 `text` 是完整内容，空闲事件的 `idle_seconds` 是本次测得的秒数。
窗口标题事件保留配置原文，匹配忽略大小写；Linux 查询需要 X11，查询失败不会
被当作窗口关闭。

`system_resource` 的磁盘监控可填写 `disk_path`，留空时 Windows 使用系统盘
根目录，Linux 使用 `/`。采样失败保留上次触发状态，恢复采样不会重复上报同一状态。

`cron_schedule` 的 daily/weekly 和 `time_schedule` 不补发启动前错过的当天
计划；已运行的触发器跨过当天计划分钟时会触发。`system_startup.started_at`
是触发器启动时间，重新启用或重启后重新计时；延迟使用单调时钟。

USB 触发器在 Windows 接受 E、E:、E:\ 和 E:/，在 Linux 接受区分大小写的
绝对挂载路径；ANY 匹配全部挂载点。

热键配置和录制至少包含一个修饰键；录制忽略 CapsLock、NumLock、ScrollLock，
Escape 取消。Linux 热键录制需要 X11。

轮询持续发生同类型、同消息错误时，最多每 60 秒记录一次堆栈；成功采样后恢复
错误记录计时。音频、锁屏、Wi-Fi 等查询失败会报告错误，不再静默保持现状。

轮询触发器在部分初始化失败后仍执行清理；状态变化在事件成功提交后才记录为
已触发。各配置仍单独采样，没有跨规则共享目录扫描或进程扫描。
