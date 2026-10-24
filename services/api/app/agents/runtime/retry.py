"""工具调用失败自动重试：对可重试异常类型在同一次调用尝试内自动重试若干次。

必须作为中间件链条的"最内层"（即 ``agent.run(middleware=[...])`` 列表最后一项，
紧贴真正的工具执行），确保重试对 ``ActionTraceMiddleware``/``LoopGuardMiddleware``
而言是透明的——它们看到的仍是"一次调用尝试成功或失败"，不会因为内部重试了几次就
多算几个步数、或触发卡死检测的连续调用计数误判。实际重试次数通过调用级
``FunctionInvocationContext.metadata`` 传给 ``ActionTraceMiddleware``，避免并行
Tool 调用共享 Run 级标量而互相覆盖。

本中间件重试的是"工具函数自身执行失败"（如内部 HTTP 调用超时/连接中断），
不是大模型 API 调用失败——后者由 ``infrastructure/llm_gateway`` 构造的
``AsyncOpenAI``/``AsyncAzureOpenAI`` 客户端自带的 ``max_retries`` 负责，
两者职责不重叠。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.runtime.middleware_context import RETRY_COUNT_METADATA_KEY

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryConfig:
    """工具调用重试的可配置策略。"""

    max_attempts: int = 3
    """单次调用尝试允许的最大总次数（含首次尝试，非"额外重试次数"）；
    小于等于 1 表示不重试。"""

    backoff_seconds: float = 0.5
    """相邻两次重试之间的固定退避等待时间（秒）；设为 0 则不等待直接重试。"""

    retryable_exceptions: tuple[type[Exception], ...] = (TimeoutError, ConnectionError, OSError)
    """判定为"可重试"的异常类型；命中以外的异常直接向上抛出，不重试。"""


class RetryMiddleware(FunctionMiddleware):
    """对可重试异常自动重试工具调用，重试间隔为固定退避时间。"""

    def __init__(self, config: RetryConfig | None = None) -> None:
        self._config = config if config is not None else RetryConfig()

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        config = self._config
        max_attempts = max(config.max_attempts, 1)
        context.metadata[RETRY_COUNT_METADATA_KEY] = 0
        for attempt in range(1, max_attempts + 1):
            try:
                await call_next()
                return
            except config.retryable_exceptions as exc:
                context.metadata[RETRY_COUNT_METADATA_KEY] = attempt
                if attempt >= max_attempts:
                    logger.warning(
                        "工具「%s」调用失败，已达最大重试次数 %d，放弃重试：%s",
                        context.function.name,
                        max_attempts,
                        exc,
                    )
                    raise
                logger.info(
                    "工具「%s」调用失败（第 %d 次尝试），将于 %.2fs 后重试：%s",
                    context.function.name,
                    attempt,
                    config.backoff_seconds,
                    exc,
                )
                if config.backoff_seconds > 0:
                    await asyncio.sleep(config.backoff_seconds)
