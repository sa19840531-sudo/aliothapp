"""Web 分类写回必须在真实单连接事务中同步页面图。"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_type_assessment import WebPageTypeAssessmentRecorder
from services.api.app.application.web_runtime_evidence import WebRuntimeEvidenceSource
from services.api.app.infrastructure.db.page_capture_repository import PostgresPageCaptureRepository
from services.api.app.infrastructure.db.page_graph_repository import PostgresPageGraphRepository
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
    WebCapturePageTypePersistenceError,
)
from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics
from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)
from services.api.tests.web_frozen_assessment_support import frozen_assessment_options

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离 PostgreSQL 双 DSN"
)


class SnapshotContent:
    """只替代附件对象存储，数据库 Writer、索引和就绪读取均使用真实实现。"""

    async def read_ui_snapshot(self, **kwargs):
        return {"web_capture_stage": "web_page_ready_baseline", "elements": []}


async def write_capture(
    connection,
    pool,
    ids,
    *,
    url="https://example.test/#/list",
    metrics=None,
    role="primary",
    parent=None,
    action=None,
    sequence=1,
    manage_lease=True,
):
    capture_id = str(uuid4())
    if manage_lease:
        await connection.execute(
            "UPDATE qc_surface_session SET lease_token=$2::uuid, lease_owner='t6-writer', "
            "lease_expires_at=now()+interval '5 minutes',command_sequence=$3 "
            "WHERE session_id=$1",
            ids["session"],
            str(uuid4()),
            sequence,
        )
    metrics = (
        metrics
        if metrics is not None
        else {"web_readiness": _stable_capture_metrics(identity_before=url, identity_after=url)}
    )
    await asyncio.wait_for(
        PostgresPageCaptureRepository(pool=pool).save_capture(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            page_capture_id=capture_id,
            url=url,
            mode="web",
            reachable=True,
            title="测试业务页",
            status_code=200,
            action_log=[],
            artifacts=[
                PersistedEvidenceArtifact(
                    str(uuid4()), kind, f"t6/{capture_id}/{kind}", mime, "sha256:" + "a" * 64, 1, {}
                )
                for kind, mime in (
                    ("screenshot", "image/png"),
                    ("dom", "text/html"),
                    ("visible_text", "text/plain"),
                )
            ],
            metrics=metrics,
            capture_role=role,
            surface_session_id=ids["session"],
            session_command_sequence=sequence,
            finalization_epoch=0,
            parent_page_capture_id=parent,
            action_execution_id=action,
        ),
        timeout=10,
    )
    await connection.execute(
        "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
        capture_id,
        ids["session"],
    )
    return capture_id


async def ready_capture(pool, ids, capture_id):
    return await WebRuntimeEvidenceSource(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        index_reader=PostgresWebCaptureEvidenceRepository(pool=pool),
        snapshot_reader=SnapshotContent(),
    ).read_ready(capture_id=capture_id, page_role="menu")


async def classify(pool, ids, capture, *, known=True):
    async with pool.acquire() as connection:
        options = await frozen_assessment_options(connection, ids)
    return await asyncio.wait_for(
        WebPageTypeAssessmentRecorder(
            repository=PostgresWebCaptureEvidenceRepository(pool=pool, **options),
            **options,
        ).persist(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            capture=capture,
            assessment=WebPageAssessment(
                page_type="content_list" if known else None,
                confidence=0.95 if known else 0.2,
                evidence=("自然页面结构",),
                needs_review_reason=None if known else "WEB_PAGE_STRUCTURE_INSUFFICIENT",
            ),
        ),
        timeout=10,
    )


async def graph_state(connection, capture_id):
    return await connection.fetchrow(
        "SELECT * FROM qc_page_graph_state WHERE state_id=$1::uuid", capture_id
    )


@pytest.mark.asyncio
async def test_real_writer_ready_classification_refreshes_same_capture_and_is_idempotent():
    async with target_database() as (connection, pool, _other, ids):
        capture_id = await write_capture(connection, pool, ids)
        initial = await graph_state(connection, capture_id)
        assert initial["identity_status"] == "unknown"
        ready = await ready_capture(pool, ids, capture_id)
        assert ready.inspection_readiness.status.value == "stable"
        first = await classify(pool, ids, ready)
        state = await graph_state(connection, capture_id)
        assert state["page_state"] == "stable"
        assert state["page_type"] == "content_list"
        assert state["identity_status"] == "confirmed"
        assert state["counts_as_business_page"] is True
        assert await classify(pool, ids, ready) == first
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_page_graph_state WHERE state_id=$1::uuid", capture_id
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_page_type_assessment WHERE page_capture_id=$1::uuid",
                capture_id,
            )
            == 1
        )


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_assessment_and_capture(monkeypatch):
    async with target_database() as (connection, pool, _other, ids):
        capture_id = await write_capture(connection, pool, ids)
        ready = await ready_capture(pool, ids, capture_id)
        original = dict(await graph_state(connection, capture_id))

        common_metadata = await connection.fetchval(
            "SELECT metadata FROM qc_capture WHERE id=$1::uuid", capture_id
        )

        async def fail_projection(self, connection, **kwargs):
            await connection.execute("SELECT 1/0")

        monkeypatch.setattr(
            PostgresPageGraphRepository, "project_capture_on_connection", fail_projection
        )
        with pytest.raises(Exception, match="division by zero"):
            await classify(pool, ids, ready)
        assert dict(await graph_state(connection, capture_id)) == original
        assert (
            await connection.fetchval(
                "SELECT metadata FROM qc_capture WHERE id=$1::uuid", capture_id
            )
            == common_metadata
        )
        assert (
            await connection.fetchval(
                "SELECT page_type FROM qc_page_capture WHERE id=$1::uuid", capture_id
            )
            is None
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_page_type_assessment WHERE page_capture_id=$1::uuid",
                capture_id,
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["epoch", "run"])
async def test_stale_epoch_or_latest_run_rejects_callback_without_graph_write(drift):
    async with target_database() as (connection, pool, _other, ids):
        capture_id = await write_capture(connection, pool, ids)
        ready = await ready_capture(pool, ids, capture_id)
        original = dict(await graph_state(connection, capture_id))
        if drift == "epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid", ids["task"]
            )
        else:
            await connection.execute(
                "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status) "
                "VALUES($1::uuid,$2::uuid,'main','新运行','running')",
                str(uuid4()),
                ids["task"],
            )
        with pytest.raises(WebCapturePageTypePersistenceError):
            await classify(pool, ids, ready)
        assert dict(await graph_state(connection, capture_id)) == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url, canonical",
    [
        ("https://example.test/?q=a%20b&tag=x&tag=y", "https://example.test/?q=a%20b&tag=x&tag=y"),
        (
            "https://example.test/#/detail/1?tag=x&tag=y&tab=info",
            "https://example.test/#/detail/1?tag=x&tag=y",
        ),
        ("https://example.test/#!/detail/2?q=%2f&page=2", "https://example.test/#!/detail/2?q=%2f"),
        (
            "https://example.test/article/1?utm_source=feed#comments",
            "https://example.test/article/1",
        ),
    ],
)
async def test_frozen_graph_key_matches_ledger_and_trusted_report_identity(url, canonical):
    from services.api.app.infrastructure.db.web_capture_identity import read_web_capture_identities
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )

    async with target_database() as (connection, pool, _other, ids):
        capture_id = await write_capture(connection, pool, ids, url=url)
        await classify(pool, ids, await ready_capture(pool, ids, capture_id))
        expected = "web-logical:" + sha256(canonical.encode()).hexdigest()
        target = await PostgresWebLogicalTargetRepository(pool=pool).claim(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            source_capture_id=capture_id,
            url=url,
        )
        report_identity = (
            await read_web_capture_identities(
                connection, task_id=ids["task"], agent_run_id=ids["run"]
            )
        )[capture_id]
        assert target["logical_page_id"] == report_identity["logical_page_id"] == expected
        assert (await graph_state(connection, capture_id))["logical_page_key"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing_policy", "conflicting_id", "wrong_owner"])
async def test_real_web_identity_failure_cannot_fall_back_to_legacy_metadata(corruption):
    async with target_database() as (connection, pool, _other, ids):
        capture_id = await write_capture(connection, pool, ids)
        await classify(pool, ids, await ready_capture(pool, ids, capture_id))
        if corruption == "missing_policy":
            await connection.execute(
                "DELETE FROM qc_web_run_policy_snapshot WHERE agent_run_id=$1::uuid", ids["run"]
            )
        elif corruption == "conflicting_id":
            await connection.execute(
                "UPDATE qc_page_capture SET metadata=metadata || "
                '\'{"logical_page_id":"web-logical:wrong"}\'::jsonb WHERE id=$1::uuid',
                capture_id,
            )
        else:
            await connection.execute(
                "UPDATE qc_surface_session SET session_owner='h5-runtime' WHERE session_id=$1",
                ids["session"],
            )
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || "
            '\'{"business_page_identity":"html-forged-page"}\'::jsonb WHERE id=$1::uuid',
            capture_id,
        )
        await PostgresPageGraphRepository(pool=pool).project_capture_from_database_on_connection(
            connection, capture_id=capture_id
        )
        state = await graph_state(connection, capture_id)
        assert state["identity_status"] == "unknown"
        assert state["counts_as_business_page"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["human", "advertisement", "loading", "diagnostic"])
async def test_blocked_or_unnatural_capture_cannot_become_business_by_classification(block):
    from services.api.app.application.web_readiness_contracts import read_web_readiness_assessment
    from services.api.tests.test_web_page_type_assessment import _capture

    async with target_database() as (connection, pool, _other, ids):
        raw = _stable_capture_metrics()
        metrics = {"web_readiness": raw}
        if block == "human":
            raw["human_verification_present"] = True
        elif block == "advertisement":
            raw["blocking_layer"] = "advertisement"
        elif block == "loading":
            raw["visible_loader"] = True
        else:
            metrics["diagnostic_evidence"] = True
        capture_id = await write_capture(connection, pool, ids, metrics=metrics)
        capture = replace(
            _capture(),
            capture_id=capture_id,
            url="https://example.test/#/list",
            inspection_readiness=read_web_readiness_assessment(metrics),
        )
        await classify(pool, ids, capture)
        state = await graph_state(connection, capture_id)
        assert state["counts_as_business_page"] is False
        assert state["page_state"] == capture.inspection_readiness.status.value


async def navigate_pair(
    connection, pool, ids, *, after_url="https://example.test/#/detail/1", after_metrics=None
):
    from services.api.app.application.capture_coordinator import CaptureCoordinator, CapturePhase
    from services.api.app.domain.interaction import CaptureKind, CaptureRef
    from services.api.app.infrastructure.db.action_execution_repository import (
        PostgresActionExecutionRepository,
    )
    from services.api.app.infrastructure.db.session_command_gate import PostgresSessionCommandGate
    from services.api.app.infrastructure.db.surface_session_repository import (
        PostgresSurfaceSessionRepository,
    )
    from services.api.tests.test_capture_coordinator import (
        RecordingEvidenceExitGate,
        RecordingInteractionExecutor,
        _command,
    )

    baseline = await write_capture(connection, pool, ids)
    await classify(pool, ids, await ready_capture(pool, ids, baseline))
    await connection.execute(
        "UPDATE qc_surface_session SET lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL "
        "WHERE session_id=$1",
        ids["session"],
    )

    class DatabaseWriter:
        async def capture(self, request):
            is_after = request.phase is CapturePhase.AFTER_ACTION
            capture_id = await write_capture(
                connection,
                pool,
                ids,
                url=after_url if is_after else "https://example.test/#/list",
                role="after_action" if is_after else "before_action",
                parent=request.parent_capture_id,
                action=request.action_execution_id,
                sequence=request.session_command_sequence,
                manage_lease=False,
                metrics=after_metrics if is_after else None,
            )
            return CaptureRef(
                task_id=request.task_id,
                session_id=request.session_id,
                capture_id=capture_id,
                surface_kind=request.surface_kind,
                capture_kind=CaptureKind.PAGE,
                role=request.directive.role,
                artifact_ids=(str(uuid4()),),
                parent_capture_id=request.parent_capture_id,
                action_execution_id=request.action_execution_id,
            )

    class NaturalExecutor(RecordingInteractionExecutor):
        async def execute(self, request):
            return replace(await super().execute(request), state_diff={"location_changed": True})

    command = _command()
    command = replace(
        command,
        agent_run_id=ids["run"],
        intent=replace(
            command.intent,
            task_id=ids["task"],
            session_id=ids["session"],
            source_capture_id=baseline,
            intent_id=str(uuid4()),
            idempotency_key=str(uuid4()),
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        ),
    )
    coordinator = CaptureCoordinator(
        capture_writer=DatabaseWriter(),
        interaction_executor=NaturalExecutor([]),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        session_command_gate=PostgresSessionCommandGate(
            repository=PostgresSurfaceSessionRepository(pool=pool),
            lease_owner="t6-coordinator",
            session_owner="web-v2-workflow",
        ),
        action_execution_repository=PostgresActionExecutionRepository(pool=pool),
    )
    result = await asyncio.wait_for(coordinator.coordinate(command), timeout=10)
    before, after = result.before_capture.capture_id, result.after_capture.capture_id
    return before, after, result.interaction.action_execution_id


@pytest.mark.asyncio
async def test_unknown_type_keeps_proven_after_identity_and_refreshes_related_edge():
    from services.api.app.application.page_graph import PageGraphService

    async with target_database() as (connection, pool, _other, ids):
        before, after, action = await navigate_pair(connection, pool, ids)
        # 自然导航本身已证明身份，分类只是独立类型事实，不再作为到达的前置循环。
        assert await connection.fetchval(
            "SELECT counts_as_business_arrival FROM qc_page_graph_transition "
            "WHERE action_execution_id=$1",
            action,
        )
        assert (await graph_state(connection, after))["page_type"] is None
        ready = await ready_capture(pool, ids, after)
        await classify(pool, ids, ready, known=False)
        state = await graph_state(connection, after)
        assert state["page_type"] is None
        assert state["identity_status"] == "confirmed"
        assert (
            state["logical_page_key"]
            == "web-logical:" + sha256(b"https://example.test/#/detail/1").hexdigest()
        )
        assert (
            await connection.fetchval(
                "SELECT counts_as_business_arrival FROM qc_page_graph_transition "
                "WHERE action_execution_id=$1",
                action,
            )
            is True
        )
        await classify(pool, ids, ready, known=False)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_page_graph_transition WHERE action_execution_id=$1", action
            )
            == 1
        )
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0)
        repository = PostgresPageGraphRepository(pool=pool)
        await asyncio.wait_for(repository.rebuild_run(**scope), timeout=10)
        graph = await PageGraphService(repository=repository).get_graph(**scope)
        transition = next(
            row for row in graph["transitions"] if row["action_execution_id"] == action
        )
        assert transition["counts_as_business_arrival"] is True
        assert isinstance(transition["outcome_payload"], dict)
        assert (await graph_state(connection, after))["logical_page_key"] == state[
            "logical_page_key"
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "presentation",
    [
        {
            "topmost_layer_kind": "overlay",
            "base_page_visibility": "fully_occluded",
            "blocking_scope": "page",
        },
        {
            "topmost_layer_kind": "page",
            "base_page_visibility": "visible",
            "blocking_scope": "none",
            "mutation_mode": "instrumented_diagnostic",
        },
    ],
)
async def test_persisted_presentation_cannot_be_hidden_by_stable_readiness(presentation):
    async with target_database() as (connection, pool, _other, ids):
        capture_id = await write_capture(connection, pool, ids)
        await connection.execute(
            "UPDATE qc_page_capture SET metadata=metadata || $1::jsonb WHERE id=$2::uuid",
            json.dumps(
                {"presentation": {"protocol_version": "presentation-stack.v1", **presentation}}
            ),
            capture_id,
        )
        await classify(pool, ids, await ready_capture(pool, ids, capture_id))
        assert (await graph_state(connection, capture_id))["counts_as_business_page"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    [
        "parent",
        "action_epoch",
        "diagnostic_before",
        "no_location_change",
        "before_parent",
        "before_spoof",
        "requested_conflict",
        "before_url",
    ],
)
async def test_unknown_after_requires_precise_natural_action_proof(drift):
    async with target_database() as (connection, pool, _other, ids):
        before, after, action = await navigate_pair(connection, pool, ids)
        ready = await ready_capture(pool, ids, after)
        if drift == "parent":
            await connection.execute(
                "UPDATE qc_page_capture SET parent_page_capture_id=NULL WHERE id=$1::uuid", after
            )
        elif drift == "action_epoch":
            with pytest.raises(Exception, match="finalization_epoch"):
                await connection.execute(
                    "UPDATE qc_action_execution SET finalization_epoch=1 "
                    "WHERE action_execution_id=$1",
                    action,
                )
            return
        elif drift == "diagnostic_before":
            await connection.execute(
                "UPDATE qc_page_capture SET metadata=jsonb_set(metadata,"
                "'{pipeline_metrics,diagnostic_capture}','true'::jsonb) WHERE id=$1::uuid",
                before,
            )
        elif drift == "before_parent":
            await connection.execute(
                "UPDATE qc_page_capture SET parent_page_capture_id=NULL WHERE id=$1::uuid", before
            )
        elif drift == "before_spoof":
            await connection.execute(
                "UPDATE qc_page_capture SET metadata=metadata || "
                '\'{"logical_page_id":"web-logical:forged"}\'::jsonb WHERE id=$1::uuid',
                before,
            )
        elif drift == "requested_conflict":
            await connection.execute(
                "UPDATE qc_page_capture SET metadata=metadata || "
                '\'{"logical_page_id":"web-logical:forged"}\'::jsonb '
                "WHERE id=(SELECT requested_source_capture_id FROM qc_action_execution "
                "WHERE action_execution_id=$1)",
                action,
            )
        elif drift == "before_url":
            await connection.execute(
                "UPDATE qc_page_capture SET url='https://example.test/#/unexpected' "
                "WHERE id=$1::uuid",
                before,
            )
        else:
            await connection.execute(
                "UPDATE qc_action_execution SET result_payload='{}'::jsonb "
                "WHERE action_execution_id=$1",
                action,
            )
        await classify(pool, ids, ready, known=False)
        assert (await graph_state(connection, after))["identity_status"] == "unknown"
        assert not await connection.fetchval(
            "SELECT counts_as_business_arrival FROM qc_page_graph_transition "
            "WHERE action_execution_id=$1",
            action,
        )


@pytest.mark.asyncio
async def test_h5_task_with_web_surface_keeps_existing_url_semantics():
    async with target_database() as (connection, pool, _other, ids):
        capture_id = await write_capture(connection, pool, ids, url="https://example.test/?q=a%20b")
        await classify(pool, ids, await ready_capture(pool, ids, capture_id))
        await connection.execute(
            "UPDATE qc_task SET target_platform='h5' WHERE id=$1::uuid", ids["task"]
        )
        await PostgresPageGraphRepository(pool=pool).project_capture_from_database_on_connection(
            connection, capture_id=capture_id
        )
        state = await graph_state(connection, capture_id)
        assert state["surface_kind"] == "web"
        assert state["logical_page_key"] == "url:https://example.test/?q=a+b"
        assert state["identity_status"] == "confirmed"
        graph = await PostgresPageGraphRepository(pool=pool).get_graph(
            task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0
        )
        assert isinstance(graph["states"][0]["state_payload"], str)


@pytest.mark.asyncio
@pytest.mark.parametrize("include_home", [False, True])
async def test_web_graph_service_and_coverage_share_run_without_inflating_required(
    include_home, monkeypatch,
):
    from services.api.app.application.page_graph import PageGraphService
    from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
        PostgresWebCoverageSnapshotRepository,
        WebCoverageSnapshotPersistenceError,
    )
    from services.api.tests import test_web_logical_target_postgres as target_fixture

    requested = ["content_detail", "content_list", "video_detail", "video_list"]
    if include_home:
        requested.append("home")
    body = target_fixture.policy_payload()
    body["discovery_policy"]["required_page_types"] = requested
    monkeypatch.setattr(target_fixture, "policy_payload", lambda: body)

    async with target_database() as (connection, pool, _other, ids):
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0)
        coverage_reader = PostgresWebCoverageSnapshotRepository(pool=pool)
        await connection.execute(
            "UPDATE qc_task SET task_config=task_config || "
            '\'{"execution_mode":"full_inspection",'
            '"web_inspection_notification_mode":"enforced",'
            '"web_exploration_target":{"required_page_types":["search_page"]}}\'::jsonb '
            "WHERE id=$1::uuid",
            ids["task"],
        )
        with pytest.raises(WebCoverageSnapshotPersistenceError, match="冻结"):
            await coverage_reader.load_reconciliation_state(**scope)
        await connection.execute(
            "UPDATE qc_task SET task_config=task_config-'web_exploration_target' WHERE id=$1::uuid",
            ids["task"],
        )
        initial = await coverage_reader.load_reconciliation_state(**scope)
        capture_ids = []
        for index, tab in enumerate(("info", "comments"), start=1):
            capture_id = await write_capture(
                connection,
                pool,
                ids,
                url=f"https://example.test/#/detail/1?tab={tab}",
                sequence=index,
            )
            await classify(pool, ids, await ready_capture(pool, ids, capture_id))
            capture_ids.append(capture_id)
        coverage = await coverage_reader.load_reconciliation_state(**scope)
        graph = await PageGraphService(repository=PostgresPageGraphRepository(pool=pool)).get_graph(
            **scope
        )
        assert graph["agent_run_id"] == coverage.agent_run_id == ids["run"]
        assert graph["finalization_epoch"] == coverage.finalization_epoch == 0
        assert initial.required_page_types == coverage.required_page_types
        # 四类显式目标与含首页目标分别冻结；后续任务配置和图节点不能扩大原分母。
        assert coverage.required_page_types == tuple(sorted(requested))
        assert initial.observed_page_types == coverage.observed_page_types == ()
        states = [row for row in graph["states"] if str(row["state_id"]) in capture_ids]
        assert len(states) == 2
        assert len({row["logical_page_key"] for row in states}) == 1
        assert len({row["state_fingerprint"] for row in states}) == 2
        assert all(row["counts_as_business_page"] for row in states)


@pytest.mark.asyncio
async def test_conflicting_revision_does_not_overwrite_classification_or_graph():
    async with target_database() as (connection, pool, _other, ids):
        capture_id = await write_capture(connection, pool, ids)
        ready = await ready_capture(pool, ids, capture_id)
        await classify(pool, ids, ready)
        original = dict(await graph_state(connection, capture_id))
        with pytest.raises(WebCapturePageTypePersistenceError):
            await classify(pool, ids, ready, known=False)
        assert dict(await graph_state(connection, capture_id)) == original


@pytest.mark.asyncio
async def test_edge_projection_failure_rolls_back_node_common_capture_and_assessment(monkeypatch):
    async with target_database() as (connection, pool, _other, ids):
        _before, after, action = await navigate_pair(connection, pool, ids)
        ready = await ready_capture(pool, ids, after)
        original = dict(await graph_state(connection, after))
        common = await connection.fetchval(
            "SELECT metadata FROM qc_capture WHERE id=$1::uuid", after
        )
        edge = dict(
            await connection.fetchrow(
                "SELECT * FROM qc_page_graph_transition WHERE action_execution_id=$1", action
            )
        )

        async def fail_edge(self, connection, **kwargs):
            await connection.execute("SELECT 1/0")

        monkeypatch.setattr(PostgresPageGraphRepository, "project_action_on_connection", fail_edge)
        with pytest.raises(Exception, match="division by zero"):
            await classify(pool, ids, ready, known=False)
        assert dict(await graph_state(connection, after)) == original
        assert (
            await connection.fetchval("SELECT metadata FROM qc_capture WHERE id=$1::uuid", after)
            == common
        )
        assert (
            dict(
                await connection.fetchrow(
                    "SELECT * FROM qc_page_graph_transition WHERE action_execution_id=$1", action
                )
            )
            == edge
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_page_type_assessment WHERE page_capture_id=$1::uuid",
                after,
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix, expected_identity", [("", "unknown"), ("?tab=comments", "confirmed")]
)
async def test_same_business_url_is_never_new_arrival_despite_location_changed(
    suffix, expected_identity
):
    async with target_database() as (connection, pool, _other, ids):
        before, after, action = await navigate_pair(
            connection, pool, ids, after_url="https://example.test/#/list" + suffix
        )
        await classify(pool, ids, await ready_capture(pool, ids, after), known=False)
        state = await graph_state(connection, after)
        assert state["identity_status"] == expected_identity
        if suffix:
            assert (
                state["logical_page_key"]
                == (await graph_state(connection, before))["logical_page_key"]
            )
        assert not await connection.fetchval(
            "SELECT counts_as_business_arrival FROM qc_page_graph_transition "
            "WHERE action_execution_id=$1",
            action,
        )


@pytest.mark.asyncio
async def test_real_coordinator_fresh_before_unknown_after_is_reachable_without_copying_identity():
    async with target_database() as (connection, pool, _other, ids):
        before, after, _action = await navigate_pair(connection, pool, ids)
        await classify(pool, ids, await ready_capture(pool, ids, after), known=False)
        assert (await graph_state(connection, after))["identity_status"] == "confirmed"
        assert (
            await connection.fetchval(
                "SELECT counts_as_business_arrival FROM qc_page_graph_transition "
                "WHERE source_state_id=$1::uuid AND target_state_id=$2::uuid",
                before,
                after,
            )
            is True
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_page_type_assessment WHERE page_capture_id=$1::uuid",
                before,
            )
            == 0
        )
        for table in ("qc_capture", "qc_page_capture"):
            assert (
                await connection.fetchval(
                    f"SELECT metadata->>'logical_page_id' FROM {table} WHERE id=$1::uuid", before
                )
                is None
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [True, False])
async def test_external_real_after_without_external_metadata_cannot_be_business(known):
    async with target_database() as (connection, pool, _other, ids):
        _before, after, action = await navigate_pair(
            connection, pool, ids, after_url="https://foreign.test/#/detail/1"
        )
        await classify(pool, ids, await ready_capture(pool, ids, after), known=known)
        state = await graph_state(connection, after)
        assert state["is_external"] is True
        assert state["identity_status"] == "unknown"
        assert state["counts_as_business_page"] is False
        assert not await connection.fetchval(
            "SELECT counts_as_business_arrival FROM qc_page_graph_transition "
            "WHERE action_execution_id=$1",
            action,
        )
