# 插件扩展 API

插件可以在 `action.json` 或 `trigger.json` 的 `contributes` 中声明命令、参数编辑器、页面和自有数据类型。Dashboard 负责显示入口、承载页面和保存值。录制热键、解析路径等具体操作留在插件命令中。

参数编辑器有两种值：

- 普通值使用 `value_type`。Dashboard 保留原参数控件，并在旁边显示插件按钮。
- 自有数据使用 `data_type`。Dashboard 只显示插件编辑入口，不读取 `data` 中的业务字段。

## 清单

`notmyfault/triggers/hotkey/trigger.json` 是普通值编辑器的实例。

```json
{
  "contributes": {
    "commands": [
      {
        "id": "capture_hotkey",
        "title": "录制热键",
        "handler": "extension.py:capture_hotkey"
      }
    ],
    "parameter_editors": [
      {
        "id": "hotkey_recorder",
        "parameter": "hotkey",
        "value_type": "string",
        "command": "capture_hotkey",
        "ui": {
          "control": "button",
          "label": "录制",
          "busy_label": "请按快捷键…",
          "icon": "keyboard"
        }
      }
    ]
  },
  "params": [
    {
      "name": "hotkey",
      "label": "快捷键",
      "type": "hotkey",
      "value_type": "string"
    }
  ]
}
```

`notmyfault/actions/uia_automation/action.json` 是自有数据编辑器的实例。

```json
{
  "contributes": {
    "commands": [
      {
        "id": "open_macro",
        "title": "打开操作宏编辑器",
        "handler": "component.py:open_macro"
      },
      {
        "id": "start_recording",
        "title": "开始录制操作",
        "handler": "component.py:start_recording"
      },
      {
        "id": "stop_recording",
        "title": "停止录制操作",
        "handler": "component.py:stop_recording"
      },
      {
        "id": "commit_macro",
        "title": "保存操作宏",
        "handler": "component.py:commit_macro"
      }
    ],
    "views": [
      {
        "id": "macro_workbench",
        "title": "操作宏编辑器",
        "page": "pages/macro.html",
        "commands": ["start_recording", "stop_recording", "commit_macro"],
        "window_controls": ["minimize", "restore"]
      }
    ],
    "data_types": [
      {
        "id": "mouse_macro",
        "version": 1,
        "binding": "private"
      }
    ],
    "parameter_editors": [
      {
        "id": "macro_editor",
        "parameter": "macro",
        "data_type": "mouse_macro",
        "command": "open_macro",
        "view": "macro_workbench",
        "ui": {
          "control": "button",
          "empty_label": "录制操作宏",
          "icon": "movie"
        }
      }
    ]
  },
  "params": [
    {
      "name": "macro",
      "label": "操作宏",
      "type": "plugin_data",
      "data_type": "mouse_macro",
      "value_type": "object",
      "required": true
    }
  ]
}
```

清单由 `notmyfault/security/plugin_schema.py` 检查。命令、页面、数据类型和参数之间的引用必须存在。处理函数和页面路径必须留在插件目录内。

### commands

每个命令包含 `id`、`title` 和 `handler`。`handler` 使用 `相对.py路径:函数名` 格式。处理函数接收 `context` 和 `payload` 两个参数。

命令不会因为出现在清单中就得到全局入口。`parameter_editors.command` 是参数编辑入口；`views.commands` 是页面在同一会话中可以调用的命令。

### parameter_editors

参数编辑器绑定本插件的一个参数和一个命令。当前 UI 控件只支持 `button`。一个参数不能声明多个编辑器。

普通参数必须同时在参数和编辑器中声明相同的 `value_type`。类型可使用基本类型、结构化声明和已安装的共享类型，见 [data-types.md](data-types.md)。普通参数编辑器不能声明 `data_type` 或 `accepts_legacy`。

普通参数的原有输入控件仍然可用。按钮文案来自 `ui.label`，命令执行中的文案来自 `ui.busy_label`。`ui.icon` 和 `ui.description` 分别控制图标和提示文字。

`plugin_data` 参数必须在参数和编辑器中声明相同的 `data_type`。编辑器不能再声明 `value_type`。未保存数据时的按钮文案来自 `ui.empty_label`。

`accepts_legacy: true` 允许编辑器读取升级前保存的非信封对象。重新保存后会写成新的自有数据格式。空对象和带有错误 `$type` 的对象不会通过规则校验。

### views

页面是插件目录内的 UTF-8 HTML 文件。单个文件不能超过 1 MiB。

页面运行在不带 `allow-same-origin` 的 sandbox iframe 中。Dashboard 会注入 CSP。页面不能联网、加载外部资源、打开子页面、提交表单或运行插件目录里的其他文件。

页面需要最小化或恢复 Dashboard 时，在 `window_controls` 中声明 `minimize` 或 `restore`。未声明的窗口操作不会执行。

### data_types

数据类型包含 `id`、正整数 `version` 和 `binding`，可选 `label`。`binding` 默认为
`private`；声明 `shared` 时必须提供 `schema`，值可跨插件绑定并由宿主校验。
完整身份由包名、类型 ID 和版本组成，依赖类型必须可用且允许共享。

`private` 参数不能绑定触发器输出、动作输出或测试数据。Dashboard 不显示绑定入口，后端也会拒绝手工写入的 `$ref`。

## 自有数据

`ExtensionContext.commit()` 把插件数据包装成统一信封。

```json
{
  "$type": "io.github.notmyfault.uia_automation/mouse_macro@1",
  "summary": "1 个操作宏 · 3 步",
  "data": {
    "version": 1,
    "steps": []
  }
}
```

