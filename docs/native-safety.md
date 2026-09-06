# 原生调用安全（Native Call Safety）

原生调用错误可能在**没有任何 Python traceback** 的情况下结束整个进程。
引擎、API、托盘和日志会一起停止，只剩 Windows 事件日志里的
`0xc0000374`（堆损坏）或 `0xc0000005`（访问冲突）。

## 问题现象与原因（2026-07）

引擎启动后 2~60 秒内无日志退出。日志最后一行往往是某个动作正常完成，
例如 text_to_speech 显示“播报完成”。WER 显示 `pythonw.exe`、`ntdll.dll`
和 `0xc0000374`（STATUS_HEAP_CORRUPTION），没有可用堆栈。

定位：单线程探针（SAPI 播报、Toast、剪贴板、窗口枚举单独跑）全部正常；
用引擎的并发模式做多线程压测才复现：

| 组合 | 结果 |
| --- | --- |
| clipboard ×3 单独 | 存活 |
| window_title ×3 单独 | 存活 |
| psutil ×6 单独 | 存活 |
| clipboard ×3 + window_title ×3 | ~28s 堆损坏崩溃 |
| clipboard ×3 + psutil ×6 | 存活 |
| window_title ×3 + psutil ×6 | 存活 |

根因：**多个线程并发调用没有声明 `argtypes` 的 ctypes 函数**。ctypes 在
参数自动转换时会把转换结果引用记到函数对象共享的 `_objects` 表里保持
存活，并发写这张表形成竞态，最终写坏堆。event-v2 迁移后同类触发器线程
数量翻倍（如 clipboard 从 1 个变成 3 个），放大了触发概率。

修复：见下方规则 1 / 规则 2。

## 规则 1：ctypes 调用必须声明 argtypes / restype

所有 `ctypes.windll` / `ctypes.WinDLL` 函数调用必须显式声明参数与返回类型：

```python
user32 = ctypes.windll.user32
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.restype = ctypes.c_bool
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.GetClipboardData.restype = ctypes.c_void_p
```

这不仅防 64 位指针截断，也消除"每次调用现场推断类型 + 写共享引用表"的
竞态路径。参考实现：`notmyfault/triggers/clipboard/trigger.py` 与
`notmyfault/triggers/window_title/trigger.py`。

## 轮询型触发器的公共基类

所有轮询型触发器应继承 `notmyfault/triggers/base.PollingTrigger`（event-v2）：

- `interval`：轮询间隔（秒）
- `native=True`：poll 触达原生 API，基类自动持 `NATIVE_LOCK`
- `validate()`：配置校验，非法取值抛 ValueError
- `setup()` / `poll()` / `teardown()`：初始化 / 单次轮询 / 资源清理

除 `manual` 只等待退出外，20 个内置触发器均使用该基类，包括目录、空闲状态、
网络、进程、系统资源、每日定时和 USB 监控。新增轮询触发器使用基类；托盘的
窗口消息循环不使用该基类。

> 注意：`NATIVE_LOCK` 必须是可重入的 `threading.RLock`——基类在外层持锁
> 调用 poll()，而 poll() 内部的 `_get_clipboard_text()` / `_get_window_titles()`
> 等助手还会再取一次锁。普通 `Lock` 二次获取会死锁（曾导致触发器线程
> 全部卡死、冒烟测试挂 30 分钟）。

## 规则 2：跨线程的原生段必须互斥

凡是从多个触发器线程触达同一原生 API（剪贴板、窗口枚举、热键、电源广播等）
的代码段，必须包在共享锁里：

```python
from notmyfault.native import NATIVE_LOCK

def _poll():
    with NATIVE_LOCK:
        # 原生调用
        ...
```

`notmyfault.native.NATIVE_LOCK` 是全引擎共享的一把锁，用于串行访问共享的
ctypes 函数对象。**新增任何轮询型触发器，只要原生段不是一次性调用，就必须套这把锁。**

锁内不能同步等待另一个也需要此锁的线程。`power_state` 的隐藏窗口在持锁轮询时
处理消息；显示器开关使用 `SendNotifyMessageW` 广播，跨线程发送后立即返回。
持锁调用 `SendMessageW(HWND_BROADCAST, ...)` 会等待隐藏窗口处理消息，形成互相等待。

## 规则 3：不可信的原生代码放子进程

COM（如 SAPI 语音）、第三方音频驱动、以及任何"坏掉时可能把进程带崩"的
原生库，不要直接在引擎进程里调用。用子进程隔离：

```python
result = subprocess.run(
    [sys.executable, "-c", _HELPER],
    input=payload, capture_output=True, timeout=60,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
```

要点：

- 子进程 stdin 用二进制读取并显式按 UTF-8 解码：
  `json.loads(sys.stdin.buffer.read().decode("utf-8"))`
  （中文 Windows 文本模式 stdin 是 GBK，直接 `sys.stdin.read()` 会把
  UTF-8 中文读成乱码，SAPI 会念出 "ting-shen" 这类音）。
- 父进程用 `timeout=` 兜底卡死；子进程退出码非 0 时抛 `RuntimeError`
  让动作流水线标记失败。
- 参考实现：`notmyfault/actions/text_to_speech/action.py`。

## 排查方法

遇到"引擎无输出静默消失"：

1. 看 `%APPDATA%\NotmyFault\logs\engine-*.log` 最后一行，确认最后活动的组件。
2. 查 Windows 事件日志（应用程序）：`pythonw.exe` + 异常码。
   `0xc0000374` = 堆损坏，`0xc0000005` = 访问冲突——都是原生层问题。
3. 用多线程压测复现（单线程探针通常不会触发）：按引擎的真实线程数并发
   跑候选触发器，观察进程是否在几十秒内消失。
4. 二分组合（A+B 崩 / A 单独活 / B 单独活），定位交互的双方。

## 附：为什么没有 traceback

堆损坏 / 访问冲突发生在原生层，Python 解释器根本不知道；Windows 在堆管理
检查点直接终止进程。`faulthandler` 也救不了（它只处理 Python 级 fault）。
所以这类 bug 只能靠"规则 + 隔离"预防，而不是事后看日志。
