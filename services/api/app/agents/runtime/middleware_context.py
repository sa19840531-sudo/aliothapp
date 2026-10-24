"""工具调用中间件共享的调用级上下文键。

这些值存放在 ``FunctionInvocationContext.metadata`` 中；该字典按单次工具调用隔离，
可安全用于并行 Tool 调用，不得用 Run 级 ``WorkingMemory`` 标量替代。
"""

RETRY_COUNT_METADATA_KEY = "alioth.runtime.retry_count"
"""当前工具调用实际记录的重试次数。"""
