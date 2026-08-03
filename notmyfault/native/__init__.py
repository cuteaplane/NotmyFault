"""多个线程修改 ctypes 函数对象会损坏堆，因此相关触发器共用一把可重入锁"""
import threading

NATIVE_LOCK = threading.RLock()
