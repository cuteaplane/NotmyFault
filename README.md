# NotmyFault

一个仍在抢救中草台班本地桌面自动化工具。

NotmyFault 让你用“条件 → 检查 → 动作”的方式告诉电脑以后该怎么做。例如：

```text
每天 20:00
  并且已接通电源
    → 将显示器亮度设置为 70%
    → 显示一条通知
```

不稳定，目前最适合愿意测试、提交日志和接受配置变化的 Alpha 用户。

## 当前状态

当前版本：`alpha-0.11.0`

| 平台 | 状态 | 说明 |
| --- | --- | --- |
| Windows | 主要开发平台 | 核心引擎、Dashboard 和大多数内置插件可用 |
| Linux | 实验性支持 | 不同桌面环境差异较大，Wayland 会限制全局热键和窗口检测 |
| macOS | 暂不支持 | 插件协议预留了平台字段，但尚未完成适配 |

## 已经能做什么

- 用定时、热键、进程、剪贴板、文件夹、设备、电源和网络状态触发规则。
- 使用可嵌套的 `AND` / `OR` 条件树组合多个条件。
- 按顺序执行多个动作，并在后续步骤中引用上一步的结果。
- 在动作开始前检查文件是否仍在写入、文档是否还在编辑。
- 调整音量和亮度、启动或结束程序、操作文件、截图、通知、HTTP 请求等。
- 将触发器和动作作为插件独立加载，并检查权限、签名和平台兼容性。
- 后台引擎与 Dashboard 分离；关闭管理窗口不会自动停止已经运行的引擎。
- 记录执行日志、动作成功/失败和触发器崩溃信息。

内置插件目前包括 13 个触发器和 16 个动作。部分功能依赖操作系统或硬件支持：例如 Windows 全局热键、窗口标题检测，以及外接显示器的亮度控制。

## 从源码运行

### 环境要求

- Python 3.11+
- Node.js 18+
- Windows 10/11，或较新的 Linux 桌面环境

### Windows

```powershell
git clone https://github.com/cuteaplane/notmyfault.git
cd notmyfault

python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt

cd dashboard
npm install
npm run build
cd ..

.\.venv\Scripts\python build.py --security-mode=permissive
.\.venv\Scripts\python dashboard.pyw
```

Dashboard 打开后可以启动、暂停和重启后台引擎。只想运行后台服务时：

```powershell
.\.venv\Scripts\python NOTMYFAULT.pyw
```

### Linux

Linux 支持仍处于实验阶段。以 Ubuntu 系桌面为例：

```bash
git clone https://github.com/cuteaplane/notmyfault.git
cd notmyfault

python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-dev.txt

cd dashboard
npm install
npm run build
cd ..

./.venv/bin/python build.py --security-mode=permissive
./.venv/bin/python dashboard.pyw
```

根据桌面环境，剪贴板、截图、空闲检测和亮度功能还可能需要：

```bash
sudo apt install wl-clipboard gnome-screenshot brightnessctl xprintidle
```

Wayland 默认禁止普通应用监听全局按键或枚举其他应用的窗口标题。NotmyFault 会跳过明确不兼容的平台插件，但不能绕过桌面系统自身的安全限制。

## 为什么首次运行需要 `build.py`

NotmyFault 会验证插件签名和核心文件完整性。直接从源码运行前，需要生成本地构建信息并为当前源码签名：

```bash
python build.py --security-mode=permissive
```

修改内置插件或核心源码后需要重新执行该命令，否则旧签名会失效。

`permissive` 适合本地开发和 Alpha 测试。默认的 `strict` 模式会要求加密签名密钥，面向正式构建：

```bash
python build.py
```

## 规则模型

一条规则由三部分组成：

```text
触发条件 → 可选的执行前检查 → 动作流水线
```

单一触发条件使用 `event`；复杂条件使用可嵌套的 `condition`：

```json
{
  "name": "晚间亮度",
  "condition": {
    "op": "all",
    "children": [
      {
        "type": "time_schedule",
        "params": {"time": "20:00"}
      },
      {
        "type": "power_state",
        "params": {"state": "ac"}
      }
    ]
  },
  "actions": [
    {
      "type": "display_control",
      "params": {
        "action": "set_brightness",
        "brightness": 70
      }
    },
    {
      "type": "notify",
      "params": {
        "title": "NotmyFault",
        "message": "晚间亮度已调整"
      }
    }
  ]
}
```

一般不需要手写 JSON，Dashboard 会负责编辑和校验规则。

## 数据位置

| 平台 | 配置与日志目录 |
| --- | --- |
| Windows | `%APPDATA%\NotmyFault\` |
| Linux | `$XDG_CONFIG_HOME/notmyfault/`，未设置时为 `~/.config/notmyfault/` |

本地 API 监听 `127.0.0.1:19198`，但它使用 Dashboard 管理的本机认证令牌。项目目前不提供“随便打开浏览器或直接 curl 就能管理”的模式。

## 开发与验证

后端测试：

```bash
python -m pytest notmyfault/tests -q
```

Dashboard 构建与挂载测试：

```bash
cd dashboard
npm test
```

验证插件签名：

```bash
python build.py verify
```

插件格式、条件树、动作上下文和目录说明见 [开发文档](docs/DEVELOPMENT.md)。

## 已知限制

- 这是 Alpha；界面、插件描述协议和配置格式仍可能调整。
- Linux 尚未覆盖足够多的桌面环境和发行版。
- 部分插件名称和参数仍然偏开发者视角，易用性正在收敛。
- 显示器亮度、蓝牙、睡眠等系统功能会受到驱动、权限和硬件能力限制。
- Dashboard 是桌面客户端，不是远程管理后台。
- 目前没有稳定版安装包承诺；从源码运行仍需要 Python 和 Node.js。

遇到问题时，请附上操作系统、复现步骤以及最新的日志文件

## License

[GNU General Public License v3.0](LICENSE)
