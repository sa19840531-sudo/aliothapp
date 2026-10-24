"""详情选择失败须沿真实业务链保存有界诊断，不扩大执行权限。"""

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.agents.explorer.web_assessment_agent import WebExplorationAssessmentAgent
from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_list_detail_navigation import (
    WebListDetailEvidence,
    WebListDetailNavigator,
    detail_candidates_from_ready_snapshot,
)
from services.api.app.application.web_menu_exploration import WebRuntimeDiscoveredLedgerEntry
from services.api.app.application.web_runtime_discovery import discovery_entry_id
from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.tests.support.fake_chat_client import ScriptedChatClient, make_text_response
from services.api.tests.test_web_exploration_assessment_agent import StaticEvidenceLoader
from services.api.tests.test_web_list_detail_navigation import (
    ORIGIN,
    _detail_command,
    _detail_entry,
    _element,
    _snapshot,
)
from services.api.tests.test_web_menu_report_projection_contract import (
    build_summary,
    discovery_row,
)

_UNTRUSTED_MARKER = "UNTRUSTED_MODEL_OR_EXCEPTION_BODY"


class _NoBrowserOrBudget:
    """任何误放行都会在外部动作边界立即失败。"""

    def __getattr__(self, name):
        raise AssertionError(f"未授权详情动作或额度请求：{name}")


class _FailingLoader:
    def __init__(self, error):
        self.error = error

    async def load(self, **kwargs):
        assert kwargs["screenshot_artifact_id"] == "list:screenshot"
        assert kwargs["dom_artifact_id"] == "list:dom"
        raise self.error


def _case(*, response_kind="valid", loader_error=None, confidence=0.9):
    command = _detail_command()
    snapshot = _snapshot(_element())
    evidence = WebListDetailEvidence(
        task_id=command.task_id,
        agent_run_id=command.agent_run_id,
        session_id=command.session_id,
        ready_capture_id="capture-list",
        screenshot_artifact_id="list:screenshot",
        dom_artifact_id="list:dom",
        origin=ORIGIN,
        current_url=f"{ORIGIN}/list",
        ui_snapshot=snapshot,
        deadline=command.deadline,
    )
    candidates = detail_candidates_from_ready_snapshot(
        snapshot, list_page_type="video_list", origin=ORIGIN
    )
    assert len(candidates) == 1
    payload = {
        "selected_candidate_id": candidates[0].candidate_id,
        "confidence": confidence,
        "reason": "当前可见卡片具有详情结构",
    }
    if response_kind == "outside":
        payload["selected_candidate_id"] = "outside-current-closed-set"
    elif response_kind == "reason":
        payload["reason"] = f"token=fixture-secret-value {_UNTRUSTED_MARKER}"
    elif response_kind == "schema":
        payload["confidence"] = f"not-a-number {_UNTRUSTED_MARKER}"
    raw = _UNTRUSTED_MARKER if response_kind == "json" else json.dumps(payload)
    client = ScriptedChatClient([make_text_response(raw)])
    selector = WebExplorationAssessmentAgent(
        client=client,
        evidence_loader=(
            _FailingLoader(loader_error) if loader_error is not None else StaticEvidenceLoader()
        ),
    )
    never = _NoBrowserOrBudget()
    navigator = WebListDetailNavigator(
        selector=selector,
        coordinator=never,
        readiness=never,
        ready_capture_reader=never,
        classifier=never,
        capture_url_reader=never,
        budget_ledger=never,
    )
    return command, evidence, candidates, client, navigator


