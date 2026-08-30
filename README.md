# NotmyFault

闲着没事儿写的自动化工具......

NotmyFault 顾名思义是一个自动化（废话），由各种条件出发，以各种方式执行。

## 亮点

自动化软件，能干的事还是有些少的，并且很零碎......为了解决这个问题，NotmyFault最大的亮点就是可扩展性！
只要会Python，你随时可以编写一个属于自己的插件，让NotmyFault帮你办成任何事！
通过插件，NotmyFault可以拓展出强大的能力，接入任何服务，构建一个更通用的自动化。

## 平台支持

| 平台 | 状态 | 说明 |
| --- | --- | --- |
| Windows | 主要开发平台 | 引擎、Dashboard 和大多数内置插件可用 |
| Linux | 实验性（糟糕）支持 | 桌面环境差异较大；Wayland 下全局热键和窗口标题检测不可用 |
| macOS | 暂不，也可能是永不支持 | 插件协议预留了平台字段，尚未适配 |

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+
- Windows 10/11，或较新的 Linux 桌面环境（实验性）

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

Dashboard用于操作引擎，用于日常管理和插件的安装。
第一次启动时如果缺少签名或 build.json，引擎会自动通过严格方式构建，或许等我再过上十年做个安装脚本出来（啥）

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
会跳过明确不兼容的平台插件，但仍然会受到环境的限制。

## 构建

构建入口是 `build.py`，负责管理安全模式。

| 命令 | 作用 |
| --- | --- |
| `python build.py build` | strict 构建：生成密码加密的私钥，签名全部插件和 build.json。不带子命令时默认执行它 |
| `python build.py build --security-mode=permissive` | 本地开发构建：使用不加密私钥，全程免密码 |
| `python build.py verify` | 校验所有插件签名，有缺失或无效时以非零退出码结束 |
| `python build.py init-keys` | 生成 Ed25519 密钥对；`--encrypt` 加密私钥，`--force` 覆盖已有密钥 |
| `python build.py version` | 查看密钥状态、公钥指纹和插件签名数量 |

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

Dashboard 负责编辑和校验规则。
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
Dashboard 是 pywebview 桌面客户端，只能在本机使用。

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

- 这是 Alpha；我可能哪天不高兴就把配置格式改了
- Linux 基本上就是个可用，当然可以自己写插件。
- 部分插件名称和参数仍然偏开发，比如你看到的这一堆文档。
- 显示器亮度、蓝牙、睡眠等系统功能会受到驱动、权限和硬件能力限制，当然你真的可以自己写插件来绕过。
- 目前没有稳定版安装包；从源码运行仍需要 Python 和 Node.js，之后会写的会写的

遇到问题时，请附上操作系统、复现步骤以及 `logs/` 里最新的日志文件？真的会有人来提Issue吗...

## License

[GNU General Public License v3.0](LICENSE)
