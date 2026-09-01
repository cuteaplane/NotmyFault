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
风险项会显示，但不会单独使检查失败；schema、平台、必需能力、权限或入口文件不通过时
退出码为 1。

`plugin test` 用当前 Python 运行插件目录里的 pytest，300 秒没有结束时返回 2。

`plugin pack` 先执行同一套检查，通过后生成 `.nmfp`。归档不会包含 `signature.sig`；
不要把该命令当作保留作者签名的发布流程。

`plugin create` 不覆盖已有目录。action 和 trigger 模板都带最小清单、Python
入口、`test_plugin.py` 和 `.github/workflows/test.yml`。工作流在 Windows 与
Ubuntu 上运行 pytest。
