"""Web 引导支线补充 Capture 和页型判定的独立落库边界。"""

from __future__ import annotations

from collections.abc import Mapping
from html import unescape

from services.api.app.application.web_guide_candidate import WebGuideCandidate
from services.api.app.application.web_guide_page import WebGuideAssessment, WebGuideLanding
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureResult,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class WebGuideEvidenceWriter:
    """只新增父 Capture 的 supplemental 子行，不修改主会话 current。"""

    def __init__(self, *, persistence, marker, gate):
        self._persistence = persistence
        self._marker = marker
        self._gate = gate

    async def persist(
        self, *, candidate: WebGuideCandidate, landing: WebGuideLanding,
        assessment: WebGuideAssessment, task_id: str, agent_run_id: str,
    ) -> tuple[str, tuple[str, ...]]:
        capture_id, refs = await self.capture(
            candidate=candidate, landing=landing,
            task_id=task_id, agent_run_id=agent_run_id,
        )
        await self.mark(
            page_capture_id=capture_id,
            source_capture_id=candidate.source_capture_id,
            assessment=assessment, task_id=task_id, agent_run_id=agent_run_id,
        )
        return capture_id, tuple(refs.values())

    async def capture(
        self, *, candidate: WebGuideCandidate, landing: WebGuideLanding,
        task_id: str, agent_run_id: str,
    ) -> tuple[str, dict[str, str]]:
        """主会话租约只覆盖补充证据落库；浏览器观测始终在租约之外。"""
        compact_html = landing.html[:1_000_000]
        elements = landing.ui_snapshot.get("elements")
        visible_text = " ".join(
            unescape(str(item.get("text") or item.get("aria_label") or ""))
            for item in elements[:300]
            if isinstance(item, Mapping) and item.get("visible") is not False
        )[:32_000] if isinstance(elements, list) else ""
        result = PageCaptureResult(
            execution_status="success",
            mode="goto",
            reachable=True,
            final_url=landing.final_url,
            title=landing.title[:500],
            status_code=None,
            artifacts=[
                PageCaptureArtifact("screenshot", None, landing.screenshot),
                PageCaptureArtifact("dom", None, compact_html),
                PageCaptureArtifact("visible_text", None, {
                    "text": visible_text, "source": "web_guide_landing_ui_snapshot",
                    "char_count": len(visible_text),
                }),
                PageCaptureArtifact("ui_snapshot", None, dict(landing.ui_snapshot)),
                PageCaptureArtifact("action_trace", None, [{
                    "action": "isolated_guide_get",
                    "candidate_id": candidate.candidate_id,
                    "source_capture_id": candidate.source_capture_id,
                    "redirect_chain": list(landing.redirect_chain),
                    "final_url": landing.final_url,
                }]),
            ],
            action_trace=[{
                "action": "isolated_guide_get",
                "candidate_id": candidate.candidate_id,
            }],
            metrics={"web_guide_branch": {
                "candidate_id": candidate.candidate_id,
                "source_capture_id": candidate.source_capture_id,
                "requested_url": candidate.destination_url,
                "redirect_chain": list(landing.redirect_chain),
                "final_url": landing.final_url,
            }},
        )
        session_id = f"web:{agent_run_id}"
        async with self._gate.hold(
            task_id=task_id, session_id=session_id, surface_kind=SurfaceKind.WEB,
        ) as permit:
            saved = await self._persistence.persist(
                task_id=task_id,
                agent_run_id=agent_run_id,
                result=result,
                parent_page_capture_id=candidate.source_capture_id,
                capture_role="supplemental",
                capture_purpose="web_guide_page",
                inherit_parent_semantics=False,
                surface_session_id=session_id,
                session_command_sequence=permit.command_sequence,
                finalization_epoch=permit.finalization_epoch,
            )
        refs = {item.artifact_type: item.artifact_id for item in saved.artifacts}
        if not {"screenshot", "dom", "ui_snapshot", "action_trace"}.issubset(refs):
            raise RuntimeError("WEB_GUIDE_ARTIFACTS_INCOMPLETE")
        return saved.page_capture_id, refs

    async def mark(
        self, *, page_capture_id: str, source_capture_id: str,
        assessment: WebGuideAssessment, task_id: str, agent_run_id: str,
    ) -> None:
        marked = await self._marker.mark(
            task_id=task_id,
            agent_run_id=agent_run_id,
            page_capture_id=page_capture_id,
            source_capture_id=source_capture_id,
            page_type=assessment.page_type,
            status=assessment.status,
            confidence=assessment.confidence,
            reason_code=assessment.reason_code,
        )
        if marked is not True:
            raise RuntimeError("WEB_GUIDE_ASSESSMENT_PERSIST_FAILED")


class PostgresWebGuideEvidenceMarker:
    """仅准许已经落库且有来源血缘的 Web 引导子 Capture 签发页型。"""

    def __init__(self, *, pool=None):
        self._pool = pool

    async def mark(
        self, *, task_id: str, agent_run_id: str, page_capture_id: str,
        source_capture_id: str, page_type: str | None, status: str,
        confidence: float, reason_code: str,
    ) -> bool:
        if page_type not in {None, "guide_page"} or status not in {"confirmed", "uncertain"}:
            raise ValueError("WEB_GUIDE_ASSESSMENT_INVALID")
        if (status == "confirmed") != (page_type == "guide_page"):
            raise ValueError("WEB_GUIDE_ASSESSMENT_INVALID")
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task_id}",
            )
            updated = await connection.fetchval(
                """
                UPDATE qc_page_capture AS child
                SET page_type=$5::text, page_state='loaded',
                    detection_status=CASE WHEN $5::text IS NULL
                        THEN 'insufficient' ELSE 'recognized' END,
                    detection_confidence=$7::double precision,
                    metadata=COALESCE(child.metadata,'{}'::jsonb) ||
                        jsonb_build_object('web_guide_assessment',
                            jsonb_build_object('status',$6::text,'page_type',$5::text,
                                'confidence',$7::double precision,'reason_code',$8::text)),
                    updated_at=now()
                FROM qc_page_capture AS parent, qc_capture AS child_capture,
                     qc_capture AS parent_capture, qc_task AS task
                WHERE child.id=$1::uuid AND child.task_id=$2::uuid
                  AND child.agent_run_id=$3::uuid
                  AND child.parent_page_capture_id=$4::uuid
                  AND child.metadata->>'capture_purpose'='web_guide_page'
                  AND child_capture.id=child.id AND child_capture.task_id=child.task_id
                  AND child_capture.capture_role='supplemental'
                  AND parent.id=child.parent_page_capture_id
                  AND parent.task_id=child.task_id AND parent.agent_run_id=child.agent_run_id
                  AND parent_capture.id=parent.id AND parent_capture.task_id=parent.task_id
                  AND task.id=child.task_id AND task.status='running'
                  AND task.finalization_state IN
                      ('open','exploration_draining','session_draining')
                  AND EXISTS (SELECT 1 FROM qc_evidence_artifact a
                      WHERE a.task_id=child.task_id AND a.page_capture_id=child.id
                        AND a.artifact_type='screenshot')
                  AND EXISTS (SELECT 1 FROM qc_evidence_artifact a
                      WHERE a.task_id=child.task_id AND a.page_capture_id=child.id
                        AND a.artifact_type='dom')
                RETURNING child.id
                """,
                page_capture_id, task_id, agent_run_id, source_capture_id,
                page_type, status, confidence, reason_code,
            )
        return updated is not None
