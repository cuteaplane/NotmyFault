# Windows 安装、升级与卸载

升级使用原安装目录内的签名密钥完成新版构建。升级失败或取消时，恢复旧安装。
新版程序与保留的用户文件重名时，升级停止并提示该文件的位置。

卸载移除程序文件、Python 运行环境、启动入口，以及
`app/.private/signing_private_key.pem` 和 `app/.private/signing_public.pem`。
`app/.private` 中的其他文件不会随这两把密钥一起删除。

用户配置目录中的规则、配置和配置验签密钥保持原位。安装目录中的用户插件和
其他用户文件移到同级的 `原目录名-保留文件-随机编号` 目录，完成页面显示实际位置。
卸载完成后可以再次安装到原路径。

## 卸载未完成时

卸载先检查文件路径并保存清理清单，再移动安装目录并删除程序文件。发生错误后，
本次卸载停止；重试时继续清理剩余文件，不恢复已删除的程序。

文件占用或权限问题解决后，可以在卸载窗口点击“重试”。窗口已经关闭时，使用
新版安装包的 `--resume-uninstall` 参数继续，参数值为失败窗口显示的实际目录。

```powershell
& $installer --resume-uninstall $directory
```

`$installer` 是安装包的完整路径，`$directory` 是未完成卸载的目录。
清理清单 `.notmyfault-uninstall` 保留到程序文件、注册信息和安装标记均已处理完毕。
即使安装标记已经删除，也可以凭清理清单继续卸载。不要手工删除该清单。
原路径的卸载清理未完成时，安装器会提示先完成清理。

## 开发检查

`installer/windows/tests/run_smoke.ps1` 使用独立测试目录和重定向的测试注册表，
检查安装、升级恢复、卸载、用户文件保留和重新安装。
传入 `-MaintenanceOnly` 会检查窗口样式初始化、快捷方式归属、首次启动参数、
卸载器临时复制交接，以及升级和卸载恢复；无需释放完整离线安装内容。
维护检查仍需要创建隔离的 HKCU 测试项。

```powershell
& installer/windows/tests/run_smoke.ps1 -Installer $installer -WorkDirectory $testDirectory -MaintenanceOnly
```

`$testDirectory` 必须是新的空目录，开发时放在 `agent_files/temp/` 下。
