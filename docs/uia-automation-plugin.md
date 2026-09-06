# UIA 自动化插件

## 适用范围

`uia_automation` 是仅支持 Windows 的内置动作插件。插件负责 Windows UI Automation 控件选择、读取、操作、全局键鼠录制和操作宏回放。

NotmyFault 宿主只提供通用插件加载、权限声明、自有数据信封和扩展页面协议，不包含 UIA、键鼠录制或回放实现，也不识别选择器和操作宏的内部结构。

## 权限

插件声明以下权限：

- `native_api`：调用 Windows 原生 API 和 UI Automation COM API。
- `screen_reader`：读取窗口与控件信息。
- `input_monitor`：录制期间监听全局键盘和鼠标输入。

插件拒绝保存密码输入框的选择器，录制期间也不会保存密码控件中的键盘事件。

## 动作模式

插件只注册一个 `uia_automation` 动作，通过“自动化方式”参数选择行为。

| 自动化方式 | 用途 |
| --- | --- |
| `control` | 按下控件、聚焦控件或写入文本 |
| `focus_window` | 切换到所选控件所属的窗口 |
| `read_text` | 读取所选控件的文本 |
| `wait` | 等待所选控件出现 |
| `macro` | 按录制顺序回放 UIA、鼠标和键盘步骤 |

屏幕控件和操作宏都以插件自有数据保存。Dashboard 只显示通用 `plugin_data` 编辑入口，选择器采集和宏编辑页面由插件贡献。

录制点击时，只采用鼠标消息继续分发前完成的控件采集结果。采集超时后保留坐标操作，稍后返回的控件不会替换这次点击目标。

## Alpha 升级

以下内置动作已经移除，不会自动迁移：

| 旧动作 | 新动作设置 |
| --- | --- |
| `uia_control` | `uia_automation`，方式设为 `control` |
| `uia_focus_window` | `uia_automation`，方式设为 `focus_window` |
| `uia_read_text` | `uia_automation`，方式设为 `read_text` |
| `uia_wait` | `uia_automation`，方式设为 `wait` |
| `uia_macro` | `uia_automation`，方式设为 `macro` |

升级后，应在规则编辑器中删除旧动作步骤并添加新的 `uia_automation` 步骤，然后重新选择屏幕控件或重新录制操作宏。旧规则继续引用已移除动作时，规则校验会报告插件未加载。

## 检查

插件目录可执行以下检查：

```powershell
python nmf.py plugin check notmyfault/actions/uia_automation
pytest notmyfault/tests/test_uia_automation.py notmyfault/tests/test_uia_selector_extension.py notmyfault/tests/test_uia_input_recorder.py notmyfault/tests/test_uia_keyboard.py notmyfault/tests/test_uia_mouse.py notmyfault/tests/test_uia_native.py -q -p no:cacheprovider
```

当前检查结果为清单和平台校验通过，插件测试 69 项通过。宿主插件边界相关测试 141 项通过、1 项跳过。Dashboard 的构建、挂载测试和插件扩展页面测试通过。
