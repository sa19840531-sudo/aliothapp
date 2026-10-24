"""Web 单阶段推进；浏览器副作用始终位于领取和结算事务之间。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from services.api.app.application.web_stage_lease import execute_with_stage_lease
from services.api.app.domain.exploration.web_run_stage_checkpoint import (
    WebRunStage,
    WebStageCursor,
    WebStageStatus,
    _code,
)


@dataclass(frozen=True, kw_only=True)
class WebStageEffectResult:
    """完成证据只携带已持久化引用；不把 DOM 或页面对象写入 checkpoint。"""

    artifact_ids: tuple[str, ...] = ()
    cursor_payload: Mapping[str, object] = field(default_factory=dict)
    authoritative_capture_id: str | None = None
    authoritative_page_type: str | None = None
    optional_primary_error_code: str | None = None
    reused_capture_id: str | None = None
    reused_logical_page_id: str | None = None

    def __post_init__(self):
        if self.reused_capture_id is not None or self.reused_logical_page_id is not None:
            if (
                not isinstance(self.reused_capture_id, str)
                or not isinstance(self.reused_logical_page_id, str)
                or not self.reused_logical_page_id.startswith("web-logical:")
                or len(self.reused_logical_page_id) > 512
            ):
                raise ValueError("分类复用必须同时携带原 Capture 和逻辑页引用")
            UUID(self.reused_capture_id)
        if self.optional_primary_error_code is not None:
            _code(self.optional_primary_error_code)
        # 权威详情事实仅写入操作证据，不扩展 checkpoint 的恢复引用白名单。
        if self.authoritative_capture_id is None and self.authoritative_page_type is None:
            return
        if (
            not isinstance(self.authoritative_capture_id, str)
            or len(self.authoritative_capture_id) != 36
            or not isinstance(self.authoritative_page_type, str)
            or self.authoritative_page_type not in {"content_detail", "video_detail"}
        ):
            raise ValueError("权威详情事实必须是 UUID 与详情类型的完整配对")
        UUID(self.authoritative_capture_id)


class WebStageFailure(RuntimeError):
    """适配器已归类的阶段错误，明细仅允许领域认可的恢复引用。"""

    def __init__(self, code: str, *, transient: bool = False, detail=None):
        super().__init__(code)
        self.code = code
        self.transient = transient
        self.detail = detail or {}


class WebStageEffects(Protocol):
    """证据查询必须核对幂等键、Run、Session、epoch 和 Capture 归属。"""

    async def find_completed(self, cursor: WebStageCursor) -> WebStageEffectResult | None: ...

    async def execute(self, cursor: WebStageCursor) -> WebStageEffectResult: ...


@dataclass(frozen=True, kw_only=True)
class WebStageAdvanceResult:
    cursor: WebStageCursor
    reused: bool = False

    @property
    def next_stage(self) -> WebRunStage | None:
        return self.cursor.next_stage


class WebRunStageOrchestrator:
    """一次调用最多领取并结算一个阶段；下一阶段由外层调度。"""

    def __init__(
        self,
        *,
        checkpoints,
        effects: WebStageEffects,
        worker_id: str,
        heartbeat_interval_seconds: float = 20,
    ):
        if not worker_id.strip():
            raise ValueError("阶段 Worker 标识不能为空")
        self._checkpoints = checkpoints
        self._effects = effects
        self._worker_id = worker_id
        if not 0 < heartbeat_interval_seconds <= 20:
            raise ValueError("阶段续租间隔必须在 0 到 20 秒之间")
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    async def advance_once(self, run_id: str, logical_page_key: str) -> WebStageAdvanceResult:
        current = await self._checkpoints.get(run_id=run_id, logical_page_key=logical_page_key)
        if current.is_terminal:
            return WebStageAdvanceResult(cursor=current)
        claimed = await self._checkpoints.claim(
            run_id=run_id,
            logical_page_key=logical_page_key,
            expected_row_version=current.row_version,
            worker_id=self._worker_id,
        )
        # 过期接管已耗尽预算时，仓储只签发 gap，不签发浏览器执行授权。
        if claimed.status is not WebStageStatus.RUNNING:
            return WebStageAdvanceResult(cursor=claimed)
        # 数据库查询和结算失败不得伪装成页面错误，更不能使用旧 token 再次回写。
        try:
            completed = await self._effects.find_completed(claimed)
        except WebStageFailure as exc:
            if (
                claimed.current_stage is not WebRunStage.CLASSIFICATION
                or exc.code not in {
                    "WEB_CLASSIFICATION_CAPTURE_MISMATCH", "WEB_CLASSIFICATION_REUSE_INVALID",
                }
            ):
                raise
            settled = await self._checkpoints.record_gap_and_advance(
                claimed, error_code=exc.code, detail=exc.detail
            )
            return WebStageAdvanceResult(cursor=settled)
        reused = completed is not None
        if completed is None:
            try:
                completed = await execute_with_stage_lease(
                    self._effects.execute(claimed),
                    renew=lambda: self._checkpoints.renew_lease(claimed),
                    interval_seconds=self._heartbeat_interval_seconds,
                )
            except WebStageFailure as exc:
                if exc.transient:
                    settled = await self._checkpoints.schedule_retry(
                        claimed, error_code=exc.code, transient=True, detail=exc.detail
                    )
                else:
                    settled = await self._checkpoints.record_gap_and_advance(
                        claimed, error_code=exc.code, detail=exc.detail
                    )
                return WebStageAdvanceResult(cursor=settled)
            except (TimeoutError, ConnectionError) as exc:
                settled = await self._checkpoints.schedule_retry(
                    claimed,
                    error_code=_transport_error(claimed.current_stage, exc),
                    transient=True,
                )
                return WebStageAdvanceResult(cursor=settled)
        settled = await self._checkpoints.mark_succeeded(
            claimed, artifact_ids=completed.artifact_ids, cursor_payload=completed.cursor_payload
        )
        return WebStageAdvanceResult(cursor=settled, reused=reused)


def _transport_error(stage: WebRunStage, error: Exception) -> str:
    if stage is WebRunStage.FULL_PAGE_CAPTURE:
        return (
            "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
            if isinstance(error, TimeoutError)
            else "WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED"
        )
    if stage in {WebRunStage.BOOTSTRAP_NAVIGATION, WebRunStage.CANDIDATE_NAVIGATION}:
        return "WEB_NAVIGATION_TRANSIENT_FAILURE"
    if stage is WebRunStage.PRIMARY_CAPTURE:
        return "WEB_PRIMARY_CAPTURE_FAILED"
    if stage is WebRunStage.AD_ISOLATED_NAVIGATION:
        return "WEB_AD_PAGE_OPEN_FAILED"
    return "WEB_STAGE_TRANSIENT_FAILURE"
