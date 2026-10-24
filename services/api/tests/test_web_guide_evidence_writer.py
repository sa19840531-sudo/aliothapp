"""引导页附件落库独立于主线 Capture 与菜单。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest

from services.api.app.application.web_guide_candidate import WebGuideCandidate
from services.api.app.application.web_guide_page import WebGuideAssessment, WebGuideLanding
from services.api.app.infrastructure.db.web_guide_evidence import (
    PostgresWebGuideEvidenceMarker,
    WebGuideEvidenceWriter,
)


@pytest.mark.asyncio
async def test_guide_writer_uses_supplemental_capture_and_preserves_source() -> None:
    saved = []
    classified = []
    class Persistence:
        async def persist(self, **kwargs):
            saved.append(kwargs)
            return SimpleNamespace(
                page_capture_id="capture-2",
                artifacts=[
                    SimpleNamespace(artifact_id="shot", artifact_type="screenshot"),
                    SimpleNamespace(artifact_id="dom", artifact_type="dom"),
                    SimpleNamespace(artifact_id="ui", artifact_type="ui_snapshot"),
                    SimpleNamespace(artifact_id="trace", artifact_type="action_trace"),
                ],
            )
    class Marker:
        async def mark(self, **kwargs):
            classified.append(kwargs)
            return True
    class Gate:
        @asynccontextmanager
        async def hold(self, **kwargs):
            assert kwargs["session_id"] == "web:run-1"
            yield SimpleNamespace(command_sequence=4, finalization_epoch=2)
    candidate = WebGuideCandidate(
        candidate_id="candidate-1", source_capture_id="capture-1",
        source_url="https://site.example/", destination_url="https://guide.example/app",
        label="官方 APP", kind="page_candidate",
    )
    landing = WebGuideLanding(
        final_url="https://guide.example/app", title="下载 APP",
        html="<main>安卓下载</main>", screenshot=b"\x89PNG\r\n\x1a\nabc",
        ui_snapshot={"elements": []}, redirect_chain=("https://guide.example/app",),
    )
    writer = WebGuideEvidenceWriter(persistence=Persistence(), marker=Marker(), gate=Gate())
    capture_id, artifact_ids = await writer.persist(
        candidate=candidate, landing=landing,
        assessment=WebGuideAssessment(
            "confirmed", "guide_page", 0.85, "WEB_GUIDE_LANDING_CONFIRMED",
        ),
        task_id="task-1", agent_run_id="run-1",
    )
    assert capture_id == "capture-2"
    assert artifact_ids == ("shot", "dom", "ui", "trace")
    assert saved[0]["capture_role"] == "supplemental"
    assert saved[0]["capture_purpose"] == "web_guide_page"
    assert saved[0]["parent_page_capture_id"] == "capture-1"
    assert saved[0]["inherit_parent_semantics"] is False
    assert saved[0]["surface_session_id"] == "web:run-1"
    assert saved[0]["result"].final_url == landing.final_url
    assert {item.artifact_type for item in saved[0]["result"].artifacts} >= {
        "screenshot", "dom", "ui_snapshot", "action_trace"
    }
    visible = next(
        item.content for item in saved[0]["result"].artifacts
        if item.artifact_type == "visible_text"
    )
    assert visible["source"] == "web_guide_landing_ui_snapshot"
    assert classified[0]["page_type"] == "guide_page"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式隔离 ALIOTH_TEST_DATABASE_URL",
)
async def test_guide_marker_sql_binds_all_parameters_on_postgres() -> None:
    dsn = os.environ["ALIOTH_TEST_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    connection = await asyncpg.connect(dsn)

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield connection

    try:
        marked = await PostgresWebGuideEvidenceMarker(pool=Pool()).mark(
            task_id="00000000-0000-0000-0000-000000000001",
            agent_run_id="00000000-0000-0000-0000-000000000002",
            page_capture_id="00000000-0000-0000-0000-000000000003",
            source_capture_id="00000000-0000-0000-0000-000000000004",
            page_type=None,
            status="uncertain",
            confidence=0.4,
            reason_code="WEB_GUIDE_LANDING_NEEDS_REVIEW",
        )
        assert marked is False
    finally:
        await connection.close()
