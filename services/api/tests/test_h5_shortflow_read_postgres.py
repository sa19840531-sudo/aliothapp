"""独立 PostgreSQL 台账读侧，保留原绑定、晚到版本与同任务历史 Run。"""

from dataclasses import replace

import pytest

from services.api.app.infrastructure.db.h5_shortflow_read_repository import (
    PostgresH5ShortflowReadRepository,
)
from services.api.tests.test_checkpoint_evidence_postgres import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
    append_quality,
    case,
    save_receipt,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_call_audit_postgres import scope
from services.api.tests.test_h5_shortflow_activation import definition_payload


@pytest.mark.parametrize(
    "mode,fault",
    [
        ("original", None),
        ("redacted", None),
        ("original", "missing"),
        ("original", "corrupt"),
        ("original", "revoked"),
        ("original", "manifest"),
        ("original", "pending_delete"),
        ("original", "lifecycle_revoked"),
    ],
)
async def test_actual_pg_materials_through_public_download_preserve_originals(
    tmp_path, mode, fault
):
    from hashlib import sha256
    from io import BytesIO
    from zipfile import ZipFile

    import httpx

    from services.api.app.api.v2 import h5_shortflows
    from services.api.app.application.task_insight import TaskInsightService
    from services.api.app.infrastructure.db.evidence_download_audit_repository import (
        PostgresEvidenceDownloadAuditRepository,
    )
    from services.api.app.infrastructure.db.object_access_repository import (
        PostgresObjectAccessRepository,
    )
    from services.api.app.infrastructure.db.task_insight_repository import (
        PostgresTaskInsightRepository,
    )
    from services.api.app.infrastructure.db.user_repository import PostgresUserRepository
    from services.api.app.infrastructure.storage.local_evidence_store import (
        LocalEvidenceArtifactStore,
    )
    from services.api.app.main import create_app
    from services.api.tests.test_journey_consumption_postgres import definition
    from services.api.tests.test_journey_cross_session_e2e_postgres import attach_actual_material

    async with case(checkpoint_payload=definition()) as (
        connection,
        repo,
        ids,
        work,
        context,
        decision,
    ):
        await repo.reserve(work=work, context=context, decision=decision)
        store = LocalEvidenceArtifactStore(root=tmp_path)
        turn = await attach_actual_material(
            connection,
            store,
            await save_receipt(connection, ids, work, decision),
            instance="controlled-player",
            document=1000,
            series=True,
        )
        await repo.bind(work=work, context=context, turn=turn)
        original_rows = await connection.fetch(
            "SELECT id::text AS artifact_id,storage_path,checksum FROM qc_evidence_artifact "
            "WHERE task_id=$1::uuid ORDER BY id",
            ids["task"],
        )
        if fault == "missing":
            await store.delete(original_rows[0]["storage_path"])
        elif fault == "corrupt":
            # 仅破坏本用例独占的临时证据，验证存储损坏不会交付部分 ZIP。
            target = store._resolve_path(original_rows[0]["storage_path"])
            assert target.is_relative_to(tmp_path.resolve())
            target.write_bytes(b"controlled-corrupt-artifact")
        elif fault == "pending_delete":
            await connection.execute(
                "UPDATE qc_evidence_artifact SET lifecycle_status='pending_delete' "
                "WHERE id=$1::uuid",
                original_rows[0]["artifact_id"],
            )
        reader = PostgresH5ShortflowReadRepository(pool=repo._pool)
        app = create_app()
        app.dependency_overrides[h5_shortflows.require_task_read_access] = lambda: scope(ids)
        export_scope = replace(
            scope(ids),
            permissions=scope(ids).permissions
            | {
                "evidenceExports:original",
                "evidenceExports:redacted",
            },
        )
        app.dependency_overrides[h5_shortflows.require_access_scope] = lambda: export_scope
        app.dependency_overrides[h5_shortflows._get_ticket_subject_repository] = lambda: (
            PostgresUserRepository(pool=repo._pool)
        )
        app.dependency_overrides[h5_shortflows._get_repository] = lambda: reader
        insight = TaskInsightService(
            repository=PostgresTaskInsightRepository(pool=repo._pool),
            evidence_store_factory=lambda: store,
        )
        if fault in {"revoked", "manifest", "lifecycle_revoked"}:
            read_original = insight.evidence_file
            changed = False

            async def read_then_change(*args, **kwargs):
                nonlocal changed
                payload = await read_original(*args, **kwargs)
                if not changed:
                    changed = True
                    if fault == "revoked":
                        await connection.execute(
                            "UPDATE qc_user SET status='disabled' WHERE id=$1::uuid",
                            ids["user"],
                        )
                    elif fault == "manifest":
                        next_version = 1 + await connection.fetchval(
                            "SELECT max(manifest_version) FROM qc_evidence_manifest "
                            "WHERE capture_id=$1::uuid",
                            turn.coordination.after_capture.capture_id,
                        )
                        await append_quality(
                            connection,
                            turn.coordination.after_capture.capture_id,
                            "fail",
                            next_version,
                        )
                    else:
                        await connection.execute(
                            "UPDATE qc_evidence_artifact SET lifecycle_status='pending_delete' "
                            "WHERE id=$1::uuid",
                            original_rows[0]["artifact_id"],
                        )
                return payload

            insight.evidence_file = read_then_change
        app.dependency_overrides[h5_shortflows._get_task_insight_service] = lambda: insight
        app.dependency_overrides[h5_shortflows._get_object_access_repository] = lambda: (
            PostgresObjectAccessRepository(pool=repo._pool)
        )
        app.dependency_overrides[h5_shortflows._get_download_audit_repository] = lambda: (
            PostgresEvidenceDownloadAuditRepository(pool=repo._pool)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://isolated.test"
        ) as api:
            view = (await api.get(f"/api/v2/tasks/{ids['task']}/h5-shortflows")).json()
            response = await api.get(
                f"/api/v2/tasks/{ids['task']}/h5-shortflows/{context['execution_plan_id']}/archive",
                params={"snapshot_hash": view["snapshot_hash"], "mode": mode},
            )
        if fault:
            status, result, code = {
                "missing": (404, "failed", "ARTIFACT_MISSING"),
                "corrupt": (409, "failed", "ARTIFACT_INTEGRITY_MISMATCH"),
                "revoked": (404, "denied", "SUBJECT_REVOKED"),
                "manifest": (409, "failed", "EXPORT_SNAPSHOT_CHANGED"),
                "pending_delete": (404, "denied", "ARTIFACT_ACCESS_DENIED"),
                "lifecycle_revoked": (404, "denied", "ARTIFACT_ACCESS_REVOKED"),
            }[fault]
            assert response.status_code == status, response.text
            assert "application/zip" not in response.headers["content-type"]
            assert code in response.text
            assert str(tmp_path) not in response.text
            rows = await connection.fetch(
                "SELECT result,denied_reason,failed_artifact_id FROM qc_evidence_download_audit "
                "WHERE export_request_id=$1",
                response.json()["error"]["error_id"],
            )
            assert len(rows) == 7
            assert all(item["result"] == result and item["denied_reason"] == code for item in rows)
            if fault in {"missing", "corrupt"}:
                assert {item["failed_artifact_id"] for item in rows} == {
                    original_rows[0]["artifact_id"],
                }
            return
        assert response.status_code == 200, response.text
        for original in original_rows:
            raw = await store.read(original["storage_path"])
            assert "sha256:" + sha256(raw).hexdigest() == original["checksum"]
        with ZipFile(BytesIO(response.content)) as archive:
            receipt = __import__("json").loads(archive.read("receipt.json"))
            assert len(receipt["materials"]) == 7
            for item in receipt["materials"]:
                assert (
                    "sha256:" + sha256(archive.read(item["path"])).hexdigest() == item["checksum"]
                )
            assert len({item["checksum"] for item in receipt["materials"]}) < 7
            if mode == "original":
                assert (
                    receipt["plan"]["checkpoints"][0]["attempts"][0]["observation_status"]
                    == "needs_review"
                )
            else:
                assert receipt["business_reviewable"] is False
                assert all(
                    item["redaction_strategy"] != "unchanged_original"
                    for item in receipt["materials"]
                )
        audit_rows = await connection.fetch(
            "SELECT * FROM qc_evidence_download_audit WHERE export_request_id=$1",
            response.headers["x-evidence-export-request-id"],
        )
        assert len(audit_rows) == 7
        assert all(item["export_mode"] == mode for item in audit_rows)
        assert all(
            item["archive_checksum"] == "sha256:" + sha256(response.content).hexdigest()
            for item in audit_rows
        )
        if mode == "original":
            assert all(item["original_checksum"] == item["derived_checksum"] for item in audit_rows)
        else:
            assert any(item["original_checksum"] != item["derived_checksum"] for item in audit_rows)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_evidence_download_audit "
                "WHERE actor_user_id=$1 AND result='allowed'",
                ids["user"],
            )
            == 7
        )


