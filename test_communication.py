"""
通信总线集成测试脚本
用于验证UI和脚本之间的通信是否正常工作

运行方式：python test_communication.py
"""

import time
import sys
from notmyfault.communication_bridge import UIComBridge, ScriptComHandler, setup_communication, cleanup_communication
from notmyfault.communication_bus import get_communication_bus, Channels


class TestResults:
    """测试结果统计"""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.tests = []
    
    def add_test(self, name: str, passed: bool, message: str = ""):
        self.tests.append({"name": name, "passed": passed, "message": message})
        if passed:
            self.passed += 1
            print(f"✅ {name}")
        else:
            self.failed += 1
            print(f"❌ {name}: {message}")
    
    def summary(self):
        print(f"\n{'='*50}")
        print(f"测试结果: {self.passed} 通过, {self.failed} 失败")
        print(f"成功率: {self.passed}/{self.passed + self.failed}")
        print(f"{'='*50}")
        return self.failed == 0


def test_basic_publish_subscribe():
    """测试1: 基本的发布-订阅"""
    print("\n【测试1】基本的发布-订阅")
    results = TestResults()
    
    bus = get_communication_bus()
    received = []
    
    def callback(message):
        received.append(message.data)
    
    # 订阅
    bus.subscribe("test.channel", callback)
    
    # 发布
    bus.publish("test.channel", {"test": "data"})
    time.sleep(0.5)
    
    results.add_test(
        "发布-订阅",
        len(received) > 0 and received[0].get("test") == "data",
        f"收到的消息数: {len(received)}"
    )
    
    # 清理
    bus.unsubscribe("test.channel", callback)
    
    return results


def test_request_response():
    """测试2: 请求-响应模式"""
    print("\n【测试2】请求-响应模式")
    results = TestResults()
    
    bus = get_communication_bus()
    
    def request_handler(message):
        """处理请求"""
        bus.respond(message, {"result": "success", "value": 42})
    
    bus.subscribe("test.request", request_handler)
    
    # 发送请求
    response = bus.request("test.request", {"query": "test"}, timeout=2.0)
    
    results.add_test(
        "请求-响应",
        response is not None and response.data.get("value") == 42,
        f"响应数据: {response.data if response else '无'}"
    )
    
    return results


def test_ui_com_bridge():
    """测试3: UI通信桥接"""
    print("\n【测试3】UI通信桥接")
    results = TestResults()
    
    # 创建脚本端处理器
    script_handler = ScriptComHandler()
    script_handler.setup_handlers()
    
    # 创建UI端桥接
    ui_bridge = UIComBridge()
    
    time.sleep(0.5)
    
    # 测试请求引擎状态
    status = ui_bridge.request_engine_status()
    results.add_test(
        "请求引擎状态",
        status is not None and "status" in status,
        f"响应: {status}"
    )
    
    return results


def test_concurrent_messages():
    """测试4: 并发消息处理"""
    print("\n【测试4】并发消息处理")
    results = TestResults()
    
    bus = get_communication_bus()
    received = []
    
    def callback(message):
        received.append(message.data)
    
    bus.subscribe("test.concurrent", callback)
    
    # 快速发布多条消息
    for i in range(10):
        bus.publish("test.concurrent", {"index": i})
    
    time.sleep(1)
    
    results.add_test(
        "并发消息处理",
        len(received) == 10,
        f"期望10条消息，收到{len(received)}条"
    )
    
    bus.unsubscribe("test.concurrent", callback)
    
    return results


def test_rule_operations():
    """测试5: 规则操作通信"""
    print("\n【测试5】规则操作通信")
    results = TestResults()
    
    # 创建脚本端处理器
    script_handler = ScriptComHandler()
    script_handler.setup_handlers()
    
    # 创建UI端桥接
    ui_bridge = UIComBridge()
    
    time.sleep(0.3)
    
    # 测试创建规则
    test_rule = {
        "name": "test_rule",
        "event": {"type": "usb_insert", "params": {}},
        "actions": []
    }
    
    try:
        ui_bridge.create_rule(test_rule)
        results.add_test("创建规则", True)
    except Exception as e:
        results.add_test("创建规则", False, str(e))
    
    return results


def test_error_handling():
    """测试6: 错误处理"""
    print("\n【测试6】错误处理")
    results = TestResults()
    
    script_handler = ScriptComHandler()
    ui_bridge = UIComBridge()
    
    ui_bridge.setup_listeners()
    
    time.sleep(0.3)
    
    # 测试错误通知
    try:
        script_handler.notify_error("测试错误消息")
        results.add_test("错误通知", True)
    except Exception as e:
        results.add_test("错误通知", False, str(e))
    
    return results


def test_timeout_handling():
    """测试7: 超时处理"""
    print("\n【测试7】超时处理")
    results = TestResults()
    
    bus = get_communication_bus()
    
    # 发送请求但没有处理器来响应
    response = bus.request("test.timeout", {}, timeout=0.5)
    
    results.add_test(
        "请求超时处理",
        response is None,
        "请求应该超时返回None"
    )
    
    return results


def main():
    print("=" * 50)
    print("NotmyFault 通信总线集成测试")
    print("=" * 50)
    
    # 初始化
    setup_communication()
    time.sleep(0.5)
    
    all_results = TestResults()
    
    try:
        # 运行所有测试
        tests = [
            test_basic_publish_subscribe,
            test_request_response,
            test_ui_com_bridge,
            test_concurrent_messages,
            test_rule_operations,
            test_error_handling,
            test_timeout_handling,
        ]
        
        for test_func in tests:
            try:
                test_result = test_func()
                all_results.passed += test_result.passed
                all_results.failed += test_result.failed
            except Exception as e:
                print(f"❌ {test_func.__name__} 发生异常: {e}")
                all_results.failed += 1
        
    finally:
        # 清理
        cleanup_communication()
        time.sleep(0.2)
    
    # 打印总结
    print("\n" + "=" * 50)
    print("📊 全局测试结果")
    print("=" * 50)
    success = all_results.summary()
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