`$type` 由插件的 `package_name`、数据类型 id 和版本组成。`summary` 是 Dashboard 可以显示的短文本。私有类型由专用编辑器处理 `data`；共享类型可以使用标准结构化输入和字段引用。

实例里的数据类型 id 仍是 `mouse_macro`，用于读取已经保存的值。它是存储标识，不是显示名称；界面统一称为“操作宏”。需要改数据类型 id 时，应先提供明确的数据迁移机制。

信封和摘要由 `notmyfault/extensions/protocol.py` 检查。信封经过通用值编码后总大小不能超过 1 MiB，摘要不能超过 160 个字符。

动作执行时仍应使用 `unpack_owned_value()` 检查归属。`notmyfault/actions/uia_automation/action.py` 演示了自有数据的读取方式；`mouse_macro` 和 `uia_selector` 的内部结构均由插件维护，宿主只校验通用信封。

## 普通值

普通参数编辑器收到的是规则中当前保存的原值。插件用 `ExtensionContext.commit_value()` 提交新值。

```python
def capture_hotkey(context, payload):
    result = _wait_for_hotkey_windows(context, 15)
    if "hotkey" in result:
        return context.commit_value(result["hotkey"])
    return context.error("没有等到按键", close=True)
```

完整实现见 `notmyfault/triggers/hotkey/extension.py`。Linux 使用同一提交接口，只替换按键读取函数。

NotmyFault 会检查当前值和提交值是否符合编辑器声明的 `value_type`。例如 `string` 编辑器返回对象时，命令调用失败，Dashboard 不会写入该值。

`commit_value(value, close=True, response=None)` 默认提交后关闭会话。`response` 会放进 HTTP 响应的 `data` 字段，不会写入规则。

## 命令处理函数

```python
def open_editor(context, payload):
    return context.open_view("editor", {"value": context.current_value})


def save(context, payload):
    data = payload if isinstance(payload, dict) else {}
    return context.commit(data, "已经保存")
```

普通参数的 `context.current_value` 是规则中保存的原值。自有数据参数会先检查信封归属，再把拆出的 `data` 交给命令。允许读取旧数据时，旧对象会按原值传入。

`context.open_view(view_id, state)` 打开清单声明的页面。`state` 通过初始化消息发给页面。

`context.result(data, close=False)` 返回普通结果。`context.error(message, close=False)` 返回插件错误。`close=True` 会在返回错误前关闭会话。

`context.commit_value(value, close=True)` 提交普通值。`context.commit(data, summary, close=True)` 生成自有数据信封并提交给 Dashboard。两个提交方法不能混用：普通值会话不能调用 `commit()`，自有数据会话不能调用 `commit_value()`。

后台线程或输入钩子需要跟着会话停下来，可以把停止函数传给 `context.register_cleanup(callback)`。页面关闭和引擎停止会直接清理；会话超时或插件更新会在下次访问该会话时清理。

同一会话的命令按顺序执行。用户关闭页面、插件提交完成或引擎停止后，会话失效。会话超过 30 分钟没有操作时，下次访问会返回“会话不存在或已过期”。插件更新后继续使用旧会话，则会提示重新打开编辑器。

## 页面消息

Dashboard 加载页面后会发送初始化消息。

```js
{
  source: 'notmyfault:extension-host',
  type: 'init',
  state: {}
}
```

页面调用命令时向 `window.parent` 发送消息。

```js
window.parent.postMessage({
  source: 'notmyfault:extension-view',
  type: 'invoke',
  request_id: 'save-1',
  command: 'save',
  payload: { text: '内容' },
}, '*')
```

Dashboard 只接受来自当前 iframe 的消息。`command` 必须出现在该页面的 `views.commands` 中。

命令完成后，Dashboard 把结果发回同一个 iframe。

```js
{
  source: 'notmyfault:extension-host',
  type: 'result',
  request_id: 'save-1',
  response: { ok: true, data: {} }
}
```

页面可以发送 `{ source: 'notmyfault:extension-view', type: 'close' }` 请求关闭。页面关掉后，NotmyFault 会调用 `context.register_cleanup()` 函数停掉输入钩子和后台线程。

清单允许后，页面可以请求 Dashboard 窗口操作。

```js
window.parent.postMessage({
  source: 'notmyfault:extension-view',
  type: 'window-control',
  action: 'minimize',
}, '*')
```

## HTTP 接口

- `GET /api/plugins/extensions` 返回四类贡献项。
- `POST /api/plugins/{plugin_id}/extensions/commands/{command_id}/invoke` 新建或继续命令会话。
- `DELETE /api/plugins/{plugin_id}/extensions/sessions/{session_id}` 关闭会话。
- `GET /api/plugins/{plugin_id}/extensions/views/{view_id}/page` 读取页面 HTML。

命令请求、命令响应和单个页面文件不能超过 1 MiB。插件页面不直接调用这些 HTTP 接口，只通过前面的 `postMessage` 消息与 Dashboard 通信。

Dashboard 的 JSON 请求通过 `window.pywebview.api.request_api()` 发送。文件上传、下载和 SSE 不能走 JSON bridge，需要直接发送 HTTP 请求。这些请求先用 `window.pywebview.api.get_api_token()` 读取令牌，再带上 `Authorization: Bearer <token>` 请求头。除 `OPTIONS` 外，所有 `/api/` 请求都要验证令牌。令牌不正确时返回 403。

HTTP 编码值请求使用 `X-NMF-Value-Encoding: typed-v1`。返回值中的大整数、
精确小数和二进制采用 `$nmf_value` 编码，页面与 Dashboard 之间保持该表示，
引擎端解码后检查提交值。编码和普通对象转义格式见 [data-types.md](data-types.md)。
