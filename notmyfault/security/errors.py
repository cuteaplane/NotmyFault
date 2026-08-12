"""管理员命令被用户或当前授权状态阻止时使用的异常。"""


class AdminExecutionBlocked(PermissionError):
    """管理员命令未执行时停止当前动作的后续重试。"""
