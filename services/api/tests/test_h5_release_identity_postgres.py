"""真实 JSONB、页面图与恢复门禁串联验证 128/204 身份及拒绝反例。"""

import json
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_targeted_recapture import (
    H5StateEquivalenceGate,
    H5TargetedRecaptureContext,
    H5TargetedRecapturePlanService,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    build_evidence_request,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
    PostgresH5StateInvariantReader,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_h5_restoration_identity_convergence import CASES, NOW
from services.api.tests.test_h5_rule_inventory_postgres import seeded

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离数据库",
)


@pytest.mark.parametrize("station", ["jha128", "jha204"])
@pytest.mark.parametrize("fault", [
    None, "business", "document", "obstruction", "media", "payment", "missing_proof",
    "material_rotation", "foreign_run",
])
async def test_persisted_measured_identity_keeps_strict_restoration_boundary(station, fault):
    require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.getenv("ALIOTH_DATABASE_URL"),
    )
    case = CASES[station]
    popup = station == "jha128"
    async with seeded() as (connection, pool, ids, _):
        session = f"h5:{ids['run']}"
        for key in ("original", "restored"):
            restored = key == "restored"
            document = case.get(
                "restored_document_identity" if restored else "source_document_identity",
                case.get("document_identity"),
            )
            state = case.get(
                "restored_state_fingerprint" if restored else "source_state_fingerprint",
                case.get("state_fingerprint"),
            )
            surface = {
                "document_identity": document, "state_fingerprint": state,
                "evidence_quality": "high",
                "state_signals": {"loading": False, "obstruction_count": "0"},
            }
            if restored and popup:
                identity = case["logical_page_key"].removeprefix("logical:")
                surface["transition"] = {
                    "logical_transition_proof": "trusted",
                    "reason_code": "trusted_obstruction_clearance",
                    "before_identity_fingerprint": identity, "after_identity_fingerprint": identity,
                    "before_document_identity": document, "after_document_identity": document,
                }
            logical_key = case["logical_page_key"]
            page_type = case["restored_page_type" if restored else "source_page_type"]
            overlay = popup and not restored
            if restored:
                if fault == "business":
                    logical_key = "logical:other-business"
                elif fault == "document":
                    surface["document_identity"] = "different-document"
                elif fault == "obstruction":
                    overlay = True
                elif fault == "media":
                    page_type = "video_detail"
                elif fault == "payment":
                    page_type = "payment"
                elif fault == "missing_proof":
                    surface = {}
                elif fault == "material_rotation":
                    if popup:
                        surface["transition"]["reason_code"] = "material_rotation"
                    else:
                        surface["state_fingerprint"] = "different-state"
            await connection.execute(
                "UPDATE qc_page_capture SET url='https://site.test/', page_type=$2, "
                "page_state=$3, captured_at=$4 WHERE id=$1::uuid",
                ids[key], page_type, "popup" if overlay else "stable",
                NOW + timedelta(seconds=int(restored)),
            )
            await connection.execute(
                "UPDATE qc_capture SET surface_session_id=$2 WHERE id=$1::uuid", ids[key], session,
            )
            await connection.execute(
                """INSERT INTO qc_page_graph_state
                (state_id,task_id,agent_run_id,surface_kind,surface_session_id,capture_role,
                 logical_page_key,identity_status,state_fingerprint,is_overlay,
                 counts_as_business_page,state_payload)
                VALUES($1::uuid,$2::uuid,$3::uuid,'web',$4,'after_action',$5,'confirmed',
                       $6,$7,NOT $7,$8::jsonb)""",
                ids[key], ids["task"], ids["run"], session, logical_key,
                ("b" if restored else "a") * 64, overlay,
                json.dumps({"identity_status": "confirmed", "logical_surface": surface}),
            )
        if fault == "foreign_run":
            await connection.execute(
                "UPDATE qc_page_graph_state SET agent_run_id=NULL WHERE state_id=$1::uuid",
                ids["restored"],
            )
        request = build_evidence_request(
            work_item_id=ids["work_item"], attempt_id=ids["attempt"], task_id=ids["task"],
            rule_id=ids["rule"], rule_code="alioth_chain_1", target_capture_id=ids["original"],
            reason_code="EVIDENCE_INSUFFICIENT", evidence_goal="补采同一业务正文",
            required_artifact_types=("screenshot", "dom"),
            session_dependency=EvidenceSessionDependency.SNAPSHOT,
            replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
            required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
            side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
            minimum_source_grade=EvidenceSourceGrade.B,
            target_invariants={"business": {
                "page_type": case["source_page_type"], "url_pattern": "https://site.test/",
            }},
            expires_at=NOW + timedelta(minutes=10),
        )
        resolution = await H5TargetedRecapturePlanService(
            contexts=SimpleNamespace(read=AsyncMock(return_value=H5TargetedRecaptureContext(
                task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0,
                session_id=session, current_capture_id=ids["original"],
            ))),
            clock=lambda: NOW,
        ).resolve(request=request, agent_run_id=ids["run"], finalization_epoch=0)
        assert resolution.plan is not None
        reader = PostgresH5StateInvariantReader(pool=pool)
        captures = {
            key: SimpleNamespace(
                capture_id=ids[key], task_id=ids["task"], session_id=session,
                surface_kind=SurfaceKind.WEB,
            ) for key in ("original", "restored")
        }
        original = await reader.read(capture=captures["original"])
        if fault == "foreign_run":
            with pytest.raises(ValueError, match="同 Run"):
                await reader.read(capture=captures["restored"])
            return
        restored = await reader.read(capture=captures["restored"])
        frozen = request.to_dict()
        result = H5StateEquivalenceGate().assess(
            request=request, plan=resolution.plan, restoration_attempt_id="identity-pg-test",
            original=original, restored=restored,
        )
        assert result.passed is (fault is None)
        assert request.to_dict() == frozen
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_llm_usage_event WHERE task_id=$1::uuid", ids["task"],
        ) == 0
