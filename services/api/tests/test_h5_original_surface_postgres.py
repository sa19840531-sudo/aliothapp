"""隔离数据库核实同原文档换帧的权威上下文；所有夹具事务回滚。"""

import json
import os
from dataclasses import fields

import pytest

from services.api.app.application.h5_targeted_recapture import H5TargetedRecapturePlanService
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSourceGrade,
    RestorationStrategy,
    build_evidence_request,
)
from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
    PostgresH5TargetedRecaptureRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)
from services.api.tests.test_h5_closed_session_recapture_postgres import _closed_request

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库",
)


@pytest.mark.parametrize("invalid", [None, "document", "state", "missing", "epoch", "closed"])
async def test_origin_continuation_reads_actual_document_and_state_not_capture_number(invalid):
    require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.getenv("ALIOTH_DATABASE_URL"),
    )
    async with _closed_request(task_status="running", check_constraint=False) as fixture:
        connection, pool, ids, prior, _, _, requests, _ = fixture
        session_id = f"h5:{ids['run']}"
        request = build_evidence_request(**{
            **{field.name: getattr(prior, field.name) for field in fields(prior)
               if field.name not in {"request_id", "requirement_hash", "protocol_version"}},
            "session_dependency": EvidenceSessionDependency.ACTIVE_SESSION,
            "replay_policy": EvidenceReplayPolicy.SAME_SURFACE_ONLY,
            "required_equivalence": EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
            "minimum_source_grade": EvidenceSourceGrade.A1,
        })
        await requests.upsert(request=request)
        await connection.execute(
            "UPDATE qc_task SET finalization_state='open',exploration_draining_at=NULL,"
            "session_draining_at=NULL WHERE id=$1", ids["task"],
        )
        await connection.execute(
            "UPDATE qc_surface_session SET status='active',draining_at=NULL,closed_at=NULL,"
            "current_capture_id=$2 WHERE session_id=$1", session_id, ids["restored"],
        )
        await connection.execute(
            "UPDATE qc_capture SET surface_session_id=$2,capture_role='after_action' WHERE id=$1",
            ids["restored"], session_id,
        )
        original = {"logical_surface": {"document_identity": "original-document",
                                        "state_fingerprint": "a" * 64}}
        current = json.loads(json.dumps(original))
        if invalid == "document":
            current["logical_surface"]["document_identity"] = "another-document"
        elif invalid == "state":
            current["logical_surface"]["state_fingerprint"] = "b" * 64
        elif invalid == "missing":
            current = {}
        await connection.execute(
            "UPDATE qc_page_graph_state SET state_payload=$2::jsonb WHERE state_id=$1",
            ids["original"], json.dumps(original),
        )
        await connection.execute(
            """INSERT INTO qc_page_graph_state(state_id,task_id,agent_run_id,surface_kind,
                surface_session_id,finalization_epoch,capture_role,logical_page_key,
                identity_status,identity_source,state_fingerprint,counts_as_business_page,
                state_payload)
                VALUES($1,$2,$3,'web',$4,0,'after_action','detail:42','confirmed',
                    'logical_surface',$5,true,$6::jsonb)""",
            ids["restored"], ids["task"], ids["run"], session_id, "a" * 64, json.dumps(current),
        )
        if invalid == "epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1", ids["task"],
            )
        elif invalid == "closed":
            await connection.execute(
                "UPDATE qc_surface_session SET status='closed',draining_at=now(),closed_at=now()"
                " WHERE session_id=$1", session_id,
            )
        before = await connection.fetchrow(
            "SELECT * FROM qc_evidence_request WHERE request_id=$1", request.request_id,
        )
        reader = PostgresH5TargetedRecaptureRepository(pool=pool)
        context = await reader.read(request=request)
        if invalid not in {"epoch", "closed"}:
            assert context.current_capture_id == ids["restored"] != request.target_capture_id
            assert context.target_document_identity == "original-document"
            assert context.target_state_fingerprint == "a" * 64
        result = await H5TargetedRecapturePlanService(contexts=reader).resolve(
            request=request, agent_run_id=ids["run"], finalization_epoch=0,
        )
        assert result.executable is (invalid is None)
        if invalid is None:
            assert result.plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN
            assert not result.plan.normalization_step_ids
        assert await connection.fetchrow(
            "SELECT * FROM qc_evidence_request WHERE request_id=$1", request.request_id,
        ) == before
