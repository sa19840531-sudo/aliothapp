"""绿色 TASK 规则通过正式封存选择器保留全量过程；原件均为受控夹具。"""

import json
from datetime import UTC, datetime

from services.api.app.application.aggregate_inspection_execution import evaluate_aggregate_context
from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
)
from services.api.app.domain.evidence import (
    AppEvidenceGap,
    AppEvidenceIndexEntry,
    AppEvidenceSemanticRole,
    AppExplorationResultOutcome,
    EvidenceInputScope,
    build_app_evidence_index,
    seal_app_evidence_index,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    PostgresAppEvidenceIndexRepository,
)
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只升级显式独立测试库
)
from services.api.tests.test_green_aggregate_execution_postgres import green_case


async def test_formal_prepare_preserves_all_green_task_members_and_missing_material_review(
    tmp_path,
):
    async with green_case(tmp_path / "full-input", "s72", publish=False) as (
        connection,
        pool,
        ids,
        _work,
        repository,
        _input,
        _rule,
        manifests,
        _intent,
        _artifacts,
    ):
        now = datetime.now(UTC)
        captured_at = {
            row["capture_id"]: row["captured_at"]
            for row in await connection.fetch(
                "SELECT id::text AS capture_id,captured_at FROM qc_capture WHERE task_id=$1::uuid",
                ids["task"],
            )
        }
        roles = (
            AppEvidenceSemanticRole.BOOTSTRAP,
            AppEvidenceSemanticRole.ACTION_BEFORE,
            AppEvidenceSemanticRole.ACTION_AFTER,
        )
        index = build_app_evidence_index(
            task_id=ids["task"],
            run_id=ids["run"],
            build_id=f"app-build:{ids['build']}",
            surface_session_id=ids["session"],
            created_at=now,
            result_context_sha256="sha256:" + "c" * 64,
            entries=tuple(
                AppEvidenceIndexEntry(
                    sequence_no=position,
                    capture_id=manifest.capture_id,
                    evidence_manifest_id=manifest.manifest_id,
                    evidence_manifest_version=1,
                    semantic_role=roles[position - 1],
                    captured_at=captured_at[manifest.capture_id],
                    metadata={"page_type": "home", "fixture": "controlled process, not product"},
                )
                for position, manifest in enumerate(manifests, 1)
            ),
        )
        indexes = PostgresAppEvidenceIndexRepository(pool=pool)
        await indexes.create(index=index)
        await indexes.seal(
            index=seal_app_evidence_index(
                index,
                outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
                terminal_reason_code="controlled_business_window_missing",
                sealed_at=now,
                evidence_gaps=(
                    AppEvidenceGap(
                        semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
                        reason_code="controlled_business_window_missing",
                        description="后续完整业务观察窗口未提供；前后 PNG 不能代替业务完成",
                        metadata={"scope": "run"},
                    ),
                ),
            )
        )
        publication = AggregateInspectionPublicationService(
            repository=PostgresAggregateInspectionPublicationRepository(pool=pool),
            max_attempts=1,
        )

        summary = await publication.prepare(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )

        assert summary.expected_rule_count == summary.published_work_item_count == 1
        assert summary.not_applicable_count == summary.evidence_insufficient_count == 0
        work = await connection.fetchrow(
            "SELECT id::text,row_version FROM qc_aggregate_inspection_work_item "
            "WHERE task_id=$1::uuid",
            ids["task"],
        )
        claim = await repository.claim_for_wakeup(
            work_item_id=work["id"],
            task_id=ids["task"],
            message_version=work["row_version"],
            lease_owner="full-green-selection",
            lease_seconds=60,
        )
        context = await repository.load_judge_context(lease=claim.lease)
        assert context.input_set.scope is EvidenceInputScope.TASK
        assert {member.capture_id for member in context.input_set.members} == {
            manifest.capture_id for manifest in manifests
        }
        assert len(context.input_set.members) == 3
        assert context.journey_input is None
        judgement = evaluate_aggregate_context(context)
        assert judgement.execution_status == "evidence_insufficient"
        assert all(item["missing"] for item in judgement.wrapper_payload["green"]["subitems"])
        await repository.complete_judgement(context=context, judgement=judgement)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_review_item WHERE task_id=$1::uuid",
                ids["task"],
            )
            == 1
        )

    # 未交正式 Index 时没有可消费材料，保留缺证复核，不能推导产品不适用。
    async with green_case(tmp_path / "missing-input", "s72", publish=False) as (
        connection,
        pool,
        ids,
        *_rest,
    ):
        summary = await AggregateInspectionPublicationService(
            repository=PostgresAggregateInspectionPublicationRepository(pool=pool),
            max_attempts=1,
        ).prepare(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0)
        assert summary.evidence_insufficient_count == 1
        assert summary.not_applicable_count == summary.published_work_item_count == 0
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_evidence_input_set WHERE task_id=$1::uuid",
                ids["task"],
            )
            == 0
        )
        result = await connection.fetchrow(
            """SELECT execution.execution_status,execution.judge_output,review.status
               FROM qc_rule_execution execution JOIN qc_review_item review
                 ON review.rule_execution_id=execution.id WHERE execution.task_id=$1::uuid""",
            ids["task"],
        )
        assert (
            result["execution_status"] == "evidence_insufficient" and result["status"] == "pending"
        )
        output = json.loads(result["judge_output"])
        assert output["reason_code"] == "AGGREGATE_APPLICABILITY_UNPROVEN"
