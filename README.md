# NotmyFault

一个仍在抢救中的草台班本地桌面自动化工具。

NotmyFault 让你用“条件 → 检查 → 动作”的方式告诉电脑以后该怎么做。例如：

```text
每天 20:00
  并且已接通电源
    → 将显示器亮度设置为 70%
    → 显示一条通知
```

当前版本 `alpha-0.14.0pre1`。不稳定，适合愿意测试、提交日志和接受配置变化的 Alpha 用户。

## 平台支持

| 平台 | 状态 | 说明 |
| --- | --- | --- |
| Windows | 主要开发平台 | 引擎、Dashboard 和大多数内置插件可用 |
| Linux | 实验性（糟糕）支持 | 桌面环境差异较大；Wayland 下全局热键和窗口标题检测不可用 |
| macOS | 暂不，也可能是永不支持 | 插件协议预留了平台字段，尚未适配 |

## 已经能做什么

内置插件现有 19 个触发器、29 个动作。

触发器按用途：

- 时间：定时、计划任务、开机启动、手动触发。
- 输入与状态：全局热键、空闲检测、锁屏/解锁、系统资源占用。
- 电源与设备：电源状态、电量阈值、蓝牙设备、音频设备、USB 设备。
- 网络：网络连通状态、WiFi 网络变化。
- 软件状态：进程启动/退出、窗口标题变化。
- 内容变化：剪贴板变化、文件夹变化。

动作按用途：

- 音量、显示器亮度、电源计划、关机。
- 启动或结束程序、打开 URL、创建快捷方式。
- 文件操作，以及在操作前等待文件停止写入。
- 剪贴板读取/清空/写入、文本输入、发送按键、追加日志。
- 截图、桌面通知、文本转语音。
- HTTP 请求、PowerShell 脚本。
- 锁屏、蓝牙开关、壁纸、窗口置顶。
- UIA 系列：控件操作、聚焦窗口、宏录制与回放、读取文本、等待元素。

组合方式：

- 单个触发条件用 `event`，复杂条件用可嵌套的 AND / OR 条件树。
- 多个动作按顺序执行，后面的动作可以引用上一步的结果。
- 触发器和动作作为插件独立加载，加载前检查权限、签名和平台兼容性。
- 引擎与 Dashboard 分离：关闭管理窗口不会停止后台引擎。
- 执行日志、动作成功/失败和触发器崩溃信息都记录在日志里。

完整插件清单见 Dashboard 的插件页；源码在 `notmyfault/triggers/` 和 `notmyfault/actions/`。

部分功能依赖操作系统或硬件：Windows 全局热键、窗口标题检测、外接显示器亮度控制。

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
```

```powershell
.\.venv\Scripts\python dashboard.pyw
```

Dashboard 打开后可以启动、暂停和重启后台引擎。
第一次启动时如果缺少签名或 build.json，引擎会自动完成一次
permissive 开发构建；需要其他安全模式时先手动执行“构建与签名”里的命令。

只想运行后台引擎：

```powershell
.\.venv\Scripts\python NOTMYFAULT.pyw
```

### Linux

Linux 支持仍处于**极其糟糕的**实验阶段。以 Ubuntu 系桌面为例：

```bash
git clone https://github.com/cuteaplane/notmyfault.git
cd notmyfault

python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-dev.txt

cd dashboard
npm install
npm run build
cd ..
```

```bash
./.venv/bin/python dashboard.pyw
```

根据桌面环境，剪贴板、截图、空闲检测和亮度功能还可能需要：

```bash
sudo apt install wl-clipboard gnome-screenshot brightnessctl xprintidle
```

Wayland 默认禁止普通应用监听全局按键或枚举其他应用的窗口标题。NotmyFault
会跳过明确不兼容的平台插件，但不能绕过桌面系统自身的安全限制。

## 构建与签名

NotmyFault 验证插件签名和核心文件完整性。构建入口是 `build.py`：

| 命令 | 作用 |
| --- | --- |
| `python build.py build` | strict 构建：生成密码加密的私钥，签名全部插件和 build.json。不带子命令时默认执行它 |
| `python build.py build --security-mode=permissive` | 本地开发构建：使用不加密私钥，全程免密码 |
| `python build.py verify` | 校验所有插件签名，有缺失或无效时以非零退出码结束 |
| `python build.py init-keys` | 生成 Ed25519 密钥对；`--encrypt` 加密私钥，`--force` 覆盖已有密钥 |
| `python build.py version` | 查看密钥状态、公钥指纹和插件签名数量 |

- 私钥放在 `.private/`，不检入 git；内置公钥写入 `notmyfault/security/signing_keys.py`。
- 修改内置插件或核心源码后需要重新 build，否则旧签名失效。
- 安全模式有 strict / normal / permissive 三档。引擎按环境变量
  `NOTMYFAULT_MODE`、签名 build.json、默认 strict 的顺序决定当前模式。
- normal 和 permissive 都使用不加密私钥，适合本地开发和测试。

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

一般不需要手写 JSON，Dashboard 负责编辑和校验规则。
条件树节点和数据引用格式见 [docs/rule-schema-v2.md](docs/rule-schema-v2.md)。

## 数据位置

| 平台 | 目录 |
| --- | --- |
| Windows | `%APPDATA%\NotmyFault\` |
| Linux | `$XDG_CONFIG_HOME/notmyfault/`，未设置时为 `~/.config/notmyfault/` |

该目录下：

- `config.json`：引擎配置。
- `rules.json`：规则。
- `logs/`：执行日志。
- `plugins/`：用户安装的插件。
- `.api_token`：Dashboard 与本地 API 之间共享的认证令牌。

本地 API 监听 `127.0.0.1:19198`，只接受本机请求和上述令牌认证。
项目不提供“随便打开浏览器或直接 curl 就能管理”的模式。
Dashboard 是 pywebview 桌面客户端，不是远程管理后台。

## 开发与验证

后端测试：

```bash
python -m pytest -q
```

测试目录为 `notmyfault/tests/` 和 `tests/`（pytest.ini 的 testpaths）。

Dashboard 构建与挂载测试：

```bash
cd dashboard
npm test
```

等于 `vite build && node tests/mount.mjs && node tests/macro-page.mjs`。

验证插件签名：

```bash
python build.py verify
```

`notmyfault/simulator/` 提供模拟环境，可以在不接触真实系统的情况下跑规则；
对应测试见 `notmyfault/tests/test_simulator.py`。

插件格式、条件树、动作上下文和目录说明见 [开发文档](docs/DEVELOPMENT.md)；
Windows 原生调用边界见 [docs/native-safety.md](docs/native-safety.md)。

## 已知限制

- 这是 Alpha；界面、插件描述协议和配置格式仍可能调整。
- Linux 尚未覆盖足够多的桌面环境和发行版。
- 部分插件名称和参数仍然偏开发者视角，用户友好度还在改进。
- 显示器亮度、蓝牙、睡眠等系统功能会受到驱动、权限和硬件能力限制。
- 目前没有稳定版安装包；从源码运行仍需要 Python 和 Node.js。

遇到问题时，请附上操作系统、复现步骤以及 `logs/` 里最新的日志文件。

## License

[GNU General Public License v3.0](LICENSE)
