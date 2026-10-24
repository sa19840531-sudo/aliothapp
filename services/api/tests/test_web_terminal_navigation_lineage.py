"""菜单重采换帧后仍使用原 GOTO 血缘结算，不引用 OBSERVE 动作。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.web_list_detail_navigation import WebListDetailResult
from services.api.app.application.web_run_stage_orchestrator import WebStageEffectResult
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.tests.test_web_runtime_stage_effects import Evidence, command, cursor
from services.api.tests.test_web_runtime_stage_frontier import Ledger


async def test_restored_bootstrap_marker_retains_original_navigation_capture():
    state = cursor(WebRunStage.BOOTSTRAP_NAVIGATION)
    state = replace(state, logical_page_key="menu", cursor_payload={
        **state.cursor_payload, "menu_ledger_id": "menu",
    })
    evidence = Evidence()
    await evidence.save_completed(state, WebStageEffectResult(
        cursor_payload={"capture_id": "menu-goto"},
    ))
    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(), evidence=evidence, command=command()
    )
    restored = await effects.find_completed(state)
    assert restored.cursor_payload["menu_navigation_capture_id"] == "menu-goto"


async def test_navigation_anchor_survives_readiness_retry_and_is_persisted_in_stage_result():
    class Frontier:
        async def get_candidate(self, *args, **kwargs):
            return object()

        async def navigate(self, *args, **kwargs):
            return SimpleNamespace(final_capture_id="menu-goto")

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(), evidence=Evidence(), command=command(), frontier=Frontier()
    )
    state = cursor(WebRunStage.BOOTSTRAP_NAVIGATION)
    state = replace(state, logical_page_key="menu", cursor_payload={
        **state.cursor_payload, "menu_ledger_id": "menu",
    })
    outcome = await effects.execute(state)
    assert outcome.cursor_payload["menu_navigation_capture_id"] == "menu-goto"
    state = replace(state, current_stage=WebRunStage.READINESS, cursor_payload={
        **state.cursor_payload, **outcome.cursor_payload,
    })
    retried = state.record_gap_and_advance(
        error_code="WEB_READINESS_TIMEOUT",
        detail={
            "capture_id": "00000000-0000-0000-0000-000000000019",
            "current_url": "https://example.test/",
        },
    )
    assert retried.cursor_payload["menu_navigation_capture_id"] == "menu-goto"


@pytest.mark.parametrize("terminal", [
    WebMenuTerminalStatus.NAVIGATION_FAILED, WebMenuTerminalStatus.DETAIL_CAPTURED,
])
async def test_detail_closeout_keeps_original_menu_action_after_observe_retry(terminal):
    records = []

    class Budget(Ledger):
        async def finalize(self, record):
            # 与真实 SQL 的 intent 归属约束一致，观察动作不得冒充菜单动作。
            assert record.menu_action_execution_id == "menu-action"
            assert record.menu_probe_capture_id == "menu-goto"
            records.append(record)

    class Facts:
        async def capture_outcome(self, *, capture_id):
            return {"action_execution_id": (
                "menu-action" if capture_id == "menu-goto" else "observe-action"
            )}

    budget = Budget()
    state = cursor(WebRunStage.CANDIDATE_NAVIGATION)
    state = replace(state, logical_page_key="menu", cursor_payload={
        **state.cursor_payload,
        "menu_ledger_id": "discovery:" + budget.candidates[0].logical_target_identity,
        "menu_navigation_capture_id": "menu-goto",
        "parent_capture_id": "observe-retry",
    })
    detail = WebListDetailResult(
        status=terminal,
        detail_capture_id=(
            "detail-ready" if terminal is WebMenuTerminalStatus.DETAIL_CAPTURED else None
        ),
        page_type="content_detail" if terminal is WebMenuTerminalStatus.DETAIL_CAPTURED else None,
        error_code="WEB_DETAIL_READINESS_FAILED", probe_capture_id="detail-goto",
        detail_action_count=1, detail_action_execution_id="detail-action",
        detail_intent_id="detail-intent", diagnostic_type="readiness_loading",
    )
    await WebRuntimeStageFrontier(
        ledger=budget, candidate_source=None, navigator=None,
        site=SimpleNamespace(site_id="runtime", origin="https://example.test/"), evidence=Facts(),
    ).finalize_detail(command(), state, detail=detail, page_type="content_list")
    assert len(records) == 1
    assert records[0].detail_action_execution_id == "detail-action"
    assert records[0].status == terminal
    assert records[0].diagnostic["type"] == "readiness_loading"


async def test_reused_detail_closeout_uses_validated_primary_frame_and_audits_ready():
    records = []

    class Budget(Ledger):
        async def finalize(self, record):
            records.append(record)

    class Facts:
        async def capture_outcome(self, *, capture_id):
            assert capture_id == "menu-goto"
            return {"action_execution_id": "menu-action"}

        async def current_menu_capture_after_primary(self, **kwargs):
            self.validated = kwargs
            return "menu-primary-after"

    budget = Budget()
    facts = Facts()
    menu_id = "discovery:" + budget.candidates[0].logical_target_identity
    state = cursor(WebRunStage.CANDIDATE_NAVIGATION)
    state = replace(state, logical_page_key="menu", cursor_payload={
        **state.cursor_payload,
        "menu_ledger_id": menu_id,
        "menu_navigation_capture_id": "menu-goto",
        "capture_id": "menu-classified-ready",
    })
    detail = WebListDetailResult(
        status=WebMenuTerminalStatus.LOGICAL_TARGET_REUSED,
        detail_capture_id=None, page_type=None,
        reused_capture_id="earlier-detail",
        reused_logical_page_id="web-logical:earlier-detail",
    )
    await WebRuntimeStageFrontier(
        ledger=budget, candidate_source=None, navigator=None,
        site=SimpleNamespace(site_id="runtime", origin="https://example.test/"),
        evidence=facts,
    ).finalize_detail(command(), state, detail=detail, page_type="video_list")
    assert facts.validated == {
        "site_id": "runtime",
        "menu_entry_id": menu_id,
        "menu_action_execution_id": "menu-action",
        "menu_probe_capture_id": "menu-goto",
        "menu_ready_capture_id": "menu-classified-ready",
    }
    assert records[0].menu_ready_capture_id == "menu-primary-after"
    assert records[0].diagnostic["menu_classification_capture_id"] == "menu-classified-ready"
    assert records[0].diagnostic["reused_capture_id"] == "earlier-detail"
