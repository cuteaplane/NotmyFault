"""跨触发器共享的原生调用串行化锁。

clipboard 与 window_title 触发器并发调用 ctypes（无/有 argtypes 的
数组参数转换都会写函数对象的 _objects 引用表）曾导致堆损坏
（0xc0000374）静默崩溃。原生段互斥后竞态消除，调用耗时都在微秒级，
不影响轮询频率。

必须用可重入的 RLock：PollingTrigger 基类会在外层持锁调用 poll()，
而 poll 内部调用的 _get_clipboard_text()/_get_window_titles() 等助手
也会再取一次这把锁。普通 Lock 二次获取会死锁。
"""
import threading

NATIVE_LOCK = threading.RLock()
