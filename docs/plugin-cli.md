# 插件开发命令

仓库根目录的 `nmf.py` 提供同一个插件开发入口：

```bash
python nmf.py plugin check path/to/plugin
python nmf.py plugin test path/to/plugin -q
python nmf.py plugin pack path/to/plugin --output-dir dist
python nmf.py plugin create action my_action --output-dir plugins
python nmf.py plugin create trigger my_trigger --output-dir plugins
```

`plugin check` 检查清单 schema、当前平台、系统能力、权限、AST 风险、借壳提权、
签名、入口文件和贡献项。`--json` 输出可直接交给其他工具。
输出分开列出开发检查和当前安全模式的加载检查。开发检查包括 schema、平台、必需能力、
入口、未声明的能力、借用其他插件权限以及禁止的静态能力；失败时退出码为 1。
未知权限名列入 `permissions.errors`，该列表不单独改变退出码。签名缺失不会单独使开发检查失败，
但 strict 模式的加载检查会拒绝签名无效或缺少本地副签的插件。

JSON 中 `ok` 对应 `checks.development.allowed`；`checks.load_policy` 给出当前模式、
是否满足加载策略、错误和警告。`checks.signature` 分别给出签名来源 `source` 与格式
`format`。安装预览和插件列表也提供同名检查结果。加载器仍会在实际导入前复查文件，
加载检查结果不代表已经执行过插件。

`plugin test` 用当前 Python 运行插件目录里的 pytest，300 秒没有结束时返回 2。

`plugin pack` 先执行开发检查，通过后生成 `.nmfp`。归档保留 `signature.sig`、
`public_key.pem` 和 `public_key.sig`，排除 `__pycache__` 缓存目录。

`plugin create` 不覆盖已有目录。action 和 trigger 模板都带最小清单、Python
入口、`test_plugin.py` 和 `.github/workflows/test.yml`。工作流在 Windows 与
Ubuntu 上运行 pytest。
