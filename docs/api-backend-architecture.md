# API 后端结构

`NOTMYFAULT.pyw` 创建桌面后台使用的路径、存储、运行控制和 API。代码依赖
按下面的方向调用：

```text
NOTMYFAULT.pyw
  -> host/api_server.py
  -> host/api/application.py
  -> host/api/routes_*.py
  -> host/api/services/*.py
  -> core、security、platform 和外部服务接口
```

路由读取 HTTP 请求并转换响应。业务对象不导入 FastAPI 或 Starlette。
`notmyfault/host/api_server.py` 不定义端点，只创建应用并运行 Uvicorn。
`ApiServer` 公开 `app`、`publish_event()`、`serve()`、`stop()`。

`notmyfault/host/api/` 按接口用途注册六组路由：

- `routes_engine.py`：引擎、运行记录、日志、桌面控件和 SSE
- `routes_rules.py`：规则读取、检查、确认、保存和手工运行
- `routes_ai.py`：AI 设置、密钥和规则草稿
- `routes_plugins.py`：插件目录、预览、安装、更新和卸载
- `routes_interactions.py`：扩展命令、页面和会话
- `routes_settings.py`：配置签名状态和管理员设置

每组路由只接收自己使用的对象。当前对象分工如下：

- `EngineService` 汇总状态、诊断、运行记录和日志，处理启停及会话清理
- `RuleService` 检查、确认和保存规则
- `RuleRunService` 校验测试数据并发起手工运行
- `AIDraftingService` 管理 AI 设置、同步草稿、流式草稿和取消
- `PluginCatalogService` 扫描插件并生成 Dashboard 使用的清单和 schema
- `PluginInstallationService` 处理预览、风险检查、安装、更新和卸载
- `PluginInteractionService` 管理扩展命令、页面和会话归属
- `SettingsService` 修改管理员设置并处理配置重新签名
- `EventBroker` 记录引擎事件并投递给 SSE 订阅者

`ApplicationPaths` 从平台配置目录计算 `config.json`、`rules.json`、
`.config_secret`、`.api_token`、`.ai_api_key`、`run-events.jsonl`、日志和用户
插件目录。插件私钥仍在项目根目录的 `.private/signing_private_key.pem`。
这些位置没有迁移。

`SignedConfigStore` 负责读取、验签、备份、迁移和写回配置与规则。
`NOTMYFAULT.pyw` 只创建一个实例。`EngineRunner` 把它交给 `create_engine()`；
`AutomationEngine` 和 `RulesHotReloader` 保留这个实例；API 服务也使用它。
配置和规则文件仍使用 `_signature` 字段和 `.config_secret` 中的
HMAC-SHA256 密钥。

桌面后台启动顺序是：

1. `ApplicationPaths.default()` 取得现有配置目录和安装目录。
2. `SignedConfigStore` 绑定这些路径。
3. `EngineRunner` 取得存储和 `create_engine()`。
4. token、AI 密钥、运行记录、事件、插件文件系统和外部注册表实现被创建。
5. `create_api_server()` 组装六组路由。
6. 引擎启动后通过 `publish_event()` 把状态和运行事件交给 `EventBroker`。
7. Uvicorn 使用已经独占的监听 socket 提供 HTTP 服务。

`ApiTokenStore` 创建和修复 token 文件。除 `OPTIONS` 外，所有 `/api/` 请求都要带
`Authorization: Bearer <token>`。令牌缺失或不匹配时返回 403，并尝试修复 token 文件。

Dashboard 的普通 JSON 请求通过 `window.pywebview.api.request_api()` 发送。文件上传、
下载和 SSE 直接访问 HTTP，在请求前通过 `window.pywebview.api.get_api_token()` 读取
令牌。直接请求收到 403 时会重新读取令牌并重试一次。

`EventBroker` 接收引擎线程发布的事件，写入运行记录，再投递给每个 SSE
订阅。每个订阅保存自己的事件循环和队列。

新增接口时先选现有路由组。请求字段、状态码和响应转换写在路由。规则、
插件、AI 或会话处理写在对应业务对象。需要操作系统、网络、密钥或文件替换
时，在 `ports.py` 或业务对象构造参数中声明所需方法，由启动代码传入实现。
不要在路由里打开 `config.json` 或 `rules.json`，也不要从路由导入具体引擎类。

API 测试创建临时 `ApplicationPaths`、`SignedConfigStore` 和固定 token。
`notmyfault/tests/api_support.py` 提供 `FakeRunner`、`FakeKeyStore`、
`FakeDesktopElements` 和 `make_api_env()`。文件替换失败使用
`PluginFileSystem` 的故障实现；过期测试给 `PendingPreviewStore` 传入时钟。
测试不修改 `api_server` 或 `config` 的模块属性。
