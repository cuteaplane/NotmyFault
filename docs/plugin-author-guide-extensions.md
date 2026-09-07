# 插件作者指南：多文件、二进制与签名

## 插件结构

插件目录需要清单和 Python 入口。清单使用 `action.json` 或 `trigger.json`；入口使用
`action.py`、`trigger.py`，也可以由 `entrypoints` 指向其他 `.py` 文件。目录里还可以放：

- 其他 `.py` 模块：入口文件可以直接 `import helper`，也可以使用包内相对导入。
  模块在各自插件的模块名称下加载，同名兄弟模块互不混用；插件目录不会加入全局
  `sys.path`，卸载时清理该插件创建的模块。
- 预编译二进制与资源：放在目录内任意位置（惯例 `bin/<平台>/`），
  由插件自己的 py 代码调用，引擎不提供二进制直执行入口。

```
my_plugin/
├── action.json        # 元数据
├── action.py          # 入口
├── helper.py          # 兄弟模块，入口可直接 import
├── bin/win64/tool.exe # 自带二进制
├── signature.sig      # 作者签名
└── public_key.pem     # 作者公钥
```

## 定位自带资源

不要自拼 `__file__`，用引擎提供的辅助：

```python
from notmyfault.security.plugin_resources import plugin_resource

tool = plugin_resource("my_plugin", "bin", "win64", "tool.exe")
```

返回插件目录内的绝对路径；路径越出插件目录会抛 ValueError。

## 提权

插件需要以管理员权限执行系统命令时，使用统一通道，并在元数据 `permissions`
里声明 `"admin"`，同时在 `security.admin_executables` 列出允许的系统命令文件名：

```python
from notmyfault.security.sudo import run_as_admin

result = run_as_admin(["netsh", "interface", "set", "interface", iface, "admin=enabled"])
```

```json
{
  "permissions": ["admin"],
  "security": {
    "admin_executables": ["netsh"]
  }
}
```

Windows 每次管理员执行请求都需要通知确认和 UAC，Linux 使用 pkexec。
可执行文件名不能包含路径分隔符，引擎只从受信任的系统目录（Windows System32、
POSIX `/usr/bin` 等）解析命令。插件自带的二进制文件目前无法通过提权通道执行。

## 作者签名

`self_sign_plugin()` 会用作者的 Ed25519 私钥生成 `signature.sig`，并把配套公钥写入
`public_key.pem`：

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from notmyfault.security.signing import self_sign_plugin

key = Ed25519PrivateKey.generate()
self_sign_plugin("my_plugin", key)   # 生成 signature.sig 与 public_key.pem
```

- 签名覆盖插件目录中的常规文件，包括二进制和资源；签名、公钥及公钥副签本身不在
  覆盖范围内。改动被覆盖的文件后必须重新签名。
- `public_key.pem` 必须与已签名的插件目录一起安装。严格模式下，普通作者签名还需要
  当前安装者的本地插件密钥为该公钥生成 `public_key.sig` 副签；安装流程会完成这一步。
- 验签结果分为 `official`、`author`、`official-legacy` 和 `none`。严格模式不加载
  `none`。普通作者签名还要有当前安装使用的本地密钥副签。声明 `admin` 权限的插件
  在严格模式下只接受 `official` 或 `official-legacy` 签名，作者签名不能加载。
- 私钥自己保管，不要放进插件目录或归档。

## 打包

```powershell
python nmf.py plugin pack <插件目录路径>
```

`nmf.py plugin pack` 会先做 schema、平台、能力、权限、入口和静态风险检查，再生成
`.nmfp`。归档包含插件源码、资源和二进制，并保留 `signature.sig`、`public_key.pem`
和 `public_key.sig`，只排除 `__pycache__` 缓存目录。

## 可选：安装期编译钩子

需要现场编译产物时，在元数据里声明 `build`。例如 C 扩展：

```json
{
  "build": {
    "command": ["gcc main.c -o bin/tool.exe"],
    "outputs": ["bin/tool.exe"]
  }
}
```

- 安装时在插件目录内逐条执行 `command`，120 秒超时；任一命令失败或
  `outputs` 缺失即安装失败。
- `outputs` 必须是插件目录内的相对路径。
- 安装构建流程保留归档里的签名材料，作者签名必须与构建后的文件内容相符。
  安装端在替换旧版本前按加载器的签名规则校验暂存目录；严格模式要求有效签名，
  作者自签的非管理员插件还需本地密钥副签。
- 钩子无依赖解析、无工具链检测，作者需保证目标机器能跑通命令；
  内置插件不允许携带 `build`。

## 声明可传递的数据

路径、网址、整数和日期时间使用对应 `value_type`，数组声明元素类型。
具有特定业务结构的共享值在 `contributes.data_types` 中声明 `binding: shared`
和 `schema`，消费者引用完整包名、类型 ID 与版本。插件不必提供专用编辑器。
私有编辑数据继续使用 `plugin_data` 和 `binding: private`。升级类型版本不隐式
迁移旧值；旧入口和规则可继续读取。示例与协议见 [data-types.md](data-types.md)。
