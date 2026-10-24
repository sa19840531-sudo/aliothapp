"""Web 辅助页独立游标：导航前落盘，广告收口后才发现下一辅助入口。"""

from datetime import UTC, datetime

from services.api.app.application.web_navigation_safety import (
    WebNavigationSafetyError,
    validate_web_final_url,
)
from services.api.app.application.web_run_stage_orchestrator import (
    WebStageEffectResult,
    WebStageFailure,
)
from services.api.app.domain.exploration.surface_phase import build_surface_phase_run_id
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor


class WebRuntimeStageAuxiliary:
    def __init__(self, *, runtime, command, evidence, checkpoints, ads, collection=None):
        self._runtime = runtime
        self._command = command
        self._evidence = evidence
        self._checkpoints = checkpoints
        self._ads = ads
        self._collection = collection
        self._service = getattr(runtime, "auxiliary_navigation_explorer", None)

    @property
    def enabled(self):
        return callable(getattr(self._service, "stage_candidates", None))

    @staticmethod
    def owns(cursor):
        return cursor.logical_page_key in {"aux:search", "aux:activity"}

    def validate_restored_navigation(self, cursor, outcome):
        """动作已提交不代表最终 URL 已验签；恢复也必须经过相同安全门。"""
        try:
            validate_web_final_url(
                site_url=self._command.target_url,
                requested_url=cursor.cursor_payload["requested_url"],
                final_url=outcome.cursor_payload.get("current_url", ""),
            )
        except WebNavigationSafetyError as exc:
            # 恢复证据查找不在阶段重试边界内，拒绝将不安全来源推进到新动作。
            raise WebStageFailure(exc.error_code) from exc

    async def schedule_next(self, parent):
        if not self.enabled or datetime.now(UTC) >= self._command.deadline:
            return
        existing = await self._checkpoints.list_for_run(run_id=parent.run_id)
        # 首页只能签发首个辅助页；它恢复时不得越过已有子任务创建下一导航。
        if parent.logical_page_key == "home" and any(self.owns(row) for row in existing):
            return
        if parent.logical_page_key == "aux:activity":
            return
        reference = await self._evidence.current_capture_reference()
        candidates = await self._service.stage_candidates(
            self._command, source_capture_id=reference["capture_id"],
        )
        for kind, key, phase_key in (
            ("search_href", "aux:search", "web_search_discovery"),
            ("activity_href", "aux:activity", "web_activity_navigation"),
        ):
            if any(row.logical_page_key == key for row in existing):
                continue
            candidate = next((item for item in candidates if item.kind == kind), None)
            if candidate is None:
                continue
            phase_id = build_surface_phase_run_id(
                task_id=self._command.task_id,
                session_id=self._command.session_id,
                phase_key=phase_key,
                idempotency_key=(
                    f"{parent.run_id}:{reference['capture_id']}:{candidate.candidate_id}"
                ),
            )
            await self._checkpoints.create(WebStageCursor.start(
                run_id=parent.run_id,
                logical_page_key=key,
                cursor_payload={
                    "session_id": self._command.session_id,
                    "finalization_epoch": parent.cursor_payload["finalization_epoch"],
                    **reference,
                    "parent_capture_id": reference["capture_id"],
                    "requested_url": candidate.url,
                    "idempotency_key": candidate.candidate_id,
                    "surface_phase_run_id": phase_id,
                },
            ))
            return

    async def execute(self, cursor):
        stage = cursor.current_stage
        payload = cursor.cursor_payload
        if stage is WebRunStage.BOOTSTRAP_NAVIGATION:
            capture_id = await self._service.stage_navigate(
                self._command,
                source_capture_id=payload["parent_capture_id"],
                candidate_id=payload["idempotency_key"],
                requested_url=payload["requested_url"],
            )
            return WebStageEffectResult(
                cursor_payload=await self._evidence.capture_reference(capture_id=capture_id),
            )
        if stage is WebRunStage.READINESS:
            ready = await self._runtime.page_readiness.prepare(
                source_capture_id=payload["capture_id"], page_role="menu",
            )
            if ready.status != "stable" or not ready.ready_capture_id:
                raise WebStageFailure("WEB_AUXILIARY_NOT_READY")
            return WebStageEffectResult(cursor_payload=await self._evidence.capture_reference(
                capture_id=ready.ready_capture_id,
            ))
        if stage is WebRunStage.CLASSIFICATION:
            ready = await self._runtime.stage_ready_capture_reader.read(
                capture_id=payload["capture_id"], page_role="menu",
            )
            await self._runtime.page_type_classifier.classify(ready)
            await self._ads.schedule(cursor, self._checkpoints)
        elif stage is WebRunStage.HREF_DISCOVERY:
            # 分类已落盘但阶段未结算时，过期接管可直接在此恢复同一广告子任务。
            await self._ads.schedule(cursor, self._checkpoints)
            if self._collection is not None:
                await self._collection.schedule(cursor)
        elif stage is WebRunStage.FRONTIER_DRAIN:
            # 分类已持久化但进程未登记广告时，从原帧补齐子任务；不重判或刷新期限。
            if datetime.now(UTC) < self._command.deadline:
                await self._ads.schedule(cursor, self._checkpoints)
            await self.schedule_next(cursor)
        elif stage is WebRunStage.EVIDENCE_RECONCILIATION:
            facts = await self._evidence.capture_outcome(capture_id=payload["capture_id"])
            expected = "search_page" if cursor.logical_page_key == "aux:search" else "activity"
            accepted = facts.get("page_type") == expected or (
                expected == "activity" and facts.get("page_type") == "collection_page"
            )
            await self._service.stage_finish(
                command=self._command, cursor=cursor,
                status="completed" if accepted else "failed",
                reason_code=(
                    "WEB_AUXILIARY_CLASSIFIED" if accepted else "WEB_AUXILIARY_PAGE_TYPE_MISMATCH"
                ),
                result_capture_id=payload["capture_id"],
                page_type=facts.get("page_type") if accepted else None,
            )
        # 辅助页不创建菜单、详情或全页补图，不负责关闭共享 Session。
        return WebStageEffectResult()
