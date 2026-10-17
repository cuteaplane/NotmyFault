# 插件索引 v1

NotmyFault 当前只读取静态 JSON 索引，不提供上传、评分、搜索和发布账户。索引可以放在
GitHub 仓库中，插件包可以放在 GitHub Releases 或其他 HTTPS 公网地址。

## 索引格式

```json
{
  "schema_version": 1,
  "plugins": [
    {
      "package_name": "com.example.clipboard_tools",
      "name": "Clipboard Tools",
      "version": "1.0.0",
      "download": "https://github.com/example/nmf-plugins/releases/download/v1.0.0/clipboard-tools.nmfp",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "homepage": "https://github.com/example/nmf-plugins/tree/main/clipboard-tools",
      "supported_platforms": ["windows", "linux"]
    }
  ]
}
```

`schema_version` 当前只能是 `1`。每个插件条目必须包含示例中的七个字段，同一
`package_name` 和 `version` 不能重复。`supported_platforms` 可使用 `windows`、`linux`
和 `macos`。

`download` 必须指向 `.nmfp` 文件。发布者应对最终下载文件计算 SHA-256；如果重新打包，必须
同步更新索引摘要。

## 网络限制

索引、插件包和主页地址只接受 HTTPS 公网地址。后台拒绝 URL 凭据、非 443 端口、本机地址、
内网地址和指向这些地址的重定向。索引最大 1 MiB，插件包最大 64 MiB。

静态索引只负责发现和分发，不代表 NotmyFault 审核或信任其中的插件。SHA-256 只能确认下载内容
与索引一致，不能替代插件签名、权限检查和源码风险审阅。
