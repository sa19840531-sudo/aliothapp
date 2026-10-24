"""App 操作日志进证据判定的单元测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.app_action_evidence import (
    PostgresAppActionEvidenceStore,
    _enrich_app_action_pair,
    build_app_action_log_entry,
)
from services.api.app.application.inspection_handoff import (
    build_inspection_handoff,
    resolve_inspection_evidence_capture_roles,
)
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemLease,
    InspectionWorkItemRule,
)
from services.api.app.application.observation_packet import _compact_action_log
from services.api.app.domain.inspection import EvidenceBundleReference
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.rule_judge_evidence import (
    PostgresRuleJudgeEvidenceReader,
)
from services.api.tests.test_rule_judge_evidence import FakeArtifactStore, FakePool, _artifact_row


def _intent() -> ActionIntent:
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    return ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="task-1:branch-1:intent-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-source",
        action_type=InteractionOperation.CLICK,
        semantic_target="ad_outbound",
        purpose="点击中部外链广告取证",
        parameters={"candidate_id": "candidate-1"},
        capture_profile=CaptureProfile(
            profile_id="app-test",
            before=CaptureDirective(
                role="before_action",
                artifact_types=("screenshot",),
            ),
            after=CaptureDirective(
                role="after_action",
                artifact_types=("screenshot",),
            ),
        ),
        deadline=deadline,
        risk_level=InteractionRiskLevel.LOW,
    )


def _interaction(
    *, status: InteractionResultStatus = InteractionResultStatus.SUCCEEDED
) -> InteractionResult:
    return InteractionResult(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        action_execution_id="action-1",
        source_capture_id="capture-before",
        status=status,
        result_capture_id=(
            "capture-after" if status is InteractionResultStatus.SUCCEEDED else None
        ),
        error_code=None if status is InteractionResultStatus.SUCCEEDED else "tap_missed",
        error_message=None if status is InteractionResultStatus.SUCCEEDED else "未命中",
    )


def test_build_app_action_log_entry_keeps_judge_usable_fields() -> None:
    entry = build_app_action_log_entry(
        intent=_intent(),
        interaction=_interaction(),
        before_capture_id="capture-before",
        after_capture_id="capture-after",
    )
    compact = _compact_action_log((entry,))
    assert compact[0]["action"] == "click"
    assert compact[0]["result"] == "success"
    assert compact[0]["purpose"] == "点击中部外链广告取证"
    assert compact[0]["semantic_target"] == "ad_outbound"
    assert compact[0]["source_capture_id"] == "capture-before"
    assert compact[0]["result_capture_id"] == "capture-after"
    assert compact[0]["execution_outcome"] == "succeeded"
    assert compact[0]["verification_outcome"] == "unverified"
    assert compact[0]["target_before"] == {
        "candidate_id": "candidate-1",
        "source_capture_id": "capture-before",
        "semantic_target": "ad_outbound",
    }
    assert entry["execution_outcome"] == "succeeded"
    assert entry["verification_outcome"] == "unverified"


def test_app_action_pair_marks_unchanged_capture_as_no_effect() -> None:
    entry = build_app_action_log_entry(
        intent=_intent(),
        interaction=_interaction(),
        before_capture_id="capture-before",
        after_capture_id="capture-after",
    )
    enriched = _enrich_app_action_pair(
        entry,
        {
            "before_image_signature": "sha256:" + "a" * 64,
            "after_image_signature": "sha256:" + "a" * 64,
            "before_package_name": "com.example.app",
            "after_package_name": "com.example.app",
            "before_activity": ".MainActivity",
            "after_activity": ".MainActivity",
            "before_page_type": "home",
            "after_page_type": "home",
        },
    )

    assert enriched["result"] == "no_effect"
    assert enriched["verification_outcome"] == "no_effect"
    assert enriched["state_diff"]["page_changed"] is False


def test_app_ad_click_builds_complete_external_browser_evidence_pair() -> None:
    intent = replace(
        _intent(),
        parameters={
            "candidate_id": "candidate-ad-1",
            "capture_ad_landing_evidence": True,
            "ad_landing_capability": "ad_outbound",
            "navigation_depth": 0,
            "app_ad_landing_contract": "app-ad-landing-consistency.v1",
        },
    )
    entry = build_app_action_log_entry(
        intent=intent,
        interaction=_interaction(),
        before_capture_id="capture-before",
        after_capture_id="capture-after",
    )
    enriched = _enrich_app_action_pair(
        entry,
        {
            "before_image_signature": "sha256:" + "a" * 64,
            "after_image_signature": "sha256:" + "b" * 64,
            "before_package_name": "com.example.app",
            "after_package_name": "com.android.chrome",
            "before_activity": ".MainActivity",
            "after_activity": "com.google.android.apps.chrome.Main",
            "before_page_type": "startup_ad",
            "after_page_type": "browser_page",
            "before_screenshot_artifact_id": "artifact-source",
            "after_screenshot_artifact_id": "artifact-landing",
        },
    )

    evidence = enriched["ad_navigation_evidence"]
    assert evidence["status"] == "observed"
    assert evidence["navigation_mode"] == "external_browser"
    assert evidence["pair_complete"] is True
    assert evidence["source"]["artifact_id"] == "artifact-source"
    assert evidence["landing"]["artifact_id"] == "artifact-landing"
    assert evidence["landing"]["package_name"] == "com.android.chrome"
    assert evidence["landing"]["evidence_role"] == "ad_outbound_landing"
    assert evidence["app_evidence_role"]["complete"] is False  # 缺 return
    assert evidence["app_evidence_role"]["landing"]["capture_id"] == "capture-after"


def test_app_ad_click_without_browser_is_evidence_insufficient() -> None:
    intent = replace(
        _intent(),
        parameters={
            "candidate_id": "candidate-ad-1",
            "capture_ad_landing_evidence": True,
            "ad_landing_capability": "ad_outbound",
            "navigation_depth": 0,
            "app_ad_landing_contract": "app-ad-landing-consistency.v1",
        },
    )
    entry = build_app_action_log_entry(
        intent=intent,
        interaction=_interaction(),
        before_capture_id="capture-before",
        after_capture_id="capture-after",
    )
    enriched = _enrich_app_action_pair(
        entry,
        {
            "before_image_signature": "sha256:" + "a" * 64,
            "after_image_signature": "sha256:" + "b" * 64,
            "before_package_name": "com.example.app",
            "after_package_name": "com.other.app",
            "before_activity": ".MainActivity",
            "after_activity": ".OtherActivity",
            "before_page_type": "startup_ad",
            "after_page_type": "unknown",
            "before_screenshot_artifact_id": "artifact-source",
            "after_screenshot_artifact_id": "artifact-after",
        },
    )

    evidence = enriched["ad_navigation_evidence"]
    # 非浏览器外跳：pair_complete 仍 false，但落地截图标 ad_outbound_landing（partial）
    assert evidence["pair_complete"] is False
    assert evidence["status"] == "partial"
    assert evidence["landing"]["evidence_role"] == "ad_outbound_landing"
    assert evidence["app_evidence_role"]["landing"]["capture_id"] == "capture-after"


def test_app_ad_click_attaches_landing_url_from_after_metadata() -> None:
    intent = replace(
        _intent(),
        parameters={
            "candidate_id": "candidate-ad-1",
            "capture_ad_landing_evidence": True,
            "ad_landing_capability": "ad_outbound",
            "navigation_depth": 0,
            "app_ad_landing_contract": "app-ad-landing-consistency.v1",
        },
    )
    entry = build_app_action_log_entry(
        intent=intent,
        interaction=_interaction(),
        before_capture_id="capture-before",
        after_capture_id="capture-after",
    )
    enriched = _enrich_app_action_pair(
        entry,
        {
            "before_image_signature": "sha256:" + "a" * 64,
            "after_image_signature": "sha256:" + "b" * 64,
            "before_package_name": "com.example.app",
            "after_package_name": "com.android.chrome",
            "before_activity": ".MainActivity",
            "after_activity": "com.google.android.apps.chrome.Main",
            "before_page_type": "startup_ad",
            "after_page_type": "browser_page",
            "before_screenshot_artifact_id": "artifact-source",
            "after_screenshot_artifact_id": "artifact-landing",
            "after_landing_url": "https://promo.example.test/land",
            "after_landing_url_candidates": [
                "https://promo.example.test/land",
                "https://cdn.example.test/x",
            ],
        },
    )
    evidence = enriched["ad_navigation_evidence"]
    assert evidence["landing"]["url"] == "https://promo.example.test/land"
    assert evidence["app_evidence_role"]["landing"]["url"] == (
        "https://promo.example.test/land"
    )


def test_observation_packet_keeps_network_fault_result_and_cleanup() -> None:
    compact = _compact_action_log(
        (
            {
                "action": "network_fault",
                "result": "network_fault_injected",
                "fault_url_pattern_length": 18,
                "fault_url_pattern_sha256": "a" * 64,
                "matched_request_count": 2,
                "matched_request_urls": ["https://ads.example.test/video"],
                "matched_requests_truncated": True,
                "route_restored": True,
                "reload_error": "net::ERR_FAILED",
            },
        )
    )[0]

    assert compact["result"] == "network_fault_injected"
    assert compact["matched_request_count"] == 2
    assert compact["route_restored"] is True
    assert compact["matched_request_urls"] == ["https://ads.example.test/video"]


def test_evidence_bundle_accepts_before_primary_roles() -> None:
    evidence = EvidenceBundleReference(
        page_capture_id="capture-after",
        producer_agent_run_id="producer-run-1",
        evidence_capture_roles={
            "before": "capture-before",
            "primary": "capture-after",
        },
    )
    assert evidence.to_dict()["evidence_capture_roles"] == {
        "before": "capture-before",
        "primary": "capture-after",
    }


def test_build_inspection_handoff_carries_roles() -> None:
    lease = InspectionWorkItemLease(
        work_item_id="work-1",
        task_id="task-1",
        page_capture_id="capture-after",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        input_hash="input-1",
        producer_agent_run_id="producer-run-1",
        consumer_agent_run_id="consumer-run-1",
        lease_token="token",
        attempt_no=1,
        row_version=1,
        rules=(
            InspectionWorkItemRule(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=0,
                contract_hash="contract-1",
            ),
        ),
    )
    handoff = build_inspection_handoff(
        lease,
        attempt_id="attempt-1",
        evidence_capture_roles={
            "before": "capture-before",
            "primary": "capture-after",
        },
    )
    assert handoff.evidence.evidence_capture_roles == {
        "before": "capture-before",
        "primary": "capture-after",
    }


@pytest.mark.asyncio
async def test_resolve_roles_pairs_before_action_parent() -> None:
    class Resolver:
        async def resolve(self, *, task_id: str, page_capture_id: str) -> dict[str, str]:
            assert task_id == "task-1"
            assert page_capture_id == "capture-after"
            return {"before": "capture-before", "primary": "capture-after"}

    lease = InspectionWorkItemLease(
        work_item_id="work-1",
        task_id="task-1",
        page_capture_id="capture-after",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        input_hash="input-1",
        producer_agent_run_id="producer-run-1",
        consumer_agent_run_id="consumer-run-1",
        lease_token="token",
        attempt_no=1,
        row_version=1,
        rules=(
            InspectionWorkItemRule(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=0,
                contract_hash="contract-1",
            ),
        ),
    )
    roles = await resolve_inspection_evidence_capture_roles(lease, resolver=Resolver())
    assert roles == {"before": "capture-before", "primary": "capture-after"}


@pytest.mark.asyncio
async def test_postgres_store_resolve_reads_parent_before_action() -> None:
    class Connection:
        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
            assert args == ("capture-after", "task-1")
            return {
                "capture_id": "capture-after",
                "capture_role": "after_action",
                "parent_capture_id": "capture-before",
                "parent_capture_role": "before_action",
            }

    class Acquire:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> Connection:
            return self.connection

        async def __aexit__(self, *args: Any) -> None:
            return None

    class Pool:
        def acquire(self) -> Acquire:
            return Acquire(Connection())

    store = PostgresAppActionEvidenceStore(pool=Pool())
    roles = await store.resolve(task_id="task-1", page_capture_id="capture-after")
    assert roles == {"before": "capture-before", "primary": "capture-after"}


@pytest.mark.asyncio
async def test_postgres_store_attaches_action_log_idempotently_to_verified_pair() -> None:
    class Transaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: Any) -> None:
            return None

    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def transaction(self) -> Transaction:
            return Transaction()

        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
            self.calls.append((" ".join(query.split()), args))
            return {
                "before_image_signature": "sha256:" + "a" * 64,
                "after_image_signature": "sha256:" + "b" * 64,
                "before_package_name": "com.example.app",
                "after_package_name": "com.example.app",
                "before_activity": ".MainActivity",
                "after_activity": ".MainActivity",
                "before_page_type": "home",
                "after_page_type": "content_detail",
            }

        async def fetchval(self, query: str, *args: Any) -> str:
            self.calls.append((" ".join(query.split()), args))
            return str(args[0])

    class Acquire:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> Connection:
            return self.connection

        async def __aexit__(self, *args: Any) -> None:
            return None

    class Pool:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def acquire(self) -> Acquire:
            return Acquire(self.connection)

    connection = Connection()
    store = PostgresAppActionEvidenceStore(pool=Pool(connection))
    entry = build_app_action_log_entry(
        intent=_intent(),
        interaction=_interaction(),
        before_capture_id="capture-before",
        after_capture_id="capture-after",
    )

    await store.attach_action_log(
        task_id="task-1",
        after_capture_id="capture-after",
        before_capture_id="capture-before",
        entry=entry,
    )

    assert len(connection.calls) == 3
    pair_query, pair_args = connection.calls[0]
    after_query, after_args = connection.calls[1]
    before_query, before_args = connection.calls[2]
    assert "before_app.image_signature" in pair_query
    assert "before_capture.agent_run_id = after_capture.agent_run_id" in pair_query
    assert pair_args == (
        "capture-after",
        "task-1",
        "capture-before",
        "action-1",
    )
    assert "item->>'action_execution_id' = $4" in after_query
    assert "capture.capture_role = 'after_action'" in after_query
    assert "capture.parent_capture_id = $5::uuid" in after_query
    assert after_args[3:] == ("action-1", "capture-before")
    enriched_entry = json.loads(after_args[2])[0]
    assert enriched_entry["state_diff"] == {
        "image_changed": True,
        "package_changed": False,
        "activity_changed": False,
        "page_type_changed": True,
        "page_changed": True,
    }
    assert enriched_entry["verification_outcome"] == "verified"
    assert "capture.capture_role = 'before_action'" in before_query
    assert "after_capture.parent_capture_id = capture.id" in before_query
    assert before_args[3:] == ("action-1", "capture-after")


@pytest.mark.asyncio
async def test_rule_judge_evidence_reader_accepts_app_runtime_source() -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    row = _artifact_row(
        payload,
        artifact_type="screenshot",
        content_type="image/png",
    )
    row["source"] = "app_runtime"
    row["storage_path"] = "task-1/capture-1/artifact-1.png"
    pool = FakePool([row], packet_row={"observation_packet_id": "packet-app"})
    reader = PostgresRuleJudgeEvidenceReader(
        pool=pool,
        artifact_store=FakeArtifactStore(payload),
    )

    bundle = await reader.prepare_observation_packet(
        task_id="task-1",
        agent_run_id="run-1",
        rule_id="rule-1",
        page_capture_id="capture-1",
        evidence_capture_roles={"primary": "capture-1"},
        evidence_artifact_ids=("artifact-1",),
        required_artifact_types=("screenshot",),
    )

    assert bundle.observation_packet_id == "packet-app"
    assert bundle.require_unique("screenshot").payload == payload
    query, _args = pool.connection.calls[0]
    assert "app_runtime" in query


def test_feedback_action_records_gap_when_refresh_frame_is_missing() -> None:
    intent = replace(_intent(), semantic_target="关注")
    entry = build_app_action_log_entry(
        intent=intent,
        interaction=_interaction(),
        before_capture_id="capture-before",
        after_capture_id="capture-after",
    )
    assert entry["evidence_capture_roles"] == {
        "before": "capture-before",
        "primary": "capture-after",
    }
    assert entry["gap_codes"] == ["interaction_refresh_capture_missing"]
    assert entry["interaction_feedback"]["complete"] is False
    assert entry["interaction_feedback"]["refreshed_capture_id"] is None
    compact = _compact_action_log((entry,))[0]
    assert compact["gap_codes"] == ["interaction_refresh_capture_missing"]


def test_feedback_action_keeps_three_distinct_frames() -> None:
    intent = replace(_intent(), semantic_target="收藏")
    entry = build_app_action_log_entry(
        intent=intent,
        interaction=_interaction(),
        before_capture_id="capture-before",
        after_capture_id="capture-after",
        refreshed_capture_id="capture-refresh",
    )
    assert entry["evidence_capture_roles"]["refreshed"] == "capture-refresh"
    assert entry["interaction_feedback"]["complete"] is True
    assert "gap_codes" not in entry


def test_feedback_action_rejects_reusing_the_after_frame_as_refresh() -> None:
    intent = replace(_intent(), semantic_target="点赞")
    entry = build_app_action_log_entry(
        intent=intent,
        interaction=_interaction(),
        before_capture_id="capture-before",
        after_capture_id="capture-after",
        refreshed_capture_id="capture-after",
    )
    assert "refreshed" not in entry["evidence_capture_roles"]
    assert entry["gap_codes"] == ["interaction_refresh_capture_missing"]


@pytest.mark.asyncio
async def test_postgres_store_resolve_attaches_distinct_refresh_capture() -> None:
    class Connection:
        def __init__(self) -> None:
            self.calls = 0

        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
            self.calls += 1
            assert args == ("capture-after", "task-1")
            if self.calls == 1:
                return {
                    "capture_id": "capture-after",
                    "capture_role": "after_action",
                    "parent_capture_id": "capture-before",
                    "parent_capture_role": "before_action",
                }
            assert "requirement_role" in query
            return {"capture_id": "capture-refresh"}

    class Acquire:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> Connection:
            return self.connection

        async def __aexit__(self, *args: Any) -> None:
            return None

    class Pool:
        def acquire(self) -> Acquire:
            return Acquire(Connection())

    store = PostgresAppActionEvidenceStore(pool=Pool())
    roles = await store.resolve(task_id="task-1", page_capture_id="capture-after")
    assert roles == {
        "before": "capture-before",
        "primary": "capture-after",
        "refreshed": "capture-refresh",
    }
