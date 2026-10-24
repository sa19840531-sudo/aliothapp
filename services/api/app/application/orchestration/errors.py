"""确定性编排工作流错误。"""


class OrchestrationWorkflowError(RuntimeError):
    """Microsoft Agent Framework 工作流未产生有效终态输出。"""