async def test_read_partial_plan_and_scope_without_binding_or_budget_writes():
    async with case(checkpoint_payload=definition_payload()) as (
        connection,
        repo,
        ids,
        work,
        context,
        decision,
    ):
        reader = PostgresH5ShortflowReadRepository(pool=repo._pool)
        assert (
            await reader.read(task_id=ids["task"], access_scope=scope(ids, authorized=False))
            is None
        )
        result = await reader.read(task_id=ids["task"], access_scope=scope(ids))
        assert result["plans"][0]["execution_plan_id"] == context["execution_plan_id"]
        assert result["plans"][0]["checkpoints"][0]["attempts"] == []
        assert not await connection.fetchval(
            "SELECT count(*) FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid", ids["task"]
        )
        assert not await connection.fetchval(
            "SELECT count(*) FROM qc_journey_checkpoint_attempt WHERE task_id=$1::uuid", ids["task"]
        )
        catalog = await reader.catalog()
        assert any(item["definition_id"] == "controlled-player" for item in catalog["definitions"])


async def test_read_exact_bound_roles_pending_and_late_quality_not_pass():
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
        reader = PostgresH5ShortflowReadRepository(pool=repo._pool)
        first = await reader.read(task_id=ids["task"], access_scope=scope(ids))
        attempt = first["plans"][0]["checkpoints"][0]["attempts"][0]
        assert attempt["observation_status"] == "needs_review"
        assert len(attempt["bindings"]) == 2
        assert all(item["integrity"] == "current" for item in attempt["bindings"])
        assert all(item["quality_status"] == "pending" for item in attempt["bindings"])
        assert all(item["artifacts"] for item in attempt["bindings"])
        assert "storage_path" not in str(first)
        after = next(item for item in attempt["bindings"] if item["role"] == "after_action")
        await append_quality(connection, after["capture_id"], "fail", 2)
        latest = await reader.read(task_id=ids["task"], access_scope=scope(ids))
        current = next(
            item
            for item in latest["plans"][0]["checkpoints"][0]["attempts"][0]["bindings"]
            if item["role"] == "after_action"
        )
        assert current["manifest_version"] == 1
        assert current["latest_manifest_version"] == 2
        assert current["quality_status"] == "fail"
        assert current["integrity"] == "current"
        versions = current["manifest_versions"]
        assert [item["manifest_version"] for item in versions] == [2, 1]
        assert all(item["review_only"] and item["integrity"] == "verified" for item in versions)
        assert versions[0]["is_latest"] and not versions[0]["is_bound"]
        assert versions[1]["is_bound"] and not versions[1]["is_latest"]
        assert versions[0]["quality_status"] == "fail"
        assert versions[1]["quality_status"] == "pending"
        timeline = latest["plans"][0]["timeline"]
        assert len(timeline) == 4
        assert timeline[0]["event_type"] == timeline[-1]["event_type"] == "capture"
        assert {item["event_type"] for item in timeline[1:3]} == {
            "action_started",
            "action_finished",
        }
        assert timeline[1]["occurred_at"] == timeline[2]["occurred_at"]
        assert timeline[1]["event_id"] < timeline[2]["event_id"]
        assert first["snapshot_hash"] != latest["snapshot_hash"]


