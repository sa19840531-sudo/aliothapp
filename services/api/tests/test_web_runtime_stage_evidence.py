"""阶段完成证据仓储的归属验证与完成引用投影。"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from services.api.app.application.web_run_stage_orchestrator import WebStageEffectResult
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    WebStageCheckpointConflict,
)
from services.api.app.infrastructure.db.web_runtime_stage_evidence_repository import (
    PostgresWebRuntimeStageEvidenceRepository,
)

RUN = "00000000-0000-0000-0000-000000000006"
TASK = "00000000-0000-0000-0000-000000000001"
CAPTURE = "00000000-0000-0000-0000-000000000007"


class Connection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.queries = []

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        return self.rows.pop(0)

    @asynccontextmanager
    async def transaction(self):
        yield


async def test_navigation_recovery_reads_completed_action_and_current_capture():
    connection = Connection([{"id": CAPTURE, "url": "https://example.test/"}])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK,
        run_id=RUN,
        session_id=f"web:{RUN}",
        pool=Pool(connection),
    )
    from dataclasses import replace

    current = replace(cursor(), current_stage=WebRunStage.BOOTSTRAP_NAVIGATION)
    result = await repository.find_navigation(current)
    assert result.cursor_payload["capture_id"] == CAPTURE
    sql, args = connection.queries[0]
    assert "action.status='completed'" in sql
    assert "session.current_capture_id=capture.id" in sql
    assert f"web-bootstrap:{RUN}" in args


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.parametrize('key', ['collection:menu', 'collection-detail:collection:menu'])
async def test_collection_recovery_matches_its_action_not_home_bootstrap(key):
    from dataclasses import replace

    connection = Connection([{'id':CAPTURE, 'url':'https://example.test/list'}])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK, run_id=RUN, session_id=f'web:{RUN}', pool=Pool(connection))
    current = cursor()
    current = replace(current, logical_page_key=key,
        current_stage=WebRunStage.BOOTSTRAP_NAVIGATION,
        cursor_payload={**current.cursor_payload, 'idempotency_key':'collection-candidate'})
    await repository.find_navigation(current)
    args = connection.queries[0][1]
    assert f'web-collection:{RUN}:collection-candidate' in args
    assert f'web-bootstrap:{RUN}' not in args


def cursor():
    return WebStageCursor.start(
        run_id=RUN,
        logical_page_key="home",
        stage=WebRunStage.CLASSIFICATION,
        cursor_payload={"session_id": f"web:{RUN}", "finalization_epoch": 2, "capture_id": CAPTURE},
    ).claim(worker_id="worker", now=datetime.now(UTC))


async def test_repository_refuses_stage_with_no_live_run_and_claim_scope():
    connection = Connection([None])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK,
        run_id=RUN,
        session_id=f"web:{RUN}",
        pool=Pool(connection),
    )
    with pytest.raises(WebStageCheckpointConflict):
        await repository.assert_active(cursor())


async def test_repository_projects_completed_phase_only_to_validated_references():
    connection = Connection(
        [
            {
                "source_capture_id": CAPTURE,
                "context_payload": {"capture_id": CAPTURE, "current_url": "https://example.test/"},
                "evidence_artifact_ids": [],
            }
        ]
    )
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK,
        run_id=RUN,
        session_id=f"web:{RUN}",
        pool=Pool(connection),
    )
    result = await repository.find_completed(cursor())
    assert result.cursor_payload == {"capture_id": CAPTURE, "current_url": "https://example.test/"}
    query, args = connection.queries[0]
    assert cursor().idempotency_key in args
    assert "phase.finalization_epoch" in query


async def test_optional_primary_failure_roundtrips_without_checkpoint_payload():
    import json

    connection = Connection(
        [{"run_id": RUN}, {"finalization_epoch": 2}, {"run_id": RUN}, {"phase_run_id": "effect"}]
    )
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK, run_id=RUN, session_id=f"web:{RUN}", pool=Pool(connection)
    )
    result = WebStageEffectResult(
        optional_primary_error_code="WEB_PRIMARY_CONTENT_CAPTURE_FAILED"
    )
    await repository.save_completed(cursor(), result)
    payload = json.loads(connection.queries[-1][1][3])
    assert payload["optional_primary_error_code"] == "WEB_PRIMARY_CONTENT_CAPTURE_FAILED"
    connection.rows.append({
        "source_capture_id": CAPTURE, "context_payload": payload, "evidence_artifact_ids": []
    })
    restored = await repository.find_completed(cursor())
    assert restored.optional_primary_error_code == "WEB_PRIMARY_CONTENT_CAPTURE_FAILED"
    assert "optional_primary_error_code" not in restored.cursor_payload


async def test_repository_does_not_accept_dom_as_stage_recovery_payload():
    connection = Connection([{
        "source_capture_id": CAPTURE,
        "context_payload": {"capture_id": CAPTURE, "dom": "unsafe"},
        "evidence_artifact_ids": [],
    }])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK,
        run_id=RUN,
        session_id=f"web:{RUN}",
        pool=Pool(connection),
    )
    with pytest.raises(ValueError):
        await repository.find_completed(cursor())


async def test_detail_marker_roundtrips_authority_without_extending_checkpoint_payload():
    import json

    authority = "00000000-0000-0000-0000-000000000008"
    connection = Connection(
        [
            {"run_id": RUN},
            {"page_type": "content_detail"},
            {"finalization_epoch": 2},
            {"run_id": RUN},
            {"phase_run_id": "effect"},
        ]
    )
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK, run_id=RUN, session_id=f"web:{RUN}", pool=Pool(connection)
    )
    result = WebStageEffectResult(
        cursor_payload={"capture_id": CAPTURE},
        authoritative_capture_id=authority,
        authoritative_page_type="content_detail",
    )
    await repository.save_completed(cursor(), result, operation="detail")
    payload = json.loads(connection.queries[-1][1][3])
    assert payload["capture_id"] == CAPTURE
    assert payload["authoritative_capture_id"] == authority
    assert payload["authoritative_page_type"] == "content_detail"
    connection.rows.append({"context_payload": payload, "evidence_artifact_ids": []})
    connection.rows.append({"page_type": "content_detail"})
    restored = await repository.find_completed(cursor(), operation="detail")
    assert restored.authoritative_capture_id == authority
    assert restored.authoritative_page_type == "content_detail"
    assert "authoritative_page_type" not in restored.cursor_payload
    sql, args = connection.queries[-1]
    assert "page.page_type" in sql
    assert "capture.agent_run_id=$3::uuid" in sql
    assert "capture.surface_session_id=$4" in sql
    assert "capture.metadata->>'finalization_epoch'=task.finalization_epoch::text" in sql
    assert authority in args


async def test_detail_marker_rejects_authority_not_matching_persisted_capture_type():
    connection = Connection(
        [
            {
                "context_payload": {
                    "capture_id": CAPTURE,
                    "authoritative_capture_id": CAPTURE,
                    "authoritative_page_type": "content_detail",
                },
                "evidence_artifact_ids": [],
            },
            {"page_type": None},
        ]
    )
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK, run_id=RUN, session_id=f"web:{RUN}", pool=Pool(connection)
    )
    with pytest.raises(WebStageCheckpointConflict):
        await repository.find_completed(cursor(), operation="detail")


@pytest.mark.parametrize(
    "capture_id,page_type",
    [
        (CAPTURE, "https://unsafe.test/"),
        ("x" * 1000, "content_detail"),
        (None, "content_detail"),
        (CAPTURE, None),
        (CAPTURE, []),
    ],
)
def test_detail_authority_rejects_unbounded_or_incomplete_facts(capture_id, page_type):
    with pytest.raises(ValueError):
        WebStageEffectResult(authoritative_capture_id=capture_id, authoritative_page_type=page_type)


async def test_completion_evidence_refuses_stale_claim_without_inserting_phase():
    connection = Connection([None])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK,
        run_id=RUN,
        session_id=f"web:{RUN}",
        pool=Pool(connection),
    )
    with pytest.raises(WebStageCheckpointConflict):
        await repository.save_completed(cursor(), WebStageEffectResult())


async def test_completion_write_locks_run_then_checkpoint_before_phase_insert():
    connection = Connection(
        [{"run_id": RUN}, {"finalization_epoch": 2}, {"run_id": RUN}, {"phase_run_id": "effect"}]
    )
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK,
        run_id=RUN,
        session_id=f"web:{RUN}",
        pool=Pool(connection),
    )
    await repository.save_completed(cursor(), WebStageEffectResult())
    assert "FOR NO KEY UPDATE OF run,task" in connection.queries[1][0]
    assert "FOR UPDATE" in connection.queries[2][0]
    assert "INSERT INTO qc_surface_phase_run" in connection.queries[3][0]


async def test_capture_reference_requires_current_session_and_epoch():
    connection = Connection([None])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK,
        run_id=RUN,
        session_id=f"web:{RUN}",
        pool=Pool(connection),
    )
    with pytest.raises(WebStageCheckpointConflict):
        await repository.capture_reference(capture_id=CAPTURE)


async def test_closed_session_is_read_from_matching_task_run_scope():
    connection = Connection([{"status": "closed"}])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK,
        run_id=RUN,
        session_id=f"web:{RUN}",
        pool=Pool(connection),
    )
    assert await repository.session_closed(session_id=f"web:{RUN}")
    with pytest.raises(WebStageCheckpointConflict):
        await repository.session_closed(session_id="web:other")


async def test_initial_epoch_is_read_from_active_task_instead_of_assumed_zero():
    connection = Connection([{"finalization_epoch": 7}])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK,
        run_id=RUN,
        session_id=f"web:{RUN}",
        pool=Pool(connection),
    )
    assert await repository.read_epoch() == 7


async def test_capture_outcome_reads_classification_and_action_without_current_requirement():
    connection = Connection([{"page_type": "home", "action_execution_id": "action:1"}])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK,
        run_id=RUN,
        session_id=f"web:{RUN}",
        pool=Pool(connection),
    )
    assert await repository.capture_outcome(capture_id=CAPTURE) == {
        "page_type": "home",
        "action_execution_id": "action:1",
    }
    sql, _ = connection.queries[0]
    assert "COALESCE(page.page_type,parent_page.page_type) AS page_type" in sql
    assert "primary_content_capture,parent_capture_id" in sql


@pytest.mark.parametrize("recorded", [True, False])
async def test_durable_page_type_assessment_checks_same_capture_lineage_without_page_type(
    recorded,
):
    connection = Connection([{"page_type": "unknown"} if recorded else None])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK, run_id=RUN, session_id=f"web:{RUN}", pool=Pool(connection)
    )

    assert await repository.has_durable_page_type_assessment(
        capture_id=CAPTURE, finalization_epoch=2
    ) is recorded

    sql, args = connection.queries[0]
    assert "qc_h5_page_type_assessment" in sql
    assert "qc_page_type_judgement" not in sql
    assert "page.page_type='home'" not in sql
    assert "session.current_capture_id" not in sql
    assert CAPTURE in args
    assert 2 in args


async def test_durable_assessment_does_not_require_optional_judgement_row():
    class NoJudgementConnection(Connection):
        async def fetchrow(self, query, *args):
            self.queries.append((query, args))
            return {"page_type": "home"} if "qc_page_type_judgement" not in query else None

    connection = NoJudgementConnection([])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK, run_id=RUN, session_id=f"web:{RUN}", pool=Pool(connection)
    )

    assert await repository.has_durable_page_type_assessment(
        capture_id=CAPTURE, finalization_epoch=2
    )


@pytest.mark.parametrize("page_type", ["home", "unknown", None])
async def test_durable_assessment_recovers_final_page_type_without_current_capture(page_type):
    connection = Connection([{"page_type": page_type}])
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK, run_id=RUN, session_id=f"web:{RUN}", pool=Pool(connection)
    )

    assert await repository.read_durable_page_type_assessment(
        capture_id=CAPTURE, finalization_epoch=2
    ) == {"page_type": page_type}
    sql, _args = connection.queries[0]
    assert "assessment.assessment_payload->>'final_page_type'" in sql
    assert "session.current_capture_id" not in sql


@pytest.mark.parametrize("mismatch", ["source", "context"])
async def test_classification_completed_phase_rejects_source_and_context_capture_mismatch(
    mismatch,
):
    other_capture = "00000000-0000-0000-0000-000000000009"
    connection = Connection(
        [{
            "source_capture_id": other_capture if mismatch == "source" else CAPTURE,
            "context_payload": {
                "capture_id": other_capture if mismatch == "context" else CAPTURE
            },
            "evidence_artifact_ids": [],
        }]
    )
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK, run_id=RUN, session_id=f"web:{RUN}", pool=Pool(connection)
    )

    with pytest.raises(Exception, match="WEB_CLASSIFICATION_CAPTURE_MISMATCH"):
        await repository.find_completed(cursor(), operation="classification")


async def test_report_capture_projection_includes_durable_auxiliary_and_primary_frames():
    connection = Connection([])

    async def fetch(sql, *args):
        assert "web-run-stage-evidence.v1" in sql and "phase.status='completed'" in sql
        return [{"capture_id": CAPTURE, "page_type": "search_page"}]

    connection.fetch = fetch
    repository = PostgresWebRuntimeStageEvidenceRepository(
        task_id=TASK, run_id=RUN, session_id=f"web:{RUN}", pool=Pool(connection)
    )
    assert await repository.report_captures() == ((CAPTURE, "search_page"),)
