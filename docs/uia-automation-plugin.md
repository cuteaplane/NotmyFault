# UIA 自动化插件

## 适用范围

`uia_automation` 是仅支持 Windows 的内置动作插件。插件负责 Windows UI Automation 控件选择、读取、操作、全局键鼠录制和操作宏回放。

NotmyFault 宿主只提供通用插件加载、权限声明、自有数据信封和扩展页面协议，不包含 UIA、键鼠录制或回放实现，也不识别选择器和操作宏的内部结构。

## 权限

插件声明以下权限：

- `native_api`：调用 Windows 原生 API 和 UI Automation COM API。
- `screen_reader`：读取窗口与控件信息。
- `input_monitor`：录制期间监听全局键盘和鼠标输入。

插件拒绝保存密码输入框的选择器。录制键盘输入前检查焦点控件是否为密码框，
查询失败、超时或判为密码控件时跳过该按键。

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

## 录制与回放

录制点击时，只采用鼠标消息继续分发前完成的控件采集结果，最多等待 50 ms。
采集超时后保留坐标操作，稍后返回的控件不会替换这次点击目标。拖动保持为鼠标
按下、移动、抬起步骤，滚轮保持为坐标滚动步骤。

键盘事件的密码判断和分组窗口查询也必须在消息继续分发前、50 ms 内完成。
未能及时确认时跳过按键，迟到结果不会补写录制事件。

每次录制最多保留 10000 个原始输入事件，达到上限后停止。等待执行的控件查询
最多 1 个。停止录制会丢弃排队查询；如果已经执行的 COM 查询仍未返回，页面
显示“录制已停止，控件查询尚未结束”，查询结束前不能开始下一次录制。停止录制
不能强制结束卡住的 COM 调用。

页面同一时间只发送一次录制状态查询。停止或取消后的旧查询响应不会恢复录制
状态；页面命令等待超过 30 秒时报错。

回放鼠标操作时，若 Windows 只发送了输入数组的一部分，插件只补发已经按下
且尚未释放的鼠标键释放事件，然后报告失败，不重放已经成功发送的前缀。

坐标回放校验当前虚拟桌面的坐标范围，不比较完整的显示器布局。更换显示器布局
后，即使原坐标仍在桌面内，也需要重新检查坐标步骤的目标。

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
node dashboard/tests/macro-page.mjs
```

模拟测试覆盖及时与迟到的控件查询、密码判断、录制容量、拖动和滚轮步骤、部分
鼠标输入后的释放，以及停止后的旧页面响应。测试使用假原生 API，不执行真实
键鼠输入，也不替代不同应用和显示器布局下的实际兼容性检查。
