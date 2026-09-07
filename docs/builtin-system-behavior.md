# 内置系统动作与状态触发器

这些行为对应 `notmyfault/actions/`、`notmyfault/triggers/` 和
`notmyfault/platform/` 的当前实现。

## 执行期限与取消

`run_powershell`、`http_request`、`bluetooth_toggle`、`kill_process` 声明
`execution_api: context-v1` 和 `cancellation_api: runtime-v1`，可以接收规则的
协作取消信号。规则超时仍需等待插件退出正在执行的系统调用。

| 动作 | 执行限制 | 取消行为 |
| --- | --- | --- |
| `run_powershell` | 命令执行最多 60 秒；stdout、stderr 各保留最多 100 KiB，超出部分丢弃并标注截断。 | 检查间隔最多约 0.1 秒，超时或取消时终止直接启动的 PowerShell 进程，再关闭输出读取。不会自动终止所有后代进程。 |
| `http_request` | `timeout_seconds` 默认 30 秒，范围 1–300 秒。候选 IP 连接、请求和正文读取共用期限；正文最多 1 MiB。 | 取消或期限到达时中断活动 socket。同步 DNS 调用不能中途终止，返回后才检查期限。 |
| `bluetooth_toggle` | Linux 的状态查询、修改和确认共用 30 秒期限，单次 bluetoothctl 命令最多 8 秒；Windows helper 最多 30 秒。 | 命令之间和确认等待期间检查取消；已启动的命令返回后才能响应取消。 |
| `kill_process` | 全部匹配进程共用 10 秒期限，不会给每个进程重新分配 10 秒。 | 进程等待分成最多 0.1 秒的小段，段间检查取消。已经终止的进程不会恢复。 |

这些动作均声明 `idempotent: false`。请求取消或超时不会撤销此前已经发生的命令、
HTTP 请求、蓝牙状态变更或进程终止。

## Windows 系统资源

`screenshot` 的全屏模式覆盖整个虚拟桌面，包括主屏左侧或上方显示器的负坐标；
活动窗口模式使用所选窗口的矩形。

剪贴板动作从打开到关闭剪贴板全程持有原生调用锁。显示器电源操作使用异步窗口
广播，发送成功表示广播已提交，不表示每个显示器都已经完成切换。

`shutdown_system.force` 用于 Windows 关机、重启或注销时强制关闭应用。
睡眠和休眠不使用该选项：Windows 的 `SetSuspendState` 参数 bForce
[没有实际作用](https://learn.microsoft.com/en-us/windows/win32/api/powrprof/nf-powrprof-setsuspendstate)。
Linux 注销要求 `XDG_SESSION_ID`，只终止该会话。

## Linux 桌面后端

Wayland 热键通过 ydotool 发送 Linux keycode 的按下和释放序列，需要可工作的
ydotool 服务及权限。X11 仍优先使用 xdotool。未支持的键名会在发送前报错。

`window_pin` 需要 wmctrl 和 xprop。活动窗口来自 `_NET_ACTIVE_WINDOW`；按标题
查找只匹配标题字段。操作后读回置顶状态，最多等待 1 秒。Windows 成功结果包含
`hwnd`，Linux 成功结果包含 `window_id`，规则应引用当前平台返回的字段。

Wayland Portal 截图在请求前监听响应，等待最多 120 秒。未完成的请求在超时、
取消或错误时尝试关闭；用户拒绝授权会报告失败。能力报告中的 `degraded: true`
表示本地依赖存在，Portal 服务或截图授权仍需执行时检查。Windows TTS 也使用
该标记说明 SAPI 服务尚未在能力探测阶段验证。

## 文件与链接动作

`append_text` 保留空行。启用时间戳时，只把形如 `[2026-09-06 12:30:00]` 的
完整前缀视为已有时间戳，其他以 `[` 开头的非空行仍会添加时间戳。

Linux 快捷方式将 `working_directory` 写入 `.desktop` 文件的 `Path`，将
`icon_path` 写入 `Icon`；参数按 Desktop Entry 的 Exec 规则转义。

`file_operation` 的 copy、move 必须提供非空目标路径，move 会创建缺少的父目录。
tar 解包先检查完整成员列表，仅接受普通文件和目录，拒绝链接、设备、FIFO 和
其他特殊成员。非法归档不写出成员文件，但可能保留已经创建的空目标目录。
磁盘读写失败不提供事务回滚。

`open_url` 先验证整个 URL 列表，再打开浏览器。`launch_program` 只在 Windows
返回“不是有效的可执行程序”错误 193 时尝试文件关联启动，并传递原有参数和
工作目录；路径不存在、权限不足等错误直接报告失败。

## 状态触发器

`network_status` 检查指定 TCP 端口的可达性，不表示所有互联网服务可用。
`host` 默认 `8.8.8.8`，`port` 默认 `53`，`timeout` 默认 2 秒，可设为
0.1–10 秒。企业网络可以设置能够代表所需服务的探测端点。

`folder_monitor` 的目录缺失、不可读或成员 stat 失败会使整次采样失败，保留
上次完整快照。恢复读取后再比较变化，不把读取错误当成所有文件被删除。

Wi-Fi 的 disconnected 包括从指定网络直接切换到其他网络。电源状态查询失败
或返回未知哨兵时不会触发状态变化；resume 仅接受 Windows 配置。

轮询触发器在部分初始化失败后仍执行清理；状态变化在事件成功提交后才记录为
已触发。各配置仍单独采样，没有跨规则共享目录扫描或进程扫描。
