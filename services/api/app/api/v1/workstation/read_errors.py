"""工作台只读接口的可审计故障响应。"""

import logging
from uuid import uuid4

from services.api.app.core.errors import AliothError

logger = logging.getLogger(__name__)


class WorkstationReadError(AliothError):
    """数据库、对象存储等真实读取故障。"""


class ReportBackendError(WorkstationReadError):
    """报告中心真实后端读取故障。"""


def audited_read_error(
    *,
    operation: str,
    cause: Exception,
    task_id: str | None = None,
    report: bool = False,
) -> WorkstationReadError:
    """生成不泄露内部异常的 5xx，并把同一错误编号写入服务端日志。"""

    prefix = "report" if report else "workstation"
    error_id = f"{prefix}-{uuid4().hex[:16]}"
    logger.exception(
        "工作台读取失败 error_id=%s operation=%s task_id=%s cause_type=%s",
        error_id,
        operation,
        task_id,
        type(cause).__name__,
    )
    error_type = ReportBackendError if report else WorkstationReadError
    return error_type(
        f"服务读取失败，请重试；如持续失败请提供错误编号 {error_id}",
        status_code=500,
        error_id=error_id,
    )
