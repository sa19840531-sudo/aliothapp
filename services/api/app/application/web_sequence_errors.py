"""只有已认证 Web 多用途合同比较可抛出的技术收口信号。"""


class WebRuleContractIncompatibleError(RuntimeError):
    """两份合法材料采用不同判定标准；不是产品失败或可重试租约错误。"""

    def __init__(self, message, *, command):
        super().__init__(message)
        self.command = command
