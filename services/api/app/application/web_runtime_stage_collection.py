"""集合到列表再到详情的两跳子游标，不占用额外主菜单槽、不递归集合。"""

from dataclasses import replace
from datetime import UTC, datetime

from services.api.app.application.web_collection_navigation import validate_collection_arrival
from services.api.app.application.web_full_page_supplemental_capture import (
    WebFullPageSupplementalCommand,
)
from services.api.app.application.web_page_readiness import WEB_READINESS_DIAGNOSTIC_CODES
from services.api.app.application.web_run_stage_orchestrator import (
    WebStageEffectResult,
    WebStageFailure,
)
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor


class WebRuntimeStageCollection:
    def __init__(self, *, runtime, command, evidence, checkpoints, ads):
        self.runtime, self.command, self.evidence = runtime, command, evidence
        self.checkpoints, self.ads = checkpoints, ads
        self.service = getattr(runtime, "auxiliary_navigation_explorer", None)

    @staticmethod
    def owns(cursor):
        return cursor.logical_page_key.startswith(("collection:", "collection-detail:"))

    def validate_restored_navigation(self, cursor, outcome):
        validate_collection_arrival(
            self.command,
            requested_url=cursor.cursor_payload["requested_url"],
            final_url=outcome.cursor_payload.get("current_url", ""),
        )

    async def schedule(self, parent, *, detail=False):
        if datetime.now(UTC) >= self.command.deadline or self.checkpoints is None:
            return
        if not callable(getattr(self.service, "stage_collection_candidates", None)):
            return
        if self.owns(parent) and not (detail and parent.logical_page_key.startswith("collection:")):
            return
        facts = await self.evidence.capture_outcome(capture_id=parent.cursor_payload["capture_id"])
        allowed = {"content_list", "video_list"} if detail else {"collection_page"}
        if facts.get("page_type") not in allowed:
            return
        key = ("collection-detail:" if detail else "collection:") + parent.logical_page_key
        existing = await self.checkpoints.list_for_run(run_id=parent.run_id)
        if any(row.logical_page_key == key for row in existing):
            return
        candidates = await self.service.stage_collection_candidates(
            self.command, source_capture_id=parent.cursor_payload["capture_id"]
        )
        visited = {row.cursor_payload.get("requested_url") for row in existing}
        candidate = next((item for item in candidates if item.url not in visited), None)
        if candidate is None:
            raise WebStageFailure("WEB_COLLECTION_ENTRY_MISSING")
        await self.checkpoints.create(
            WebStageCursor.start(
                run_id=parent.run_id,
                logical_page_key=key,
                cursor_payload={
                    "session_id": self.command.session_id,
                    "finalization_epoch": parent.cursor_payload["finalization_epoch"],
                    "parent_capture_id": parent.cursor_payload["capture_id"],
                    "capture_id": parent.cursor_payload["capture_id"],
                    "requested_url": candidate.url,
                    "idempotency_key": candidate.candidate_id,
                },
            )
        )

    async def execute(self, cursor):
        stage, payload = cursor.current_stage, cursor.cursor_payload
        if stage is WebRunStage.BOOTSTRAP_NAVIGATION:
            capture_id = await self.service.stage_collection_navigate(
                self.command,
                source_capture_id=payload["parent_capture_id"],
                candidate_id=payload["idempotency_key"],
                requested_url=payload["requested_url"],
            )
            result = WebStageEffectResult(
                cursor_payload=await self.evidence.capture_reference(capture_id=capture_id)
            )
            self.validate_restored_navigation(cursor, result)
            return result
        if stage is WebRunStage.READINESS:
            result = await self.runtime.page_readiness.prepare(
                source_capture_id=payload["capture_id"], page_role="menu"
            )
            if result.status != "stable" or not result.ready_capture_id:
                from services.api.app.application.web_runtime_stage_effects import (
                    _readiness_stage_failure,
                )

                code, transient = _readiness_stage_failure(result)
                diagnostic = next((
                    reason for reason in getattr(result, "blocking_reasons", ())
                    if reason in WEB_READINESS_DIAGNOSTIC_CODES
                ), None)
                # 失败前的观察可能已换帧，重试只能引用当前会话的证据。
                reference = await self.evidence.current_capture_reference()
                raise WebStageFailure(
                    code, transient=transient,
                    detail={
                        **reference,
                        **({"raw_error_code": diagnostic} if diagnostic else {}),
                    },
                )
            return WebStageEffectResult(
                cursor_payload=await self.evidence.capture_reference(
                    capture_id=result.ready_capture_id
                )
            )
        if stage is WebRunStage.FULL_PAGE_CAPTURE:
            service = getattr(self.runtime, "pre_gate_supplemental_capture", None)
            if service is not None:
                result = await service.capture(
                    WebFullPageSupplementalCommand(
                        task_id=self.command.task_id,
                        agent_run_id=cursor.run_id,
                        session_id=self.command.session_id,
                        source_capture_id=payload["capture_id"],
                        source_url=payload["current_url"],
                        deadline=self.command.deadline,
                        stage_cursor=cursor,
                    )
                )
                if result.status != "captured":
                    raise WebStageFailure(result.reason_code)
                refreshed = await self.runtime.page_readiness.refresh_ready_baseline(
                    source_capture_id=result.source_capture_id, page_role="menu"
                )
                return WebStageEffectResult(
                    artifact_ids=result.screenshot_artifact_ids,
                    cursor_payload=await self.evidence.capture_reference(capture_id=refreshed),
                )
        if stage is WebRunStage.CLASSIFICATION:
            ready = await self.runtime.stage_ready_capture_reader.read(
                capture_id=payload["capture_id"], page_role="menu"
            )
            if cursor.artifact_ids:
                ready = replace(
                    ready,
                    supplemental_screenshot_artifact_ids=cursor.artifact_ids,
                    supplemental_evidence_status="captured",
                    supplemental_evidence_reason_code=None,
                )
            try:
                await self.runtime.page_type_classifier.classify(ready)
            except RuntimeError as exc:
                if str(exc) != "WEB_ASSESSMENT_SENSITIVE_CONTENT_PRESENT":
                    raise
                # 只终结当前合集支线，保留拒绝记录；敏感证据仍不送模型。
                raise WebStageFailure("WEB_COLLECTION_SENSITIVE_EVIDENCE_REJECTED") from exc
        if stage in {WebRunStage.CLASSIFICATION, WebRunStage.HREF_DISCOVERY}:
            await self.ads.schedule(cursor, self.checkpoints)
        if stage is WebRunStage.CANDIDATE_NAVIGATION:
            await self.schedule(cursor, detail=True)
        if stage is WebRunStage.EVIDENCE_RECONCILIATION:
            if any(
                gap.stage is WebRunStage.CLASSIFICATION
                and gap.error_code == "WEB_COLLECTION_SENSITIVE_EVIDENCE_REJECTED"
                for gap in cursor.gaps
            ):
                return WebStageEffectResult()
            facts = await self.evidence.capture_outcome(capture_id=payload["capture_id"])
            expected = (
                {"content_detail", "video_detail"}
                if cursor.logical_page_key.startswith("collection-detail:")
                else {"content_list", "video_list"}
            )
            if facts.get("page_type") not in expected:
                raise WebStageFailure("WEB_COLLECTION_TARGET_TYPE_MISMATCH")
        return WebStageEffectResult()