async def test_cross_session_capture_is_visible_as_mismatch_not_usable_material():
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
        # 使用实际台账动作身份，不把意图 ID 假装成动作 ID。
        action_id = await connection.fetchval(
            "SELECT action_execution_id FROM qc_journey_checkpoint_evidence "
            "WHERE attempt_id=$1 LIMIT 1",
            context["attempt_id"],
        )
        await connection.execute(
            "UPDATE qc_capture SET surface_session_id='foreign-session' "
            "WHERE action_execution_id=$1",
            action_id,
        )
        result = await PostgresH5ShortflowReadRepository(pool=repo._pool).read(
            task_id=ids["task"], access_scope=scope(ids)
        )
        bindings = result["plans"][0]["checkpoints"][0]["attempts"][0]["bindings"]
        assert all(
            item["integrity"] == "binding_mismatch" and not item["artifacts"] for item in bindings
        )
        assert not result["plans"][0]["timeline"]
        assert all(not item["manifest_versions"] for item in bindings)


async def test_artifact_size_drift_cannot_hide_behind_same_checksum():
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
        await connection.execute(
            "UPDATE qc_evidence_artifact SET size_bytes=size_bytes+1 WHERE task_id=$1::uuid",
            ids["task"],
        )
        result = await PostgresH5ShortflowReadRepository(pool=repo._pool).read(
            task_id=ids["task"], access_scope=scope(ids)
        )
        bindings = result["plans"][0]["checkpoints"][0]["attempts"][0]["bindings"]
        assert all(
            item["integrity"] == "binding_mismatch" and not item["artifacts"] for item in bindings
        )
