"""引导页报告只投影当前 Web Run 的阶段与其补充 Capture。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_guide_report_projects_phase_status_and_landing_artifacts() -> None:
    from services.api.app.infrastructure.db.web_guide_report_snapshot import (
        read_web_guide_page_snapshot,
    )

    class Connection:
        async def fetch(self, query, *args):
            compact = " ".join(query.split())
            assert "phase.phase_key='web_guide_page'" in compact
            assert "phase.phase_protocol_version='web-guide-page.v1'" in compact
            assert "child.parent_page_capture_id=phase.source_capture_id" in compact
            assert "child.id=step.result_capture_id" in compact
            assert "decision_payload->>'capture_id'" not in compact
            assert "child_capture.capture_role='supplemental'" in compact
            assert "manifest.capture_id=child.id" in compact
            assert "manifest.manifest_version DESC" in compact
            assert "source_capture.metadata->>'finalization_epoch'=$3::integer::text" in compact
            assert args == ("task", "run", 3)
            return [{
                "source_capture_id": "source-1",
                "candidate_id": "candidate-1",
                "status": "completed",
                "decision_status": "confirmed",
                "reason_code": "WEB_GUIDE_LANDING_CONFIRMED",
                "capture_id": "guide-1",
                "page_type": "guide_page",
                "requested_url": "https://guide.example/app",
                "final_url": "https://guide.example/app",
                "screenshot_artifact_id": "shot-1",
                "manifest_quality_status": "pass",
                "artifact_ids": ["shot-1", "dom-1"],
            }]

    rows = await read_web_guide_page_snapshot(
        Connection(), task_id="task", agent_run_id="run", epoch=3,
    )
    assert rows == [{
        "source_capture_id": "source-1",
        "candidate_id": "candidate-1",
        "status": "confirmed",
        "reason_code": "WEB_GUIDE_LANDING_CONFIRMED",
        "capture_id": "guide-1",
        "page_type": "guide_page",
        "coverage_group": "evidence_only",
        "requested_url": "https://guide.example/app",
        "final_url": "https://guide.example/app",
        "screenshot_artifact_id": "shot-1",
        "artifact_ids": ["shot-1", "dom-1"],
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("quality_status", ["pending", "fail", None])
async def test_guide_report_never_confirms_unready_evidence(quality_status) -> None:
    from services.api.app.infrastructure.db.web_guide_report_snapshot import (
        read_web_guide_page_snapshot,
    )

    class Connection:
        async def fetch(self, _query, *_args):
            return [{
                "source_capture_id": "source", "candidate_id": "candidate",
                "status": "completed", "decision_status": "confirmed",
                "reason_code": "WEB_GUIDE_LANDING_CONFIRMED",
                "capture_id": "guide", "page_type": "guide_page",
                "final_url": "https://guide.example/", "screenshot_artifact_id": "shot",
                "artifact_ids": ["shot", "dom"],
                "manifest_quality_status": quality_status,
            }]

    rows = await read_web_guide_page_snapshot(
        Connection(), task_id="task", agent_run_id="run", epoch=1,
    )
    assert rows[0]["status"] == "incomplete"
    assert rows[0]["page_type"] is None
    assert rows[0]["screenshot_artifact_id"] is None
    assert rows[0]["artifact_ids"] == []
    assert rows[0]["reason_code"] == "WEB_GUIDE_EVIDENCE_NOT_READY"


def test_guide_page_is_separate_from_business_coverage_summary() -> None:
    from services.api.app.application.web_report_explanation import build_web_report_explanation

    result = build_web_report_explanation(
        task_id="task", agent_run_id="run", epoch=3,
        rows={
            "web_logical_pages": [{"page_type": "home", "coverage_group": "business"}],
            "web_guide_pages": [{
                "status": "confirmed", "page_type": "guide_page",
                "final_url": "https://private.example/app",
            }],
        },
    )
    assert result["page_type_summary"]["business_page_types"] == ["home"]
    assert result["page_type_summary"]["guide_page_count"] == 1
    assert "https://private.example" not in repr(result)


@pytest.mark.asyncio
async def test_guide_projection_failure_does_not_abort_main_web_report() -> None:
    from services.api.app.infrastructure.db.web_report_explanation_repository import (
        read_web_report_explanation,
    )

    class Connection:
        async def fetchrow(self, query, *_args):
            return {"finalization_epoch": 2} if "SELECT task.finalization_epoch" in query else None

        async def fetch(self, query, *_args):
            if "FROM qc_page_capture page JOIN qc_capture capture" in query:
                assert "page.metadata->>'capture_purpose','')<>'web_guide_page'" in query
            if "phase.phase_key='web_guide_page'" in query:
                raise RuntimeError("optional guide query failed")
            return []

    result = await read_web_report_explanation(
        Connection(), task_id="task", agent_run_id="run",
        scope_sql=" AND true", scope_args=(),
    )
    assert result is not None
    assert result["web_guide_pages"] == []
    assert result["page_type_summary"]["guide_page_count"] == 0
