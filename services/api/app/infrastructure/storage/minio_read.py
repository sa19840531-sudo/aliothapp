"""MinIO 对象读取辅助能力。"""

from __future__ import annotations

import time
from typing import Any

from urllib3.exceptions import HTTPError as Urllib3HTTPError

_RETRYABLE_READ_ERRORS = (
    TimeoutError,
    ConnectionResetError,
    Urllib3HTTPError,
)


def read_minio_object_with_retry(
    client: Any,
    bucket: str,
    object_name: str,
    *,
    max_attempts: int = 3,
    backoff_seconds: float = 0.5,
) -> bytes:
    """读取 MinIO 对象，遇到瞬时网络读异常时重试。"""

    if max_attempts < 1:
        raise ValueError("MinIO 读取重试次数必须大于 0")

    for attempt in range(1, max_attempts + 1):
        response: Any | None = None
        try:
            response = client.get_object(bucket, object_name)
            return response.read()
        except _RETRYABLE_READ_ERRORS:
            if attempt >= max_attempts:
                raise
            time.sleep(backoff_seconds * attempt)
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    raise RuntimeError("MinIO 读取重试流程异常结束")
