"""广告子游标：复用隔离采集原语，以落盘回执恢复后两阶段。"""

from services.api.app.application.web_ad_landing_evidence import (
    WebAdDirectCaptureCommand,
    WebAdLandingCaptureError,
    select_web_ad_candidate,
)
from services.api.app.application.web_page_classification import (
    WEB_DETAIL_PAGE_TYPES,
    WEB_LIST_PAGE_TYPES,
)
from services.api.app.application.web_run_stage_orchestrator import (
    WebStageEffectResult,
    WebStageFailure,
)
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor


class WebRuntimeStageAds:
    def __init__(self, *, runtime, command, evidence=None):
        self._runtime = runtime
        self._command = command
        self._evidence = evidence

    async def schedule(self, cursor, checkpoints):
        reader = getattr(self._runtime, "ad_snapshot_reader", None)
        if (
            reader is None
            or getattr(self._runtime, "stage_ad_capture_port", None) is None
            or self._evidence is None
        ):
            return
        capture_id = cursor.cursor_payload["capture_id"]
        facts = await self._evidence.capture_outcome(capture_id=capture_id)
        page_type = facts.get("page_type") or cursor.cursor_payload.get("page_type")
        page_role = _business_page_role(page_type)
        if page_role is None:
            return
        # 每个业务页独立采集一条广告；仅同一逻辑页的恢复不得重复直开。
        existing = await checkpoints.list_for_run(run_id=cursor.run_id)
        ad_key = f"ad:{cursor.logical_page_key}"
        if any(item.logical_page_key == ad_key for item in existing):
            return
        snapshots = [(capture_id, await reader.read_snapshot(capture_id=capture_id))]
        candidate = select_web_ad_candidate(
            registered_domain=self._runtime.ad_registered_domain, capture_snapshots=snapshots,
        )
        if candidate is None:
            return
        await checkpoints.create(
            WebStageCursor.start(
                run_id=cursor.run_id,
                logical_page_key=ad_key,
                stage=WebRunStage.AD_ISOLATED_NAVIGATION,
                cursor_payload={
                    "session_id": self._command.session_id,
                    "finalization_epoch": cursor.cursor_payload["finalization_epoch"],
                    "capture_id": cursor.cursor_payload["capture_id"],
                    "parent_capture_id": candidate.source_capture_id,
                    "current_url": cursor.cursor_payload["current_url"],
                    "requested_url": candidate.url,
                },
            )
        )

    async def _request(self, cursor):
        payload = cursor.cursor_payload
        if self._evidence is None:
            page_type = payload.get("page_type")
        else:
            facts = await self._evidence.capture_outcome(capture_id=payload["capture_id"])
            page_type = facts.get("page_type")
        page_role = _business_page_role(page_type)
        if page_role is None:
            raise WebStageFailure("WEB_AD_SOURCE_PAGE_TYPE_MISSING")
        return WebAdDirectCaptureCommand(
            task_id=self._command.task_id,
            agent_run_id=cursor.run_id,
            session_id=self._command.session_id,
            logical_page_id=cursor.logical_page_key,
            source_capture_id=payload["capture_id"],
            source_url=payload["current_url"],
            destination_url=payload.get("requested_url", payload["current_url"]),
            candidate_capture_id=payload.get("parent_capture_id", payload["capture_id"]),
            candidate_kind="persisted_candidate",
            page_role=page_role,
            page_type=page_type,
            deadline=self._command.deadline,
        )

    async def execute(self, cursor):
        request = await self._request(cursor)
        port = self._runtime.stage_ad_capture_port
        try:
            receipt = await port.read_terminal(request)
            if receipt is None and cursor.current_stage is WebRunStage.AD_ISOLATED_NAVIGATION:
                # 原语在自身 finally 关闭隔离页；不把活页句柄跨 checkpoint 保存。
                receipt = await port.capture(request)
        except WebAdLandingCaptureError as exc:
            raise WebStageFailure(
                exc.reason_code,
                detail=exc.detail,
                transient=(
                    cursor.current_stage is WebRunStage.AD_ISOLATED_NAVIGATION
                    and exc.reason_code == "WEB_AD_PAGE_OPEN_FAILED"
                ),
            ) from None
        if receipt is None:
            if cursor.current_stage is WebRunStage.AD_CLOSEOUT and cursor.gaps:
                return WebStageEffectResult()
            raise WebStageFailure("WEB_AD_CAPTURE_EVIDENCE_MISSING")
        if receipt.page_type != "ad_page" or not receipt.source_restored:
            raise WebStageFailure(receipt.reason_code or "WEB_AD_LANDING_RETURN_FAILED")
        # 广告 Capture 仅做证据，不替换业务页的 current capture。
        return WebStageEffectResult(artifact_ids=receipt.artifact_ids)


def _business_page_role(page_type: object) -> str | None:
    if page_type == "home":
        return "home"
    if page_type in WEB_LIST_PAGE_TYPES:
        return "menu"
    if page_type in WEB_DETAIL_PAGE_TYPES:
        return "detail"
    return None
