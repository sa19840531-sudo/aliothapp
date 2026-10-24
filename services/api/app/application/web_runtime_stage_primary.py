"""正文与详情增强的可恢复窄端口，复用既有业务服务。"""

import asyncio
import logging
from dataclasses import replace

from services.api.app.application.web_page_classification import (
    WEB_DETAIL_PAGE_TYPES,
    WEB_LIST_PAGE_TYPES,
)
from services.api.app.application.web_run_stage_orchestrator import (
    WebStageEffectResult,
    WebStageFailure,
)
from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage

logger = logging.getLogger(__name__)


class WebRuntimeStagePrimary:
    def __init__(self, *, runtime, evidence, command, before_detail=None):
        self._runtime = runtime
        self._evidence = evidence
        self._command = command
        self._before_detail = before_detail

    async def enhance(self, cursor, *, page_type, frontier=None):
        detail_done = await self._evidence.find_completed(cursor, operation="detail")
        if detail_done is not None:
            return detail_done
        completed = await self._evidence.find_completed(cursor, operation="primary_content")
        capture_id = (
            completed.cursor_payload["capture_id"]
            if completed is not None
            else cursor.cursor_payload["capture_id"]
        )
        ready = await self._runtime.stage_ready_capture_reader.read(
            capture_id=capture_id, page_role="home" if cursor.logical_page_key == "home" else "menu"
        )
        if getattr(self._runtime, "primary_content_capture", None) is None:
            return WebStageEffectResult(cursor_payload={"capture_id": ready.capture_id})
        if completed is None:
            if page_type in WEB_LIST_PAGE_TYPES | WEB_DETAIL_PAGE_TYPES | {"home"}:
                completed = await self.capture_primary(cursor, ready=ready, page_type=page_type)
        reference = await self._evidence.capture_reference(
            capture_id=(
                completed.cursor_payload["capture_id"]
                if completed is not None
                else ready.capture_id
            )
        )
        return WebStageEffectResult(cursor_payload=reference)

    async def advance_detail(self, cursor, *, frontier):
        if (
            cursor.logical_page_key.startswith("detail:")
            or "menu_ledger_id" not in cursor.cursor_payload
            or getattr(self._runtime, "stage_detail_navigator", None) is None
        ):
            return None
        for source in (cursor, replace(cursor, current_stage=WebRunStage.CLASSIFICATION)):
            completed = await self._evidence.find_completed(source, operation="detail")
            if completed is not None:
                return completed
        facts = await self._evidence.capture_outcome(capture_id=cursor.cursor_payload["capture_id"])
        if facts["page_type"] not in WEB_LIST_PAGE_TYPES:
            return None
        reference = await self._evidence.current_capture_reference()
        return await self._capture_detail(
            cursor, frontier=frontier, page_type=facts["page_type"], reference=reference
        )

    async def _capture_detail(self, cursor, *, frontier, page_type, reference):
        if self._before_detail is not None:
            await self._before_detail(cursor, reference)
        candidate = await frontier.get_candidate(
            self._command, entry_id=cursor.cursor_payload["menu_ledger_id"]
        )
        detail = await self._runtime.stage_detail_navigator.capture_one(
            await self._runtime.stage_detail_evidence_source.read_list(
                capture_id=reference["capture_id"]
            ),
            list_page_type=page_type,
            command=self._command,
            entry=WebRuntimeMenuEntry.from_candidate(candidate),
        )
        optional_primary_error_code = None
        if detail.detail_capture_id:
            reference = await self._evidence.capture_reference(capture_id=detail.detail_capture_id)
            if detail.page_type in WEB_DETAIL_PAGE_TYPES and not detail.reused_capture_id:
                ready = await self._runtime.stage_ready_capture_reader.read(
                    capture_id=detail.detail_capture_id, page_role="detail"
                )
                try:
                    primary = await self.capture_primary(
                        cursor,
                        ready=ready,
                        page_type=detail.page_type,
                        operation="detail_primary_content",
                    )
                except WebStageFailure as exc:
                    # 详情正文增强失败独立留证，详情分类与广告分支仍继续。
                    primary = None
                    optional_primary_error_code = exc.code
                if primary is not None and primary.cursor_payload.get("capture_id"):
                    reference = await self._evidence.capture_reference(
                        capture_id=primary.cursor_payload["capture_id"]
                    )
                visual = getattr(self._runtime, "player_visual_recheck", None)
                if visual is not None and detail.page_type in {"content_detail", "video_detail"}:
                    try:
                        visual_capture_id = reference["capture_id"]
                        visual_ready = await self._runtime.stage_ready_capture_reader.read(
                            capture_id=visual_capture_id, page_role="detail"
                        )
                        observation = await visual.assess(
                            task_id=self._command.task_id,
                            agent_run_id=cursor.run_id,
                            session_id=self._command.session_id,
                            capture_id=visual_capture_id,
                            page_url=visual_ready.url,
                            page_type=detail.page_type,
                            deadline=self._command.deadline,
                            allow_scroll=optional_primary_error_code is None,
                        )
                        if observation.reason_code is None and observation.evidence_capture_id:
                            reference = await self._evidence.capture_reference(
                                capture_id=observation.evidence_capture_id
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - 可选视觉补判不得覆盖详情终态
                        logger.exception("Web 详情播放器视觉补判失败")
        await frontier.finalize_detail(self._command, cursor, detail=detail, page_type=page_type)
        outcome = WebStageEffectResult(
            cursor_payload=reference,
            optional_primary_error_code=optional_primary_error_code,
            authoritative_capture_id=(
                detail.detail_capture_id if detail.page_type in WEB_DETAIL_PAGE_TYPES else None
            ),
            authoritative_page_type=(
                detail.page_type
                if detail.detail_capture_id and detail.page_type in WEB_DETAIL_PAGE_TYPES
                else None
            ),
        )
        await self._evidence.save_completed(cursor, outcome, operation="detail")
        return outcome

    async def auxiliary(self, cursor):
        if not cursor.cursor_payload.get("capture_id"):
            # 根导航尚未形成可信主帧时，只允许证据对账和 Session 收口。
            return
        service = getattr(self._runtime, "auxiliary_navigation_explorer", None)
        if service is None:
            return
        completed = await self._evidence.find_completed(cursor, operation="auxiliary")
        if completed is not None:
            return
        read_current = getattr(self._evidence, "current_capture_reference", None)
        reference = await read_current() if read_current is not None else cursor.cursor_payload
        try:
            results = await service.explore_all(
                self._command, source_capture_id=reference["capture_id"]
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 可选辅助入口失败不得截断已冻结的菜单 href
            # 由阶段调度器记独立 gap 后推进到对账，不能静默吞掉诊断。
            raise WebStageFailure("WEB_AUXILIARY_NAVIGATION_FAILED") from None
        captures = [
            item.ready_capture_id or item.probe_capture_id
            for item in results
            if item.ready_capture_id or item.probe_capture_id
        ]
        outcome = WebStageEffectResult(
            cursor_payload={
                "capture_id": captures[-1] if captures else reference["capture_id"],
            }
        )
        await self._evidence.save_completed(cursor, outcome, operation="auxiliary")

    async def capture_primary(self, cursor, *, ready, page_type, operation="primary_content"):
        completed = await self._evidence.find_completed(cursor, operation=operation)
        if completed is not None:
            return completed
        service = getattr(self._runtime, "primary_content_capture", None)
        if service is None:
            return WebStageEffectResult()
        result = await service.capture(
            task_id=self._command.task_id,
            agent_run_id=cursor.run_id,
            session_id=self._command.session_id,
            capture_id=ready.capture_id,
            page_url=ready.url,
            page_type=page_type,
            deadline=self._command.deadline,
        )
        if result.status in {"failed", "blocked"}:
            raise WebStageFailure(result.reason_code or "WEB_PRIMARY_CONTENT_CAPTURE_FAILED")
        capture_id = result.evidence_capture_id or ready.capture_id
        outcome = WebStageEffectResult(cursor_payload={"capture_id": capture_id})
        await self._evidence.save_completed(cursor, outcome, operation=operation)
        return outcome
