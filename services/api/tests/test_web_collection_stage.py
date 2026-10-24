"""集合子页单入口、两跳上限和真实类型结算。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
from services.api.app.application.web_runtime_stage_collection import WebRuntimeStageCollection
from services.api.app.domain.exploration.web_run_stage_checkpoint import (
    WebRunStage,
    WebStageCursor,
    WebStageGap,
)


def setup(page_type="collection_page", key="menu:one", stage=WebRunStage.HREF_DISCOVERY):
    run = str(uuid4())
    cursor = WebStageCursor.start(
        run_id=run,
        logical_page_key=key,
        stage=stage,
        cursor_payload={
            "session_id": f"web:{run}",
            "capture_id": str(uuid4()),
            "finalization_epoch": 0,
        },
    )
    rows = []

    async def create(row):
        rows.append(row)

    checkpoints = SimpleNamespace(
        list_for_run=AsyncMock(side_effect=lambda **kw: list(rows)),
        create=AsyncMock(side_effect=create),
    )
    evidence = SimpleNamespace(capture_outcome=AsyncMock(return_value={"page_type": page_type}))
    service = SimpleNamespace(
        stage_collection_candidates=AsyncMock(
            return_value=(
                SimpleNamespace(url="https://example.com/group/one", candidate_id="candidate-one"),
                SimpleNamespace(url="https://example.com/group/two", candidate_id="candidate-two"),
            )
        )
    )
    command = SimpleNamespace(
        deadline=datetime.now(UTC) + timedelta(minutes=5), session_id=f"web:{run}"
    )
    handler = WebRuntimeStageCollection(
        runtime=SimpleNamespace(auxiliary_navigation_explorer=service),
        command=command,
        evidence=evidence,
        checkpoints=checkpoints,
        ads=SimpleNamespace(schedule=AsyncMock()),
    )
    return handler, cursor, rows


@pytest.mark.asyncio
async def test_collection_reserves_one_child_without_menu_slot_and_is_idempotent():
    handler, cursor, rows = setup()
    await handler.schedule(cursor)
    await handler.schedule(cursor)
    assert len(rows) == 1
    assert rows[0].logical_page_key == "collection:menu:one"
    assert "menu_ledger_id" not in rows[0].cursor_payload
    assert rows[0].cursor_payload["requested_url"] == "https://example.com/group/one"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unknown", "home", "content_detail", "video_list"])
async def test_non_collection_page_cannot_open_collection_child(kind):
    handler, cursor, rows = setup(page_type=kind)
    await handler.schedule(cursor)
    assert rows == []


@pytest.mark.asyncio
async def test_collection_recursion_is_forbidden():
    handler, cursor, rows = setup(key="collection:menu:one")
    await handler.schedule(cursor)
    await handler.schedule(cursor, detail=True)
    assert rows == []


@pytest.mark.asyncio
async def test_one_detail_child_only_for_list_and_no_third_hop():
    handler, cursor, rows = setup(page_type="video_list", key="collection:menu:one")
    await handler.schedule(cursor, detail=True)
    assert len(rows) == 1
    await handler.schedule(rows[0], detail=True)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_wrong_landing_type_is_not_reported_as_list_success():
    handler, cursor, _ = setup(
        page_type="collection_page",
        key="collection:menu:one",
        stage=WebRunStage.EVIDENCE_RECONCILIATION,
    )
    with pytest.raises(WebStageFailure, match="WEB_COLLECTION_TARGET_TYPE_MISMATCH"):
        await handler.execute(cursor)


@pytest.mark.asyncio
async def test_collection_sensitive_evidence_rejects_only_child_classification():
    handler, cursor, _ = setup(
        key="collection-detail:collection:menu:one", stage=WebRunStage.CLASSIFICATION
    )
    handler.runtime.stage_ready_capture_reader = SimpleNamespace(
        read=AsyncMock(return_value=SimpleNamespace())
    )
    handler.runtime.page_type_classifier = SimpleNamespace(
        classify=AsyncMock(
            side_effect=RuntimeError("WEB_ASSESSMENT_SENSITIVE_CONTENT_PRESENT")
        )
    )

    with pytest.raises(WebStageFailure, match="WEB_COLLECTION_SENSITIVE_EVIDENCE_REJECTED"):
        await handler.execute(cursor)
    handler.ads.schedule.assert_not_awaited()

    handler.runtime.page_type_classifier.classify.side_effect = RuntimeError("DATABASE_DOWN")
    with pytest.raises(RuntimeError, match="DATABASE_DOWN"):
        await handler.execute(cursor)


@pytest.mark.asyncio
async def test_collection_sensitive_evidence_gap_does_not_add_type_mismatch():
    handler, cursor, _ = setup(
        key="collection-detail:collection:menu:one",
        stage=WebRunStage.EVIDENCE_RECONCILIATION,
    )
    rejected = WebStageGap(
        stage=WebRunStage.CLASSIFICATION,
        stage_generation=0,
        code="WEB_COLLECTION_SENSITIVE_EVIDENCE_REJECTED",
        error_code="WEB_COLLECTION_SENSITIVE_EVIDENCE_REJECTED",
        attempt_count=1,
    )

    result = await handler.execute(replace(cursor, gaps=(rejected,)))

    assert result == type(result)()
    handler.evidence.capture_outcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_collection_full_page_uses_current_url_and_refreshes_ready_source():
    handler, cursor, _ = setup(key='collection:menu:one', stage=WebRunStage.FULL_PAGE_CAPTURE)
    cursor = replace(cursor, cursor_payload={**cursor.cursor_payload,
        'current_url':'https://example.com/list'})
    handler.command.task_id = str(uuid4())
    handler.runtime.pre_gate_supplemental_capture = SimpleNamespace(capture=AsyncMock(
        return_value=SimpleNamespace(status='captured', source_capture_id='old',
                                     screenshot_artifact_ids=('scroll',))))
    handler.runtime.page_readiness = SimpleNamespace(refresh_ready_baseline=AsyncMock(
        return_value='new'))
    handler.evidence.capture_reference = AsyncMock(return_value={'capture_id':'new'})
    result = await handler.execute(cursor)
    assert result.artifact_ids == ('scroll',)
    call = handler.runtime.pre_gate_supplemental_capture.capture.call_args.args[0]
    assert call.source_url == 'https://example.com/list'
    assert result.cursor_payload['capture_id'] == 'new'


def test_collection_redirect_does_not_relax_origin_boundary():
    from services.api.app.application.web_collection_navigation import validate_collection_arrival

    with pytest.raises(WebStageFailure, match='WEB_NAVIGATION_CROSS_ORIGIN'):
        validate_collection_arrival(SimpleNamespace(target_url='https://example.com/'),
            requested_url='https://example.com/entry', final_url='https://other.example/list')


@pytest.mark.asyncio
async def test_collection_child_uses_existing_obstruction_handler_before_readiness():
    from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects

    handler, cursor, _ = setup(key='collection:menu:one', stage=WebRunStage.OBSTRUCTION_CLEAR)
    runtime = handler.runtime
    runtime.age_evidence_reader = SimpleNamespace(read=AsyncMock(return_value='evidence'))
    runtime.age_gate_handler = SimpleNamespace(handle=AsyncMock(
        return_value=SimpleNamespace(after_capture_id='cleared')))
    evidence = SimpleNamespace(capture_reference=AsyncMock(return_value={'capture_id':'cleared'}),
                               save_completed=AsyncMock())
    effects = WebRuntimeStageEffects(runtime=runtime, evidence=evidence, command=handler.command)
    result = await effects._execute(cursor)
    runtime.age_gate_handler.handle.assert_awaited_once()
    assert result.cursor_payload['capture_id'] == 'cleared'


@pytest.mark.asyncio
async def test_fresh_collection_navigation_checks_final_origin_before_saving_success():
    handler, cursor, _ = setup(key='collection:menu:one', stage=WebRunStage.BOOTSTRAP_NAVIGATION)
    cursor = replace(cursor, cursor_payload={**cursor.cursor_payload,
        'requested_url':'https://example.com/entry', 'parent_capture_id':str(uuid4()),
        'idempotency_key':'candidate-one'})
    handler.command.target_url = 'https://example.com/'
    handler.service.stage_collection_navigate = AsyncMock(return_value='redirected')
    handler.evidence.capture_reference = AsyncMock(return_value={
        'capture_id':'redirected', 'current_url':'https://other.example/list'})
    with pytest.raises(WebStageFailure, match='WEB_NAVIGATION_CROSS_ORIGIN'):
        await handler.execute(cursor)


@pytest.mark.asyncio
async def test_collection_readiness_retry_rebinds_current_capture_after_observe():
    handler, cursor, _ = setup(
        key="collection-detail:collection:menu:one", stage=WebRunStage.READINESS
    )
    current_id = str(uuid4())
    handler.runtime.page_readiness = SimpleNamespace(prepare=AsyncMock(
        return_value=SimpleNamespace(
            status="timeout", ready_capture_id=None, blocking_reasons=()
        )
    ))
    handler.evidence.current_capture_reference = AsyncMock(return_value={
        "capture_id": current_id, "current_url": "https://example.com/detail"
    })
    with pytest.raises(WebStageFailure, match="WEB_READINESS_TIMEOUT") as error:
        await handler.execute(cursor)
    assert error.value.transient is True
    assert error.value.detail["capture_id"] == current_id
    handler.evidence.current_capture_reference.assert_awaited_once()


def test_collection_menu_shape_is_valid_without_claiming_detail_success():
    from services.api.app.application.web_submenu_expansion import (
        persisted_ordinary_menu_page_has_valid_shape,
    )

    row = {
        "status": "menu_page_captured", "menu_action_count": 1,
        "detail_action_count": 0, "execution_slot": 4,
        "menu_intent_id": "menu-intent", "menu_source_capture_id": "source",
        "menu_action_execution_id": "action", "menu_probe_capture_id": "probe",
        "menu_ready_capture_id": "ready", "menu_page_type": "collection_page",
    }
    assert persisted_ordinary_menu_page_has_valid_shape(row)
    assert not persisted_ordinary_menu_page_has_valid_shape({
        **row, "menu_probe_capture_id": "ready"
    })
    assert not persisted_ordinary_menu_page_has_valid_shape({
        **row, "detail_action_count": 1
    })
