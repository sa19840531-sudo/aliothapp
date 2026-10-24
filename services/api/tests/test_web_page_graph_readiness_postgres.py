"""真实导航经过 readiness 独立 OBSERVE 后仍保留图身份，不复制原始事实。"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from services.api.app.application.capture_coordinator import CaptureCoordinator, CapturePhase
from services.api.app.application.web_logical_target import WebLogicalTargetClassifier
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_readiness import (
    WebObstructionAssessment,
    WebPageReadinessCoordinator,
)
from services.api.app.application.web_page_type_assessment import WebPageTypeAssessmentRecorder
from services.api.app.application.web_runtime_evidence import WebRuntimeEvidenceSource
from services.api.app.domain.interaction import CaptureKind, CaptureRef, InteractionOperation
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.page_graph_repository import PostgresPageGraphRepository
from services.api.app.infrastructure.db.session_command_gate import PostgresSessionCommandGate
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_logical_target_repository import (
    PostgresWebLogicalTargetRepository,
)
from services.api.tests.test_capture_coordinator import (
    RecordingEvidenceExitGate,
    RecordingInteractionExecutor,
)
from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics
from services.api.tests.test_web_page_graph_projection_postgres import (
    SnapshotContent,
    navigate_pair,
    target_database,
    write_capture,
)
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)
from services.api.tests.web_frozen_assessment_support import frozen_assessment_options

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离 PostgreSQL 双 DSN"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "known, drift",
    [
        (True, "none"),
        (False, "none"),
        (False, "unstable_before"),
        (False, "diagnostic_before"),
        (False, "broken_parent"),
        (False, "non_observe"),
        (False, "cycle"),
        (False, "out_of_order"),
        (False, "probe_spoof"),
        (False, "probe_version"),
        (False, "unstable_probe"),
        (False, "diagnostic_probe"),
        (False, "observe_url_change"),
    ],
)
async def test_real_readiness_observe_classification_preserves_navigation_identity(known, drift):
    await _assert_readiness_graph(known, drift)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt_patch",
    [
        {"location_changed": True},
        {"location_changed": 0},
        {"transition_kind": "new_page"},
        {"before_location": "https://foreign.test/"},
        {"after_location": "https://example.test/#/detail/2"},
        {"new_page_count": 1},
        {"scroll_effect": {"changed": True}},
        {"logical_surface_transition": {"status": "trusted"}},
    ],
)
async def test_observe_contradictory_receipt_cannot_inherit_identity(receipt_patch):
    await _assert_readiness_graph(
        False,
        "receipt_conflict",
        observe_state_diff={"location_changed": False, **receipt_patch},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift", ["action_unfinished", "action_error", "result_failure", "result_error"]
)
async def test_observe_unsuccessful_action_or_result_cannot_inherit_identity(drift):
    await _assert_readiness_graph(False, drift)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["observe_url_change", "non_observe", "unstable_before"])
async def test_null_observe_receipt_cannot_bypass_actual_capture_facts(drift):
    await _assert_readiness_graph(False, drift, observe_state_diff={"location_changed": None})


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_CHROME_EXECUTABLE"), reason="必须显式提供受控 Chrome 可执行路径"
)
@pytest.mark.parametrize("known", [True, False])
async def test_real_chrome_observe_null_receipt_preserves_readiness_graph_identity(known):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    from services.api.app.application.web_readiness_contracts import read_web_readiness_assessment
    from services.api.app.infrastructure.browser import page_capture as capture
    from services.api.app.infrastructure.browser.capture_runtime import _public_state_diff

    executable = os.environ["ALIOTH_TEST_CHROME_EXECUTABLE"]
    assert Path(executable).is_file(), "显式 Chrome 路径不存在，不将产品错误转为 skip"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            context = await browser.new_context()
            await context.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body="<html><title>本地证据</title><body><main><h1>正文详情</h1>"
                    "<p>正文内容</p></main></body></html>",
                ),
            )
            page = await context.new_page()
            logs, requests, inflight = [], [], {}
            capture._attach_observers(page, logs, requests, inflight)
            await page.goto("https://example.test/#/detail/1")
            result = await asyncio.wait_for(
                capture.PlaywrightPageCaptureBrowser()._capture_on_page_impl(
                    request=capture.PageCaptureRequest(
                        mode=capture.PageCaptureMode.OBSERVE,
                        startup_policy="direct_home",
                        web_capture_stage="web_page_ready_baseline",
                    ),
                    context=context,
                    page=page,
                    console_logs=logs,
                    network_requests=requests,
                    network_inflight=inflight,
                    timeout_error_type=PlaywrightTimeoutError,
                ),
                timeout=20,
            )
            assert result.execution_status == "success"
            assert read_web_readiness_assessment(result.metrics).can_serve_evidence
            assert result.action_trace == []
            diff = _public_state_diff(result)
            assert diff == {
                "transition_kind": None,
                "before_location": None,
                "after_location": None,
                "location_changed": None,
                "new_page_count": None,
            }
            print("REAL_CHROME_OBSERVE_DIFF", json.dumps(diff))
            await _assert_readiness_graph(known, "none", observe_state_diff=diff)
        finally:
            await browser.close()


async def _assert_readiness_graph(known, drift, *, observe_state_diff=None):
    async with target_database() as (connection, pool, other, ids):
        frozen_options = await frozen_assessment_options(connection, ids)
        probe_metrics = None
        if drift in {"unstable_probe", "diagnostic_probe"}:
            probe_metrics = {"web_readiness": _stable_capture_metrics()}
            if drift == "unstable_probe":
                probe_metrics["web_readiness"]["visible_loader"] = True
            else:
                probe_metrics["diagnostic_capture"] = True
        nav_before, probe, nav_action = await navigate_pair(
            connection, pool, ids, after_metrics=probe_metrics
        )
        source = WebRuntimeEvidenceSource(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            index_reader=PostgresWebCaptureEvidenceRepository(pool=pool, **frozen_options),
            snapshot_reader=SnapshotContent(),
        )

        class SamePageWriter:
            async def capture(self, request):
                role = (
                    "after_action"
                    if request.phase is CapturePhase.AFTER_ACTION
                    else "before_action"
                )
                metrics = None
                if role == "before_action" and drift in {"unstable_before", "diagnostic_before"}:
                    raw = _stable_capture_metrics()
                    metrics = {"web_readiness": raw}
                    if drift == "unstable_before":
                        raw["visible_loader"] = True
                    else:
                        metrics["diagnostic_capture"] = True
                cid = await write_capture(
                    connection,
                    pool,
                    ids,
                    url=(
                        "https://example.test/#/detail/2"
                        if drift == "observe_url_change" and role == "after_action"
                        else "https://example.test/#/detail/1"
                    ),
                    role=role,
                    parent=request.parent_capture_id,
                    action=request.action_execution_id,
                    sequence=request.session_command_sequence,
                    manage_lease=False,
                    metrics=metrics,
                )
                return CaptureRef(
                    task_id=request.task_id,
                    session_id=request.session_id,
                    capture_id=cid,
                    surface_kind=request.surface_kind,
                    capture_kind=CaptureKind.PAGE,
                    role=request.directive.role,
                    artifact_ids=(str(uuid4()),),
                    parent_capture_id=request.parent_capture_id,
                    action_execution_id=request.action_execution_id,
                )

        class ObserveExecutor(RecordingInteractionExecutor):
            async def execute(self, request):
                return replace(
                    await super().execute(request),
                    state_diff=(
                        observe_state_diff
                        if observe_state_diff is not None
                        else {"location_changed": drift == "observe_url_change"}
                    ),
                )

        class EvidenceReader:
            async def read(self, *, capture_id):
                return await source.read_page(capture_id=capture_id)

        class NoObstruction:
            async def assess_obstruction(self, **kwargs):
                return WebObstructionAssessment(
                    obstruction_present=False,
                    dismiss_candidate_id=None,
                    confidence=0.99,
                    reason="自然无遮挡",
                    human_verification_present=False,
                    age_gate_present=False,
                )

        class ActualActionCoordinator(CaptureCoordinator):
            async def coordinate(self, command):
                if drift == "non_observe":
                    command = replace(
                        command,
                        intent=replace(
                            command.intent,
                            action_type=InteractionOperation.CLICK,
                            semantic_target="同页按钮",
                        ),
                    )
                return await super().coordinate(command)

        coordinator = ActualActionCoordinator(
            capture_writer=SamePageWriter(),
            interaction_executor=ObserveExecutor([]),
            evidence_exit_gate=RecordingEvidenceExitGate(),
            session_command_gate=PostgresSessionCommandGate(
                repository=PostgresSurfaceSessionRepository(pool=pool),
                lease_owner="t6-review",
                session_owner="web-v2-workflow",
            ),
            action_execution_repository=PostgresActionExecutionRepository(pool=pool),
        )
        ready_result = await asyncio.wait_for(
            WebPageReadinessCoordinator(
                assessor=NoObstruction(), coordinator=coordinator, evidence_reader=EvidenceReader()
            ).prepare(source_capture_id=probe, page_role="detail"),
            timeout=10,
        )
        assert ready_result.is_ready and ready_result.ready_capture_id != probe
        ready_id = ready_result.ready_capture_id
        if observe_state_diff is not None:
            stored_diff = await connection.fetchval(
                "SELECT result_payload->'state_diff' FROM qc_action_execution "
                "WHERE result_capture_id=$1::uuid",
                ready_id,
            )
            assert json.loads(stored_diff) == observe_state_diff
        ready = await source.read_ready(capture_id=ready_id, page_role="detail")
        if drift == "action_unfinished":
            await connection.execute(
                "UPDATE qc_action_execution SET status='evidence_pending', "
                "result_capture_id=NULL, evidence_finished_at=NULL "
                "WHERE result_capture_id=$1::uuid",
                ready_id,
            )
        elif drift == "action_error":
            await connection.execute(
                "UPDATE qc_action_execution SET status='failed', error_code='injected' "
                "WHERE result_capture_id=$1::uuid",
                ready_id,
            )
        elif drift in {"result_failure", "result_error"}:
            await connection.execute(
                "UPDATE qc_action_execution SET result_payload=result_payload || $2::jsonb "
                "WHERE result_capture_id=$1::uuid",
                ready_id,
                json.dumps(
                    {"status": "failed"}
                    if drift == "result_failure"
                    else {"error_message": "失败回执不能冒充成功观察"}
                ),
            )
        observe_before = await connection.fetchval(
            "SELECT parent_page_capture_id::text FROM qc_page_capture WHERE id=$1::uuid", ready_id
        )
        if drift in {"broken_parent", "cycle"}:
            await connection.execute(
                "UPDATE qc_page_capture SET parent_page_capture_id=$1::uuid WHERE id=$2::uuid",
                ready_id if drift == "cycle" else None,
                observe_before,
            )
        elif drift == "out_of_order":
            await connection.execute(
                "UPDATE qc_page_capture SET session_command_sequence=3 WHERE id=$1::uuid", probe
            )
        elif drift in {"probe_spoof", "probe_version"}:
            await connection.execute(
                "UPDATE qc_page_capture SET metadata=metadata || $1::jsonb WHERE id=$2::uuid",
                '{"logical_page_id":"web-logical:forged"}'
                if drift == "probe_spoof"
                else '{"identity_policy_version":"web-url-identity.v1"}',
                probe,
            )

        class PersistingClassifier:
            async def classify(self, capture):
                assessment = WebPageAssessment(
                    page_type="content_detail" if known else None,
                    confidence=0.95 if known else 0.2,
                    evidence=("自然页面结构",),
                    needs_review_reason=None if known else "WEB_PAGE_STRUCTURE_INSUFFICIENT",
                )
                await WebPageTypeAssessmentRecorder(
                    repository=PostgresWebCaptureEvidenceRepository(pool=pool, **frozen_options),
                    **frozen_options,
                ).persist(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    session_id=ids["session"],
                    capture=capture,
                    assessment=assessment,
                )
                return assessment

        await asyncio.wait_for(
            WebLogicalTargetClassifier(
                classifier=PersistingClassifier(),
                repository=PostgresWebLogicalTargetRepository(pool=pool),
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
            ).classify(ready),
            timeout=10,
        )

        async def assert_graph():
            captures = await connection.fetch(
                "SELECT p.id::text, p.parent_page_capture_id::text AS parent_id, "
                "p.capture_role, p.url, "
                "p.page_type, p.metadata->>'logical_page_id' AS persisted_id, g.identity_status, "
                "g.page_state, g.counts_as_business_page "
                "FROM qc_page_capture p JOIN qc_page_graph_state g ON g.state_id=p.id "
                "WHERE p.task_id=$1::uuid ORDER BY p.page_seq",
                ids["task"],
            )
            edges = await connection.fetch(
                "SELECT action_execution_id, source_state_id::text, target_state_id::text, "
                "counts_as_business_arrival, proof FROM qc_page_graph_transition "
                "WHERE task_id=$1::uuid "
                "ORDER BY command_sequence",
                ids["task"],
            )
            nav = next(r for r in edges if r["action_execution_id"] == nav_action)
            assert nav["counts_as_business_arrival"] is (
                drift
                not in {
                    "out_of_order",
                    "probe_spoof",
                    "probe_version",
                    "unstable_probe",
                    "diagnostic_probe",
                }
            )
            assert nav["target_state_id"] == probe
            if drift == "none":
                assert all(r["identity_status"] == "confirmed" for r in captures)
            assert all(
                not r["counts_as_business_arrival"]
                for r in edges
                if r["action_execution_id"] != nav_action
            )
            for capture_id in (nav_before, probe, observe_before):
                original = next(r for r in captures if r["id"] == capture_id)
                if drift != "probe_spoof":
                    assert original["persisted_id"] is None
                assert original["page_type"] is None
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM qc_h5_page_type_assessment "
                        "WHERE page_capture_id=$1::uuid",
                        capture_id,
                    )
                    == 0
                )
            ready_state = next(r for r in captures if r["id"] == ready_id)
            assert ready_state["identity_status"] == ("confirmed" if drift == "none" else "unknown")
            assert ready_state["page_type"] == ("content_detail" if known else None)
            assert ready_state["page_state"] == "stable"
            if drift in {
                "unstable_before",
                "diagnostic_before",
                "unstable_probe",
                "diagnostic_probe",
            }:
                middle_id = probe if drift.endswith("probe") else observe_before
                middle = next(r for r in captures if r["id"] == middle_id)
                assert middle["counts_as_business_page"] is False
                if drift.startswith("unstable"):
                    assert middle["page_state"] == "loading"

        if drift in {"out_of_order", "probe_spoof", "probe_version"}:
            # 这些用例刻意篡改历史事实，先重建再检查旧导航，不用陈旧图自证。
            await asyncio.wait_for(
                PostgresPageGraphRepository(pool=pool).rebuild_run(
                    task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0
                ),
                timeout=10,
            )
        await assert_graph()
        await asyncio.wait_for(
            PostgresPageGraphRepository(pool=pool).rebuild_run(
                task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0
            ),
            timeout=10,
        )
        await assert_graph()