async def _terminal_record_from_real_frontier(command, detail):
    """仅替代账本 I/O，保留真实候选恢复、DTO 映射和终态记录生成。"""
    candidate = WebNavigationCandidate(
        entry_id="menu-entry-1",
        label="列表",
        url=f"{ORIGIN}/list",
        menu_path=("列表",),
        dom_fingerprint="fixture-fingerprint",
        source_signal="trusted_navigation",
        page_type_key="video_list",
        source_capture_id="capture-home",
        source_page_url=ORIGIN,
    )
    row = WebRuntimeDiscoveredLedgerEntry(
        entry_id=discovery_entry_id(candidate),
        menu_path=candidate.menu_path,
        canonical_url=candidate.url,
        menu_position=1,
        source_capture_id="capture-home",
        menu_depth=candidate.menu_depth,
        exploration_path=candidate.exploration_path,
        logical_target_identity=candidate.logical_target_identity,
        candidate_manifest=candidate.to_dict(),
    )
    records = []

    class Ledger:
        async def read_summary(self, *, agent_run_id, site_id):
            assert (agent_run_id, site_id) == (command.agent_run_id, "example")
            return SimpleNamespace(runtime_discovered_ledger=(row,))

        async def finalize(self, record):
            records.append(record)

    class Evidence:
        async def capture_outcome(self, *, capture_id):
            assert capture_id == "capture-list"
            return {"action_execution_id": "menu-action-1"}

    frontier = WebRuntimeStageFrontier(
        ledger=Ledger(),
        candidate_source=None,
        navigator=None,
        site=SimpleNamespace(site_id="example", origin=ORIGIN),
        evidence=Evidence(),
    )
    cursor = SimpleNamespace(
        cursor_payload={"menu_ledger_id": row.entry_id, "capture_id": "capture-list"}
    )
    await frontier.finalize_detail(command, cursor, detail=detail, page_type="video_list")
    assert len(records) == 1
    return records[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,error,phase,reason_code,exception_type,attempts",
    [
        ("valid", TypeError(_UNTRUSTED_MARKER), "evidence_load",
         "WEB_DETAIL_EVIDENCE_LOAD_INVALID", "TypeError", 0),
        ("valid", ValueError(_UNTRUSTED_MARKER), "evidence_load",
         "WEB_DETAIL_EVIDENCE_LOAD_INVALID", "ValueError", 0),
        ("json", None, "response_parse",
         "WEB_DETAIL_RESPONSE_SCHEMA_INVALID", "ValidationError", 2),
        ("schema", None, "response_parse",
         "WEB_DETAIL_RESPONSE_SCHEMA_INVALID", "ValidationError", 2),
        ("outside", None, "candidate_membership",
         "WEB_DETAIL_CANDIDATE_OUTSIDE_CLOSED_SET", "ValueError", 2),
        ("reason", None, "reason_sanitize",
         "WEB_DETAIL_SELECTION_REASON_REJECTED", "ValueError", 1),
    ],
)
async def test_detail_failure_diagnostic_survives_agent_navigator_and_terminal_record(
    kind, error, phase, reason_code, exception_type, attempts
):
    command, evidence, candidates, client, navigator = _case(
        response_kind=kind, loader_error=error
    )
    result = await navigator.capture_one(
        evidence, list_page_type="video_list", command=command, entry=_detail_entry()
    )
    assert result.status is WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED
    assert result.error_code == "WEB_DETAIL_SELECTION_FAILED"
    assert result.detail_action_count == 0
    assert result.selected_candidate_id is None
    assert len(client.received_messages) == attempts
    record = await _terminal_record_from_real_frontier(command, result)
    assert record.status is WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED
    assert record.detail_selection == {}
    assert record.diagnostic["error_code"] == "WEB_DETAIL_SELECTION_FAILED"
    diagnostic = record.diagnostic.get("detail_selection_diagnostic")
    assert isinstance(diagnostic, dict), "失败细分诊断在真实终态链路中丢失"
    candidate_ids = sorted(candidate.candidate_id for candidate in candidates)
    expected_digest = "sha256:" + hashlib.sha256(
        json.dumps(candidate_ids, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert diagnostic == {
        "schema_version": "web-detail-selection-diagnostic.v1",
        "phase": phase,
        "reason_code": reason_code,
        "exception_type": exception_type,
        "attempt_count": attempts,
        "source_capture_id": "capture-list",
        "screenshot_artifact_id": "list:screenshot",
        "dom_artifact_id": "list:dom",
        "candidate_count": 1,
        "candidate_set_digest": expected_digest,
        **({"reason_rejection_category": "credential_content"} if kind == "reason" else {}),
    }
    assert _UNTRUSTED_MARKER not in json.dumps(record.diagnostic)
    assert "fixture-secret-value" not in json.dumps(record.diagnostic)


@pytest.mark.asyncio
async def test_low_confidence_remains_untrusted_without_browser_or_diagnostic_failure():
    command, evidence, _, client, navigator = _case(confidence=0.69)
    result = await navigator.capture_one(
        evidence, list_page_type="video_list", command=command, entry=_detail_entry()
    )
    assert result.error_code == "WEB_DETAIL_SELECTION_UNTRUSTED"
    assert result.detail_action_count == 0
    assert result.detail_selection_confidence == 0.69
    assert len(client.received_messages) == 1
    record = await _terminal_record_from_real_frontier(command, result)
    assert record.detail_selection["confidence"] == 0.69
    assert not record.diagnostic.get("detail_selection_diagnostic")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_invalid", [False, True])
async def test_valid_closed_set_selection_keeps_existing_bounded_parser_recovery(first_invalid):
    _, _, candidates, _, _ = _case()
    valid = make_text_response(json.dumps({
        "selected_candidate_id": candidates[0].candidate_id,
        "confidence": 0.91,
        "reason": "当前可见卡片具有详情结构",
    }))
    responses = [make_text_response(_UNTRUSTED_MARKER), valid] if first_invalid else [valid]
    client = ScriptedChatClient(responses)
    selector = WebExplorationAssessmentAgent(
        client=client, evidence_loader=StaticEvidenceLoader()
    )
    assessment = await selector.select_detail_candidate(
        screenshot_artifact_id="list:screenshot",
        dom_artifact_id="list:dom",
        candidates=tuple(candidate.to_public_dict() for candidate in candidates),
        list_page_type="video_list",
    )
    assert assessment.selected_candidate_id == candidates[0].candidate_id
    assert assessment.confidence == 0.91
    assert len(client.received_messages) == (2 if first_invalid else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("WEB_ASSESSMENT_ARTIFACT_CHECKSUM_MISMATCH"),
        RuntimeError("WEB_STAGE_LEASE_LOST"),
        ConnectionError(_UNTRUSTED_MARKER),
        asyncio.CancelledError(),
    ],
)
async def test_existing_non_value_exceptions_propagate_without_reclassification(error):
    command, evidence, _, client, navigator = _case(loader_error=error)
    with pytest.raises(type(error)) as caught:
        await navigator.capture_one(
            evidence, list_page_type="video_list", command=command, entry=_detail_entry()
        )
    assert caught.value is error
    assert client.received_messages == []


@pytest.mark.asyncio
async def test_detail_failure_diagnostic_survives_cold_database_row_projection():
    command, evidence, _, _, navigator = _case(response_kind="outside")
    result = await navigator.capture_one(
        evidence, list_page_type="video_list", command=command, entry=_detail_entry()
    )
    record = await _terminal_record_from_real_frontier(command, result)
    row = discovery_row(
        status="detail_selection_untrusted",
        execution_slot=1,
        menu_action_count=1,
        menu_probe_capture_id="capture-list",
        menu_ready_capture_id="capture-list",
        menu_page_type="video_list",
        detail_selection=dict(record.detail_selection),
        diagnostic=dict(record.diagnostic),
    )
    projected = build_summary(row).to_dict()["runtime_discovered_ledger"][0]
    assert projected["status"] == "detail_selection_untrusted"
    assert projected["error_code"] == "WEB_DETAIL_SELECTION_FAILED"
    assert projected.get("detail_selection_diagnostic") == record.diagnostic[
        "detail_selection_diagnostic"
    ]
    assert projected["detail_action_count"] == 0


@pytest.mark.asyncio
async def test_terminal_boundary_drops_injected_raw_exception_model_and_input_fields():
    command, evidence, _, _, navigator = _case(response_kind="outside")
    result = await navigator.capture_one(
        evidence, list_page_type="video_list", command=command, entry=_detail_entry()
    )
    contaminated = dict(result.detail_selection_diagnostic)
    contaminated.update({
        "raw_model": _UNTRUSTED_MARKER,
        "error": _UNTRUSTED_MARKER,
        "input": {"secret": _UNTRUSTED_MARKER},
        "reason_code": _UNTRUSTED_MARKER,
        "exception_type": _UNTRUSTED_MARKER,
        "source_capture_id": f"https://unsafe.test/?token={_UNTRUSTED_MARKER}",
    })
    record = await _terminal_record_from_real_frontier(
        command, replace(result, detail_selection_diagnostic=contaminated)
    )
    safe = record.diagnostic["detail_selection_diagnostic"]
    assert safe["source_capture_id"] is None
    assert safe["reason_code"] == "WEB_DETAIL_CANDIDATE_OUTSIDE_CLOSED_SET"
    assert safe["exception_type"] == "ValueError"
    assert _UNTRUSTED_MARKER not in json.dumps(record.diagnostic)
