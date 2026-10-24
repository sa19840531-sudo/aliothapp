"""Web V2 Runtime 组合根和 exploration_only 终态测试。"""

from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.api.app.agents.runtime.models import ExecutionOutcome, TerminationReason
from services.api.app.api.v2.schemas import V2TaskSubmissionRequest
from services.api.app.application import web_runtime_composition as web_runtime_module
from services.api.app.application.task_submission import TaskSubmissionResult
from services.api.app.application.v2_task_entries import TaskSubmissionV2EntryService
from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_evidence_exit_gate import NoopEvidenceExitGate
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_list_detail_navigation import WebListDetailResult
from services.api.app.application.web_menu_exploration import (
    WebMenuExplorationSummary,
    WebMenuLedgerEntry,
    WebMenuVisitResult,
)
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_inspection_notification import (
    WebPageInspectionNotifyingClassifier,
)
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeAssessmentRecordingClassifier,
)
from services.api.app.application.web_runtime_composition import (
    PostgresWebRuntimeProvider,
    ProductionWebV2RuntimeExecutor,
    WebOrchestratedMenuPageVisitor,
    WebRuntimeConfigurationError,
    WebV2Runtime,
    _web_exploration_is_partial,
)
from services.api.app.core.config import Settings
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.page import WebMenuDecision
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


@pytest.fixture(autouse=True)
def _isolate_composition_unit_environment(monkeypatch):
    """组合单测不继承实站登录和数据库连接配置。"""
    monkeypatch.delenv("ALIOTH_WEB_LOGIN_ENABLED", raising=False)
    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)


@pytest.mark.asyncio
async def test_web_runtime_exposes_auxiliary_href_exploration_without_changing_menu_flow():
    calls = []

    class AuxiliaryExplorer:
        async def explore(self, command, *, source_capture_id, candidate=None):
            calls.append((command, source_capture_id, candidate))
            return SimpleNamespace(status="classified", page_type="search_page")

    runtime = WebV2Runtime(
        bootstrapper=SimpleNamespace(),
        capture_runtime=SimpleNamespace(),
        auxiliary_navigation_explorer=AuxiliaryExplorer(),
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-v2-workflow",
        target_url="https://example.test/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC),
    )

    result = await runtime.explore_auxiliary(
        command,
        source_capture_id="capture-ready",
    )

    assert result.page_type == "search_page"
    assert calls == [(command, "capture-ready", None)]


@pytest.mark.asyncio
async def test_main_web_exploration_lifecycle_attempts_auxiliary_on_current_terminal_page() -> None:
    calls: list[str] = []

    class Explorer:
        async def explore(self, command, **kwargs):
            del command, kwargs
            return SimpleNamespace(
                page_timeline=("capture-home", "capture-evidence-only"),
                page_type_keys=("home",),
                navigation_current_capture_id="capture-current-authoritative",
            )

    class AuxiliaryExplorer:
        async def explore(self, command, *, source_capture_id, candidate=None):
            del command, candidate
            calls.append(source_capture_id)
            return SimpleNamespace(status="no_candidate")

    runtime = WebV2Runtime(
        bootstrapper=SimpleNamespace(),
        capture_runtime=SimpleNamespace(),
        explorer=Explorer(),
        auxiliary_navigation_explorer=AuxiliaryExplorer(),
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-v2-workflow",
        target_url="https://example.test/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC),
    )

    summary = await runtime.explore(command, initial_capture_id="capture-home")

    assert summary.page_timeline[-1] == "capture-evidence-only"
    assert calls == ["capture-current-authoritative"]


@pytest.mark.asyncio
async def test_main_web_lifecycle_projects_each_auxiliary_category_result() -> None:
    class Explorer:
        async def explore(self, command, **kwargs):
            del command, kwargs
            return WebMenuExplorationSummary(
                page_timeline=("capture-home",),
                page_type_keys=("home",),
                navigation_current_capture_id="capture-current",
            )

    class AuxiliaryExplorer:
        async def explore_all(self, command, *, source_capture_id):
            del command
            assert source_capture_id == "capture-current"
            return (
                SimpleNamespace(ready_capture_id="capture-search", page_type="search_page"),
                SimpleNamespace(ready_capture_id="capture-activity", page_type="activity"),
            )

    runtime = WebV2Runtime(
        bootstrapper=SimpleNamespace(),
        capture_runtime=SimpleNamespace(),
        explorer=Explorer(),
        auxiliary_navigation_explorer=AuxiliaryExplorer(),
    )

    summary = await runtime.explore(
        RunWebWorkflowCommand(
            task_id="task-web",
            agent_run_id="run-web",
            session_id="web:run-web",
            session_owner="web-v2-workflow",
            target_url="https://example.test/",
            browser_profile_id="desktop_fhd_1920x1080_v1",
            deadline=datetime.now(UTC),
        ),
        initial_capture_id="capture-home",
    )

    assert summary.page_timeline == (
        "capture-home",
        "capture-search",
        "capture-activity",
    )
    assert summary.page_type_keys == ("home", "search_page", "activity")
    assert summary.navigation_current_capture_id == "capture-activity"


@pytest.mark.asyncio
async def test_main_web_lifecycle_keeps_failed_probe_as_authoritative_current_capture() -> None:
    class Explorer:
        async def explore(self, command, **kwargs):
            del command, kwargs
            return WebMenuExplorationSummary(
                page_timeline=("capture-home",),
                page_type_keys=("home",),
                navigation_current_capture_id="capture-current",
            )

    class AuxiliaryExplorer:
        async def explore_all(self, command, *, source_capture_id):
            del command
            assert source_capture_id == "capture-current"
            return (
                SimpleNamespace(
                    status="failed",
                    probe_capture_id="capture-search-probe",
                    ready_capture_id=None,
                    page_type=None,
                ),
            )

    runtime = WebV2Runtime(
        bootstrapper=SimpleNamespace(),
        capture_runtime=SimpleNamespace(),
        explorer=Explorer(),
        auxiliary_navigation_explorer=AuxiliaryExplorer(),
    )

    summary = await runtime.explore(
        RunWebWorkflowCommand(
            task_id="task-web",
            agent_run_id="run-web",
            session_id="web:run-web",
            session_owner="web-v2-workflow",
            target_url="https://example.test/",
            browser_profile_id="desktop_fhd_1920x1080_v1",
            deadline=datetime.now(UTC),
        ),
        initial_capture_id="capture-home",
    )

    assert summary.page_timeline == ("capture-home",)
    assert summary.page_type_keys == ("home",)
    assert summary.navigation_current_capture_id == "capture-search-probe"


@pytest.mark.asyncio
async def test_postgres_auxiliary_claim_binds_web_v2_current_source_and_terminal_result() -> None:
    from contextlib import asynccontextmanager

    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebAuxiliaryNavigationClaimRepository,
    )

    calls: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        async def fetchval(self, query, *args):
            calls.append((" ".join(query.split()), args))
            return "phase-1"

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    repository = PostgresWebAuxiliaryNavigationClaimRepository(pool=Pool())
    phase_id = await repository.claim(
        task_id="11111111-1111-4111-8111-111111111111",
        agent_run_id="22222222-2222-4222-8222-222222222222",
        session_id="web:22222222-2222-4222-8222-222222222222",
        phase_key="web_search_discovery",
        source_capture_id="33333333-3333-4333-8333-333333333333",
        candidate_source_capture_id="33333333-3333-4333-8333-333333333333",
        candidate_id="candidate-1",
    )
    await repository.finish(
        phase_id=phase_id,
        status="completed",
        reason_code="WEB_AUXILIARY_CLASSIFIED",
        result_capture_id="44444444-4444-4444-8444-444444444444",
        page_type="search_page",
    )

    assert phase_id == "phase-1"
    claim_query, claim_args = calls[0]
    assert "task.target_platform='web'" in claim_query
    assert "task.task_config->>'workflow_version'='v2'" in claim_query
    assert "session.current_capture_id=capture.id" in claim_query
    assert "run.id=(SELECT latest.id" in claim_query
    assert "web_search_discovery" in claim_args
    assert "33333333-3333-4333-8333-333333333333" in claim_args
    assert "candidate-1" in str(claim_args)
    finish_query, finish_args = calls[1]
    assert "result_capture_id" in finish_query
    assert "result_capture.surface_session_id=phase.session_id" in finish_query
    assert "result_capture.surface_kind='web'" in finish_query
    assert (
        "result_capture.metadata->>'finalization_epoch'=phase.finalization_epoch::text"
        in finish_query
    )
    assert "($5::text IS NULL OR result_page.page_type=$5)" in finish_query
    assert "phase.phase_key='web_search_discovery' AND $5=ANY" in finish_query
    assert "phase.phase_key='web_activity_navigation' AND $5=ANY" in finish_query
    assert "RETURNING phase.phase_run_id" in finish_query
    assert finish_args[:5] == (
        "phase-1",
        "completed",
        "WEB_AUXILIARY_CLASSIFIED",
        "44444444-4444-4444-8444-444444444444",
        "search_page",
    )
    assert set(finish_args[5]) == {"web_search_discovery", "web_activity_navigation"}
    assert finish_args[6] == ("search_page",)
    assert finish_args[7] == ("activity", "collection_page")


@pytest.mark.asyncio
async def test_postgres_auxiliary_failed_probe_preserves_reason_and_strict_capture_scope() -> None:
    from contextlib import asynccontextmanager

    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebAuxiliaryNavigationClaimRepository,
    )

    calls: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        async def fetchval(self, query, *args):
            normalized = " ".join(query.split())
            calls.append((normalized, args))
            assert "$2='failed'" in normalized
            assert "($5::text IS NULL OR result_page.page_type=$5)" in normalized
            assert "result_capture.task_id=phase.task_id" in normalized
            assert (
                "result_capture.agent_run_id=( phase.context_payload->>'agent_run_id' )::uuid"
                in normalized
            )
            assert "result_capture.surface_session_id=phase.session_id" in normalized
            assert "result_capture.surface_kind='web'" in normalized
            assert (
                "result_capture.metadata->>'finalization_epoch'=phase.finalization_epoch::text"
                in normalized
            )
            assert "result_capture.capture_status='captured'" in normalized
            return args[0]

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    repository = PostgresWebAuxiliaryNavigationClaimRepository(pool=Pool())

    await repository.finish(
        phase_id="phase-failed",
        status="failed",
        reason_code="WEB_AUXILIARY_FINAL_URL_UNAVAILABLE",
        result_capture_id="44444444-4444-4444-8444-444444444444",
        page_type=None,
    )

    assert calls[0][1][:5] == (
        "phase-failed",
        "failed",
        "WEB_AUXILIARY_FINAL_URL_UNAVAILABLE",
        "44444444-4444-4444-8444-444444444444",
        None,
    )


@pytest.mark.asyncio
async def test_postgres_auxiliary_completed_unknown_page_type_is_rejected() -> None:
    from contextlib import asynccontextmanager

    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebAuxiliaryNavigationClaimRepository,
    )

    class Connection:
        async def fetchval(self, query, *args):
            del query
            assert args[6] == ("search_page",)
            assert args[7] == ("activity", "collection_page")
            return None

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    repository = PostgresWebAuxiliaryNavigationClaimRepository(pool=Pool())
    with pytest.raises(RuntimeError, match="WEB_AUXILIARY_FINISH_SCOPE_INVALID"):
        await repository.finish(
            phase_id="phase-search",
            status="completed",
            reason_code="WEB_AUXILIARY_CLASSIFIED",
            result_capture_id="44444444-4444-4444-8444-444444444444",
            page_type="unknown",
        )


@pytest.mark.asyncio
async def test_postgres_auxiliary_claim_rejects_non_auxiliary_phase_without_sql() -> None:
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebAuxiliaryNavigationClaimRepository,
    )

    class Pool:
        def acquire(self):
            raise AssertionError("非法 phase 不得访问数据库")

    repository = PostgresWebAuxiliaryNavigationClaimRepository(pool=Pool())
    with pytest.raises(ValueError, match="phase_key"):
        await repository.claim(
            task_id="task",
            agent_run_id="run",
            session_id="web:run",
            phase_key="web_menu_navigation",
            source_capture_id="capture",
            candidate_source_capture_id="capture",
            candidate_id="candidate",
        )


@pytest.mark.asyncio
async def test_postgres_auxiliary_finish_rejects_unbound_result_capture() -> None:
    from contextlib import asynccontextmanager

    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebAuxiliaryNavigationClaimRepository,
    )

    class Connection:
        async def fetchval(self, query, *args):
            del query, args
            return None

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    repository = PostgresWebAuxiliaryNavigationClaimRepository(pool=Pool())
    with pytest.raises(RuntimeError, match="WEB_AUXILIARY_FINISH_SCOPE_INVALID"):
        await repository.finish(
            phase_id="phase-1",
            status="completed",
            reason_code="WEB_AUXILIARY_CLASSIFIED",
            result_capture_id="44444444-4444-4444-8444-444444444444",
            page_type="search_page",
        )


class _PolicyRepository:
    async def read(self, *, task_id, agent_run_id):
        from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
        from services.api.tests.test_web_run_policy_snapshot import policy_payload

        return WebRunPolicySnapshot.create(
            task_id=task_id, agent_run_id=agent_run_id, payload=policy_payload()
        )


@pytest.mark.asyncio
async def test_legacy_allowlist_run_is_rejected_before_building_browser():
    from services.api.app.application.web_run_policy import materialize_web_policy
    from services.api.app.domain.exploration.web_discovery_policy import (
        LEGACY_WEB_DISCOVERY_POLICY,
    )
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.app.domain.page.web_site_menu_registry import WebSiteMenuRegistry
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    record = _web_record()
    payload = policy_payload()
    payload["page_catalog"]["version"] = "2026-09-13.web.v8"
    payload["page_catalog"]["page_types"] = [
        item for item in payload["page_catalog"]["page_types"] if item["code"] != "ad_page"
    ]
    payload["page_catalog"]["page_type_groups"].pop("evidence_only")
    payload["guide_version"] = "2026-09-13.web.v8"
    payload["menu_priors"] = WebSiteMenuRegistry().to_payload()
    payload["budget"]["menu_limit"] = 10
    payload["discovery_policy"] = dict(LEGACY_WEB_DISCOVERY_POLICY)
    payload.pop("full_page_supplemental_policy")
    snapshot = WebRunPolicySnapshot.create(
        task_id=record.task_id, agent_run_id=record.run_id, payload=payload
    )

    class HistoricalRepository:
        async def read(self, **kwargs):
            return snapshot

        async def freeze_or_read(self, value):
            raise AssertionError("历史身份不得被重新冻结或迁移")

    with pytest.raises(ValueError, match="WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"):
        materialize_web_policy(snapshot)

    def build_capture_runtime(**kwargs):
        raise AssertionError("历史运行不得构建浏览器或执行检查点")

    provider = PostgresWebRuntimeProvider(
        policy_repository=HistoricalRepository(),
        capture_runtime_builder=build_capture_runtime,
        assessment_agent=SimpleNamespace(),
    )
    with pytest.raises(ValueError, match="WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"):
        await provider.build(record=record, settings=Settings())


@pytest.mark.asyncio
async def test_home_menu_and_detail_classification_share_one_page_type_recorder() -> None:
    calls: list[tuple[str, str]] = []

    class Recorder:
        async def persist(self, *, capture, assessment, **kwargs):
            del kwargs
            calls.append((capture.page_role, assessment.page_type))

    class Classifier:
        async def classify(self, capture):
            return WebPageAssessment(
                page_type={
                    "home": "content_list",
                    "menu": "content_list",
                    "detail": "content_detail",
                }[capture.page_role],
                confidence=0.9,
                evidence=("角色对应的受控完整分类合同",),
            )

    classifier = WebPageTypeAssessmentRecordingClassifier(
        classifier=Classifier(),
        recorder=Recorder(),
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
    )
    for role in ("home", "menu", "detail"):
        await classifier.classify(SimpleNamespace(page_role=role, capture_id=f"capture-{role}"))

    assert calls == [
        ("home", "content_list"),
        ("menu", "content_list"),
        ("detail", "content_detail"),
    ]


def _successful_web_summary(
    *,
    executed: int = 5,
    skipped: int = 2,
    menu_limit: int = 5,
) -> dict[str, object]:
    ledger: list[dict[str, object]] = [
        {
            "entry_id": "home",
            "decision": "home_baseline",
            "status": "home_baseline",
            "execution_slot": None,
            "menu_action_count": 0,
            "detail_action_count": 0,
            "menu_probe_capture_id": "capture-home-probe",
            "menu_ready_capture_id": "capture-home-ready",
            "menu_page_type": "home",
        }
    ]
    for index in range(executed):
        ledger.append(
            {
                "entry_id": f"menu-{index}",
                "decision": "same_origin_collect",
                "status": "detail_captured",
                "execution_slot": index + 1,
                "menu_action_count": 1,
                "menu_intent_id": f"menu-intent-{index}",
                "menu_source_capture_id": f"menu-source-{index}",
                "menu_action_execution_id": f"menu-action-{index}",
                "menu_probe_capture_id": f"menu-probe-{index}",
                "menu_ready_capture_id": f"menu-ready-{index}",
                "menu_page_type": "video_list",
                "detail_action_count": 1,
                "detail_intent_id": f"detail-intent-{index}",
                "detail_source_capture_id": f"detail-source-{index}",
                "detail_action_execution_id": f"detail-action-{index}",
                "detail_probe_capture_id": f"detail-probe-{index}",
                "detail_ready_capture_id": f"detail-ready-{index}",
                "detail_page_type": "video_detail",
            }
        )
    ledger.extend(
        {
            "entry_id": f"skipped-{index}",
            "decision": "same_origin_collect",
            "status": "menu_limit_skipped",
            "execution_slot": None,
            "menu_action_count": 0,
            "detail_action_count": 0,
        }
        for index in range(skipped)
    )
    counts = Counter(str(item["status"]) for item in ledger)
    return {
        "normalization_version": "web-page-type-assessment.v3",
        "menu_total": len(ledger),
        "menu_limit": menu_limit,
        "executed_same_origin_count": executed,
        "menu_limit_skipped_count": skipped,
        "frontier_remaining": 0,
        "menu_ledger": ledger,
        "runtime_discovered_count": 46,
        "status_counts": dict(counts),
    }


def _web_record(*, execution_mode: str = "exploration_only") -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-web",
        task_id="task-web",
        message="web task",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v2",
            "web_runtime_version": "web-runtime.v2",
            "execution_mode": execution_mode,
            "web_inspection_notification_mode": (
                "enforced" if execution_mode == "full_inspection" else "off"
            ),
            # H5 Publication 是正式表兼容键；Web 应用门禁仍只读 Web 键。
            "h5_inspection_notification_mode": (
                "enforced" if execution_mode == "full_inspection" else "off"
            ),
            "browser_profile_snapshot": {
                "profile_id": "desktop_fhd_1920x1080_v1",
                "viewport_width": 1920,
                "viewport_height": 1080,
                "screen_width": 1920,
                "screen_height": 1080,
                "device_scale_factor": 1.0,
                "is_mobile": False,
                "has_touch": False,
                "color_scheme": "light",
            },
            "_task_target": {
                "platform": "web",
                "target_url": "https://example.test/dashboard",
                "app": None,
                "web": {
                    "browser_profile_id": "desktop_fhd_1920x1080_v1",
                    "adult_self_attestation": {
                        "declared": True,
                        "statement_version": "adult-v1",
                        "declared_by": "operator-1",
                        "declared_at": "2026-08-30T12:00:00Z",
                    },
                },
            },
        },
    )


def test_runtime_discovery_does_not_make_catalog_exploration_partial() -> None:
    summary = _successful_web_summary()

    assert (
        _web_exploration_is_partial(
            SimpleNamespace(failed_urls=()),
            summary=summary,
        )
        is False
    )


@pytest.mark.parametrize(
    ("normalization_version", "page_type", "expected_partial"),
    [
        ("web-page-type-normalization.v1", "content_list", False),
        ("web-page-type-assessment.v2", "video_detail", False),
        ("web-page-type-assessment.v3", "home", False),
        ("web-page-type-assessment.v3", "content_list", True),
    ],
)
def test_web_home_baseline_uses_frozen_page_type_version(
    normalization_version: str,
    page_type: str,
    expected_partial: bool,
) -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    summary["normalization_version"] = normalization_version
    summary["menu_ledger"][0]["menu_page_type"] = page_type

    assert (
        _web_exploration_is_partial(SimpleNamespace(failed_urls=()), summary=summary)
        is expected_partial
    )


@pytest.mark.parametrize(
    "normalization_version",
    [None, "", "web-page-type-assessment.v999"],
)
def test_web_home_baseline_is_partial_without_trusted_normalization_version(
    normalization_version: str | None,
) -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    summary["normalization_version"] = normalization_version

    assert _web_exploration_is_partial(
        SimpleNamespace(failed_urls=()),
        summary=summary,
    )


def test_new_ten_menu_budget_can_reach_success_terminal() -> None:
    summary = _successful_web_summary(executed=10, skipped=2, menu_limit=10)

    assert not _web_exploration_is_partial(
        SimpleNamespace(failed_urls=()),
        summary=summary,
    )


def test_configured_twenty_five_menu_budget_can_reach_success_terminal() -> None:
    summary = _successful_web_summary(executed=10, skipped=0, menu_limit=25)

    assert not _web_exploration_is_partial(
        SimpleNamespace(failed_urls=()),
        summary=summary,
    )


@pytest.mark.parametrize(
    "status",
    [
        "popup_blocked",
        "human_verification_blocked",
        "unsafe_navigation",
        "navigation_failed",
        "allowlist_drifted",
        "no_detail_candidate",
        "page_type_unclassified",
        "detail_selection_untrusted",
        "detail_page_type_mismatch",
        "menu_page_captured",
        "evidence_incomplete",
        "budget_conflict",
        "unknown_terminal",
    ],
)
def test_web_exploration_review_statuses_never_report_success(status: str) -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    ledger = list(summary["menu_ledger"])
    ledger[1] = {**ledger[1], "status": status}
    summary["menu_ledger"] = ledger
    summary["status_counts"] = dict(Counter(str(item["status"]) for item in ledger))

    assert _web_exploration_is_partial(SimpleNamespace(failed_urls=()), summary=summary)


def test_web_exploration_recomputes_counts_instead_of_trusting_summary() -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    summary["executed_same_origin_count"] = 10
    summary["status_counts"] = {"home_baseline": 1, "detail_captured": 10}

    assert _web_exploration_is_partial(SimpleNamespace(failed_urls=()), summary=summary)


def test_web_exploration_rejects_eleventh_executed_menu() -> None:
    summary = _successful_web_summary(executed=11, skipped=0)

    assert _web_exploration_is_partial(SimpleNamespace(failed_urls=()), summary=summary)


def test_web_exploration_rejects_broken_list_detail_lineage() -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    ledger = list(summary["menu_ledger"])
    ledger[1] = {**ledger[1], "detail_action_execution_id": None}
    summary["menu_ledger"] = ledger

    assert _web_exploration_is_partial(SimpleNamespace(failed_urls=()), summary=summary)


def test_web_exploration_rejects_ready_capture_aliasing_probe() -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    ledger = list(summary["menu_ledger"])
    ledger[1] = {
        **ledger[1],
        "menu_ready_capture_id": ledger[1]["menu_probe_capture_id"],
    }
    summary["menu_ledger"] = ledger

    assert _web_exploration_is_partial(SimpleNamespace(failed_urls=()), summary=summary)


def test_web_exploration_accepts_direct_detail_without_extra_detail_action() -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    ledger = list(summary["menu_ledger"])
    ledger[1] = {
        **ledger[1],
        "menu_page_type": "content_detail",
        "detail_action_count": 0,
        "detail_intent_id": None,
        "detail_source_capture_id": None,
        "detail_action_execution_id": None,
        "detail_probe_capture_id": None,
        "detail_ready_capture_id": None,
        "detail_page_type": None,
    }
    summary["menu_ledger"] = ledger

    assert not _web_exploration_is_partial(
        SimpleNamespace(failed_urls=()),
        summary=summary,
    )


def test_web_exploration_restores_verified_submenu_expansion_as_complete() -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    ledger = list(summary["menu_ledger"])
    ledger[1] = {
        **ledger[1],
        "status": "menu_page_captured",
        "menu_probe_capture_id": "submenu-after",
        "menu_ready_capture_id": "submenu-after",
        "menu_page_type": None,
        "submenu_relations_verified": True,
        "detail_action_count": 0,
        "detail_intent_id": None,
        "detail_source_capture_id": None,
        "detail_action_execution_id": None,
        "detail_probe_capture_id": None,
        "detail_ready_capture_id": None,
        "detail_page_type": None,
        "diagnostic_type": "WEB_SUBMENU_EXPANDED",
        "menu_interaction_outcome": {
            "command_accepted": True,
            "effect_observed": True,
            "postcondition_satisfied": True,
            "transition_arrived": True,
            "after_capture_id": "submenu-after",
            "failure_reason": None,
            "evidence_mode": "natural",
        },
    }
    summary["menu_ledger"] = ledger
    summary["status_counts"] = dict(Counter(str(item["status"]) for item in ledger))

    assert not _web_exploration_is_partial(
        SimpleNamespace(failed_urls=()),
        summary=summary,
    )


def test_web_exploration_accepts_ordinary_menu_list_as_complete() -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    ledger = list(summary["menu_ledger"])
    ledger[1] = {
        **ledger[1],
        "status": "menu_page_captured",
        "menu_page_type": "content_list",
        "detail_action_count": 0,
        "detail_intent_id": None,
        "detail_source_capture_id": None,
        "detail_action_execution_id": None,
        "detail_probe_capture_id": None,
        "detail_ready_capture_id": None,
        "detail_page_type": None,
        "diagnostic_type": None,
        "diagnostic": {},
        "submenu_relations_verified": False,
    }
    summary["menu_ledger"] = ledger
    summary["status_counts"] = dict(Counter(str(item["status"]) for item in ledger))

    assert not _web_exploration_is_partial(
        SimpleNamespace(failed_urls=()),
        summary=summary,
    )


@pytest.mark.parametrize(
    "missing_field",
    ["execution_slot", "menu_intent_id", "menu_source_capture_id"],
)
def test_ordinary_menu_page_shared_shape_requires_complete_menu_identity(
    missing_field: str,
) -> None:
    from services.api.app.application.web_submenu_expansion import (
        persisted_ordinary_menu_page_has_valid_shape,
    )

    row = {
        "status": "menu_page_captured",
        "execution_slot": 1,
        "menu_action_count": 1,
        "menu_intent_id": "menu-intent",
        "menu_source_capture_id": "menu-source",
        "menu_action_execution_id": "menu-action",
        "menu_probe_capture_id": "menu-probe",
        "menu_ready_capture_id": "menu-ready",
        "menu_page_type": "content_list",
        "detail_action_count": 0,
    }
    row[missing_field] = None

    assert not persisted_ordinary_menu_page_has_valid_shape(row)


@pytest.mark.parametrize(
    "mutation",
    [
        "submenu_page_type_present",
        "submenu_relations_unverified",
        "ordinary_detail_residual",
        "ordinary_missing_execution_slot",
        "ordinary_missing_menu_intent",
        "ordinary_missing_menu_source",
    ],
)
def test_web_exploration_rejects_invalid_menu_page_capture_branch(mutation: str) -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    ledger = list(summary["menu_ledger"])
    row = {
        **ledger[1],
        "status": "menu_page_captured",
        "menu_page_type": "content_list",
        "detail_action_count": 0,
        "detail_intent_id": None,
        "detail_source_capture_id": None,
        "detail_action_execution_id": None,
        "detail_probe_capture_id": None,
        "detail_ready_capture_id": None,
        "detail_page_type": None,
        "diagnostic_type": None,
        "diagnostic": {},
        "submenu_relations_verified": False,
    }
    if mutation.startswith("submenu_"):
        row.update(
            menu_probe_capture_id="submenu-after",
            menu_ready_capture_id="submenu-after",
            diagnostic_type="WEB_SUBMENU_EXPANDED",
            submenu_relations_verified=mutation == "submenu_page_type_present",
            menu_interaction_outcome={
                "command_accepted": True,
                "effect_observed": True,
                "postcondition_satisfied": True,
                "transition_arrived": True,
                "after_capture_id": "submenu-after",
                "failure_reason": None,
                "evidence_mode": "natural",
            },
        )
        if mutation == "submenu_relations_unverified":
            row["menu_page_type"] = None
    elif mutation == "ordinary_detail_residual":
        row["detail_probe_capture_id"] = "unexpected-detail-probe"
    elif mutation == "ordinary_missing_execution_slot":
        row["execution_slot"] = None
    elif mutation == "ordinary_missing_menu_intent":
        row["menu_intent_id"] = None
    else:
        row["menu_source_capture_id"] = None
    ledger[1] = row
    summary["menu_ledger"] = ledger
    summary["status_counts"] = dict(Counter(str(item["status"]) for item in ledger))

    assert _web_exploration_is_partial(SimpleNamespace(failed_urls=()), summary=summary)


def test_web_exploration_rejects_direct_detail_with_extra_detail_probe() -> None:
    summary = _successful_web_summary(executed=1, skipped=0)
    ledger = list(summary["menu_ledger"])
    ledger[1] = {
        **ledger[1],
        "menu_page_type": "content_detail",
        "detail_action_count": 0,
        "detail_intent_id": None,
        "detail_source_capture_id": None,
        "detail_action_execution_id": None,
        "detail_probe_capture_id": "unexpected-detail-probe",
        "detail_ready_capture_id": None,
        "detail_page_type": None,
    }
    summary["menu_ledger"] = ledger

    assert _web_exploration_is_partial(SimpleNamespace(failed_urls=()), summary=summary)


@pytest.mark.asyncio
async def test_bootstrap_order_registers_session_then_catalog_then_home_action() -> None:
    events: list[str] = []

    class Bootstrapper:
        async def open_session(self, command):
            del command
            events.append("session.register_active")

        async def navigate_home(self, command):
            del command
            events.append("home.coordinate")
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-home-probe"))

    class Ledger:
        async def bootstrap(self, command, site, *, catalog_version, catalog_sha256):
            del command, site, catalog_version, catalog_sha256
            events.append("ledger.bootstrap")

        async def finalize(self, record):
            events.append(f"ledger.finalize:{record.status.value}")

    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-v2-workflow",
        target_url="http://91porna.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC),
    )
    runtime = WebV2Runtime(
        bootstrapper=Bootstrapper(),
        capture_runtime=SimpleNamespace(),
        budget_ledger=Ledger(),
        navigation_context=__import__(
            "services.api.app.domain.page",
            fromlist=["WebSiteMenuRegistry"],
        ).WebSiteMenuRegistry(),
        home_ready_capture_reader=SimpleNamespace(),
        page_type_classifier=SimpleNamespace(),
    )

    result = await runtime.prepare_home(
        command,
        adult_self_attestation=SimpleNamespace(),
    )

    assert result.ready_capture_id == "capture-home-probe"
    assert events == [
        "session.register_active",
        "ledger.bootstrap",
        "home.coordinate",
        "ledger.finalize:page_type_unclassified",
    ]


@pytest.mark.asyncio
async def test_home_readiness_reuses_age_gate_absent_verdict_for_same_capture() -> None:
    readiness_calls: list[dict[str, object]] = []

    class Bootstrapper:
        async def start(self, command):
            del command
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-entry"))

    class AgeReader:
        async def read(self, *, capture_id):
            assert capture_id == "capture-entry"
            return SimpleNamespace(capture_id=capture_id)

    class AgeHandler:
        async def handle(self, evidence, *, adult_self_attestation):
            del adult_self_attestation
            assert evidence.capture_id == "capture-entry"
            return SimpleNamespace(
                after_capture_id="capture-after-age",
                gate_disappeared=True,
            )

    class Readiness:
        async def prepare(self, **kwargs):
            readiness_calls.append(kwargs)
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-ready",
                evidence_capture_ids=("capture-after-age", "capture-ready"),
                dismissed_layers=(),
            )

    runtime = WebV2Runtime(
        bootstrapper=Bootstrapper(),
        capture_runtime=SimpleNamespace(),
        age_gate_handler=AgeHandler(),
        age_evidence_reader=AgeReader(),
        page_readiness=Readiness(),
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-v2-workflow",
        target_url="https://example.test/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC),
    )

    result = await runtime.prepare_home(
        command,
        adult_self_attestation=SimpleNamespace(),
    )

    assert result.ready_capture_id == "capture-ready"
    assert readiness_calls == [
        {
            "source_capture_id": "capture-after-age",
            "page_role": "home",
            "age_gate_verified_absent_capture_id": "capture-after-age",
        }
    ]


@pytest.mark.asyncio
async def test_home_age_gate_keeps_pre_post_classification_and_supplemental_order():
    from dataclasses import replace

    from services.api.app.application.web_age_gate import WebAgeGateAssessment, WebAgeGateHandler
    from services.api.app.application.web_page_classification import WebPageAssessment
    from services.api.app.application.web_page_type_assessment import (
        WebAgeGatePageTypeRecorder,
        WebPageTypeAssessmentRecorder,
        WebPageTypeAssessmentRecordingClassifier,
    )
    from services.api.tests.test_web_age_gate import _attestation, _evidence
    from services.api.tests.test_web_page_type_assessment import _capture, _catalog_identity

    calls, persisted = [], {}
    entry = replace(
        _evidence(capture_id="entry"), captured_at=datetime.now(UTC), url="https://example.test/"
    )

    class Repository:
        async def persist_web_page_type_assessment(self, **kwargs):
            calls.append(("record", kwargs["capture_id"], kwargs["canonical_page_type"]))
            assert kwargs["capture_id"] not in persisted
            persisted[kwargs["capture_id"]] = kwargs["assessment_payload"]
            return kwargs["capture_id"]

    class Bootstrapper:
        async def start(self, command):
            calls.append(("entry",))
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="entry"))

    class AgeReader:
        async def read(self, *, capture_id):
            return (
                entry
                if capture_id == "entry"
                else replace(entry, capture_id=capture_id, candidates=())
            )

    class Supplemental:
        async def capture(self, command):
            calls.append(("supplemental", command.source_capture_id))
            return SimpleNamespace(
                status="captured",
                source_capture_id="entry",
                screenshot_artifact_id="entry-long",
                screenshot_artifact_ids=("entry-long",),
                reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
            )

    class Assessor:
        async def assess(self, **kwargs):
            present = bool(kwargs["candidates"])
            return WebAgeGateAssessment(
                gate_present=present,
                confidence=0.9,
                confirmation_candidate_id=entry.candidates[0].candidate_id if present else None,
                reason="年龄门状态",
                human_verification_present=False,
            )

    class Coordinator:
        async def coordinate(self, command):
            calls.append(("click",))
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="after"))

    class Readiness:
        async def prepare(self, **kwargs):
            assert kwargs["source_capture_id"] == "after"
            calls.append(("ready",))
            return SimpleNamespace(
                status="ready",
                ready_capture_id="ready",
                evidence_capture_ids=("after", "ready"),
                dismissed_layers=(),
            )

    class ReadyReader:
        async def read(self, *, capture_id, page_role):
            calls.append(("supplemental", capture_id))
            return replace(
                _capture(),
                capture_id=capture_id,
                supplemental_screenshot_artifact_ids=("ready-long",),
                supplemental_evidence_status="captured",
            )

    class Classifier:
        async def classify(self, capture):
            assert capture.capture_id == "ready"
            assert capture.supplemental_screenshot_artifact_ids == ("ready-long",)
            return WebPageAssessment(page_type="home", confidence=0.9, evidence=("首页",))

    repository, age_reader = Repository(), AgeReader()
    catalog_identity = _catalog_identity()
    runtime = WebV2Runtime(
        bootstrapper=Bootstrapper(),
        capture_runtime=SimpleNamespace(),
        age_gate_handler=WebAgeGateHandler(
            assessor=Assessor(),
            coordinator=Coordinator(),
            evidence_reader=age_reader,
            page_type_recorder=WebAgeGatePageTypeRecorder(
                repository=repository, catalog_identity=catalog_identity
            ),
        ),
        age_evidence_reader=age_reader,
        pre_gate_supplemental_capture=Supplemental(),
        page_readiness=Readiness(),
        home_ready_capture_reader=ReadyReader(),
        page_type_classifier=WebPageTypeAssessmentRecordingClassifier(
            classifier=Classifier(),
            recorder=WebPageTypeAssessmentRecorder(
                repository=repository, catalog_identity=catalog_identity
            ),
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
        ),
    )
    result = await runtime.prepare_home(
        RunWebWorkflowCommand(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            session_owner="web-v2-workflow",
            target_url="https://example.test/",
            browser_profile_id="desktop_fhd_1920x1080_v1",
            deadline=datetime.now(UTC),
        ),
        adult_self_attestation=_attestation(),
    )
    assert calls == [
        ("entry",),
        ("supplemental", "entry"),
        ("record", "entry", "age_gate"),
        ("click",),
        ("ready",),
        ("supplemental", "ready"),
        ("record", "ready", "home"),
    ]
    assert persisted["entry"]["evidence_artifact_ids"][-1] == "entry-long"
    assert persisted["entry"]["final_page_type"] == "age_gate"
    assert result.ready_capture_id == "ready"
    assert result.age_gate_result.post_gate_capture_id == "after"
    assert result.age_gate_result.dismiss_action_id


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["failed", "wrong-parent", "exception", "cancelled"])
async def test_pre_gate_supplemental_failure_preserves_primary_and_cancellation(scenario):
    import asyncio
    from dataclasses import replace

    from services.api.app.application import web_runtime_evidence as module
    from services.api.tests.test_web_age_gate import _evidence

    entry = replace(_evidence(capture_id="entry"), url="https://example.test/")

    class Supplemental:
        async def capture(self, command):
            if scenario == "exception":
                raise OSError("unavailable")
            if scenario == "cancelled":
                raise asyncio.CancelledError()
            return SimpleNamespace(
                status="failed" if scenario == "failed" else "captured",
                source_capture_id="other" if scenario == "wrong-parent" else "entry",
                screenshot_artifact_id="long",
                reason_code="WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
            )

    if scenario == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await module.supplement_web_age_evidence(entry, service=Supplemental())
    else:
        result = await module.supplement_web_age_evidence(entry, service=Supplemental())
        assert result.capture_id == "entry"
        assert result.screenshot_artifact_id == "entry:screenshot"
        assert result.supplemental_screenshot_artifact_ids == ()
        assert result.supplemental_evidence_status == "failed"


@pytest.mark.asyncio
async def test_gate_absent_reuses_entry_supplement_when_home_ready_is_same_capture():
    from dataclasses import replace

    from services.api.app.application.web_age_gate import (
        WebAgeGateAssessment,
        WebAgeGateHandler,
    )
    from services.api.tests.test_web_age_gate import _evidence
    from services.api.tests.test_web_page_type_assessment import _capture

    entry = replace(
        _evidence(capture_id="entry"),
        captured_at=datetime.now(UTC),
        url="https://example.test/",
    )

    class Bootstrapper:
        async def start(self, command):
            del command
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="entry"))

    class AgeReader:
        async def read(self, *, capture_id):
            assert capture_id == "entry"
            return entry

    class Supplemental:
        async def capture(self, command):
            assert command.source_capture_id == "entry"
            return SimpleNamespace(
                status="captured",
                source_capture_id="entry",
                screenshot_artifact_id="entry-long",
                screenshot_artifact_ids=("entry-long",),
                reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
            )

    class Assessor:
        async def assess(self, **kwargs):
            del kwargs
            return WebAgeGateAssessment(
                gate_present=False,
                confidence=0.99,
                confirmation_candidate_id=None,
                reason="无年龄门",
                human_verification_present=False,
            )

    class NeverClick:
        async def coordinate(self, command):
            raise AssertionError(command)

    class Readiness:
        async def prepare(self, **kwargs):
            assert kwargs["source_capture_id"] == "entry"
            return SimpleNamespace(
                status="ready",
                ready_capture_id="entry",
                evidence_capture_ids=("entry",),
                dismissed_layers=(),
            )

    class ReadyReader:
        async def read(self, *, capture_id, page_role):
            assert (capture_id, page_role) == ("entry", "home")
            return replace(
                _capture(),
                capture_id="entry",
                supplemental_screenshot_artifact_ids=(),
                supplemental_evidence_status="failed",
                supplemental_evidence_reason_code=("WEB_FULL_PAGE_SCREENSHOT_ALREADY_ATTEMPTED"),
            )

    class Classifier:
        async def classify(self, capture):
            assert capture.supplemental_screenshot_artifact_ids == ("entry-long",)
            assert capture.supplemental_evidence_status == "captured"
            return SimpleNamespace(page_type="home", raw_page_type="home")

    runtime = WebV2Runtime(
        bootstrapper=Bootstrapper(),
        capture_runtime=SimpleNamespace(),
        age_gate_handler=WebAgeGateHandler(
            assessor=Assessor(),
            coordinator=NeverClick(),
            evidence_reader=AgeReader(),
        ),
        age_evidence_reader=AgeReader(),
        pre_gate_supplemental_capture=Supplemental(),
        page_readiness=Readiness(),
        home_ready_capture_reader=ReadyReader(),
        page_type_classifier=Classifier(),
    )

    await runtime.prepare_home(
        RunWebWorkflowCommand(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            session_owner="web-v2-workflow",
            target_url="https://example.test/",
            browser_profile_id="desktop_fhd_1920x1080_v1",
            deadline=datetime.now(UTC),
        ),
        adult_self_attestation=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["refined", "known", "failed"])
async def test_home_classification_uses_primary_supplement_and_ledgers_latest_evidence(
    scenario,
) -> None:
    classified: list[str] = []
    finalized: list[object] = []

    home_entry = SimpleNamespace(
        entry_id="home",
        decision=WebMenuDecision.HOME_BASELINE,
        classification_hint=None,
    )
    site = SimpleNamespace(
        site_id="example",
        entries=(home_entry,),
        catalog_version="v2",
    )

    class Registry:
        version = "v2"
        catalog_content_sha256 = "sha256:catalog"

        def resolve_site_context(self, target_url):
            assert target_url == "https://example.test/"
            return SimpleNamespace(
                site=site, source_kind="catalog", content_sha256=self.catalog_content_sha256
            )

    class Bootstrapper:
        async def open_session(self, command):
            del command

        async def navigate_home(self, command):
            del command
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-home-probe"))

    class Ledger:
        async def bootstrap(self, *args, **kwargs):
            del args, kwargs

        async def finalize(self, record):
            finalized.append(record)

    class AgeReader:
        async def read(self, *, capture_id):
            return SimpleNamespace(capture_id=capture_id)

    class AgeHandler:
        async def handle(self, evidence, *, adult_self_attestation):
            del evidence, adult_self_attestation
            return SimpleNamespace(after_capture_id=None, gate_disappeared=False)

    class Readiness:
        async def prepare(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-home-ready",
                evidence_capture_ids=("capture-home-ready",),
                dismissed_layers=(),
            )

    class ReadyReader:
        async def read_current(self, *, page_role):
            # 即使补图服务异常，已经发生的新帧也须单独交接；不改原分类基线。
            return await self.read(capture_id="capture-home-primary", page_role=page_role)

        async def read(self, *, capture_id, page_role):
            return SimpleNamespace(
                capture_id=capture_id,
                page_role=page_role,
                url="https://example.test/",
                capture_purpose=(
                    "web_primary_content_evidence"
                    if capture_id == "capture-home-primary"
                    else "web_page_ready_baseline"
                ),
            )

    class Classifier:
        async def classify(self, capture):
            classified.append(capture.capture_id)
            if capture.capture_id == "capture-home-ready" and scenario == "refined":
                return SimpleNamespace(
                    page_type=None,
                    raw_page_type="home",
                    needs_review_reason="WEB_PAGE_TYPE_LOW_CONFIDENCE",
                )
            return SimpleNamespace(
                page_type="home",
                raw_page_type="home",
                needs_review_reason=None,
            )

    class PrimaryCapture:
        async def capture(self, **kwargs):
            assert kwargs["capture_id"] == "capture-home-ready"
            assert kwargs["page_type"] == "home"
            if scenario == "failed":
                raise RuntimeError("补图失败正文不得透传")
            return SimpleNamespace(
                status="captured",
                parent_capture_id="capture-home-ready",
                evidence_capture_id="capture-home-primary",
            )

    runtime = WebV2Runtime(
        bootstrapper=Bootstrapper(),
        capture_runtime=SimpleNamespace(),
        age_gate_handler=AgeHandler(),
        age_evidence_reader=AgeReader(),
        page_readiness=Readiness(),
        budget_ledger=Ledger(),
        navigation_context=Registry(),
        home_ready_capture_reader=ReadyReader(),
        page_type_classifier=Classifier(),
        primary_content_capture=PrimaryCapture(),
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-v2-workflow",
        target_url="https://example.test/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC),
    )

    result = await runtime.prepare_home(
        command,
        adult_self_attestation=SimpleNamespace(),
    )

    assert classified == (
        ["capture-home-ready", "capture-home-primary"]
        if scenario == "refined"
        else ["capture-home-ready"]
    )
    assert result.page_type == "home"
    expected = "capture-home-primary" if scenario == "refined" else "capture-home-ready"
    assert result.ready_capture_id == expected
    assert result.navigation_current_capture_id == "capture-home-primary"
    assert result.navigation_handoff_error is None
    assert finalized[0].menu_ready_capture_id == expected
    assert finalized[0].diagnostic["primary_content_diagnostics"][0]["status"] == (
        "failed" if scenario == "failed" else "captured"
    )
    assert "失败正文" not in str(finalized[0].diagnostic)


@pytest.mark.asyncio
async def test_minimal_api_web_payload_reaches_runtime_without_adult_attestation() -> None:
    """API 省略声明时，冻结快照必须以 None 进入首页门禁判定。"""

    submitted: dict[str, object] = {}

    class SubmissionService:
        async def submit_task(self, **kwargs):
            submitted.update(kwargs)
            return TaskSubmissionResult(
                task_id="task-web",
                run_id="run-web",
                ruleset_id="ruleset-web",
                status="queued",
            )

    payload = {
        "task_name": "Web完整链路测试",
        "target": {"platform": "web", "url": "https://example.test/"},
        "ruleset_code": "alioth_first_ruleset",
        "ruleset_version": 1,
        "channel": "workbench",
        "priority": 100,
        "product": {
            "department_name": "本地测试",
            "product_code": "LOCAL-WEB-E2E",
            "product_name": "本地Web完整链路测试",
        },
        "scan_mode": "full",
        "journey_steps": [],
        "remark": "Web V2 完整链路测试",
    }
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()
    await TaskSubmissionV2EntryService(
        SubmissionService(),  # type: ignore[arg-type]
        web_v2_mode="on",
        now=lambda: datetime(2026, 9, 3, tzinfo=UTC),
    ).submit(
        command,
        access_scope=AccessScope(
            actor_user_id="operator-1",
            tenant_id="tenant-1",
            permissions=frozenset(),
        ),
    )

    target = submitted["target"]
    task_config = dict(submitted["task_config"])  # type: ignore[arg-type]
    task_config["_task_target"] = target.to_dict()  # type: ignore[union-attr]
    target_snapshot = task_config["_task_target"]
    assert isinstance(target_snapshot, dict)
    web_snapshot = target_snapshot["web"]
    assert isinstance(web_snapshot, dict)
    assert web_snapshot["adult_self_attestation"] is None

    runtime_calls: list[object] = []

    class Runtime:
        async def prepare_home(self, command, *, adult_self_attestation):
            del command
            runtime_calls.append(adult_self_attestation)
            return SimpleNamespace(
                coordination=SimpleNamespace(),
                ready_capture_id="capture-home",
                age_gate_result=SimpleNamespace(gate_present=False),
                readiness_result=None,
            )

        async def explore(self, command, *, initial_capture_id, initial_page_type=None):
            del command
            assert initial_capture_id == "capture-home"
            assert initial_page_type is None
            summary = _successful_web_summary()
            return SimpleNamespace(
                **summary,
                page_timeline=("capture-home", "capture-menu-ready"),
                page_type_keys=("content_list", "video_list"),
                failed_urls=(),
                to_dict=lambda: summary,
            )

        async def close(self, *, task_id, session_id):
            assert (task_id, session_id) == ("task-web", "web:run-web")

    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="web-worker-1").run(
        Runtime(),  # type: ignore[arg-type]
        record=AgentRunRecord(
            run_id="run-web",
            task_id="task-web",
            message="web task",
            submitted_at=datetime.now(UTC),
            task_config=task_config,
        ),
        settings=Settings(),
    )

    assert runtime_calls == [None]
    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.web_exploration_summary["adult_self_attestation"] is None


@pytest.mark.asyncio
async def test_persistent_runtime_requires_home_reader_and_bridge_classifier() -> None:
    runtime = WebV2Runtime(
        bootstrapper=SimpleNamespace(),
        capture_runtime=SimpleNamespace(),
        budget_ledger=SimpleNamespace(),
        navigation_context=__import__(
            "services.api.app.domain.page",
            fromlist=["WebSiteMenuRegistry"],
        ).WebSiteMenuRegistry(),
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-v2-workflow",
        target_url="http://91porna.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC),
    )

    with pytest.raises(
        WebRuntimeConfigurationError,
        match="WEB_HOME_PAGE_TYPE_ASSESSMENT_REQUIRED",
    ):
        await runtime.prepare_home(command, adult_self_attestation=SimpleNamespace())


@pytest.mark.asyncio
async def test_unrecognized_home_finalizes_as_page_type_unclassified_not_home_baseline() -> None:
    records: list[object] = []

    class Ledger:
        async def finalize(self, record):
            records.append(record)

    site = (
        __import__(
            "services.api.app.domain.page",
            fromlist=["WebSiteMenuRegistry"],
        )
        .WebSiteMenuRegistry()
        .resolve_site("http://91porna.com/")
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-v2-workflow",
        target_url="http://91porna.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC),
    )
    runtime = WebV2Runtime(
        bootstrapper=SimpleNamespace(),
        capture_runtime=SimpleNamespace(),
        budget_ledger=Ledger(),
        navigation_context=SimpleNamespace(),
    )

    await runtime._finalize_home(
        command,
        site=site,
        result=SimpleNamespace(
            coordination=SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-home")),
            ready_capture_id="capture-home-ready",
            page_type=None,
            raw_page_type="web_unknown_home",
        ),
    )

    assert records[0].status is WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
    assert records[0].menu_page_type is None
    assert records[0].diagnostic == {"error_code": "WEB_PAGE_TYPE_UNCLASSIFIED"}


@pytest.mark.asyncio
async def test_runtime_start_with_persistent_budget_cannot_skip_catalog_bootstrap() -> None:
    events: list[str] = []

    class Bootstrapper:
        async def open_session(self, command):
            del command
            events.append("session.register_active")

        async def navigate_home(self, command):
            del command
            events.append("home.coordinate")
            return "home-coordination"

        async def start(self, command):
            raise AssertionError(command)

    class Ledger:
        async def bootstrap(self, command, site, *, catalog_version, catalog_sha256):
            del command, site, catalog_version, catalog_sha256
            events.append("ledger.bootstrap")

    registry = __import__(
        "services.api.app.domain.page",
        fromlist=["WebSiteMenuRegistry"],
    ).WebSiteMenuRegistry()
    runtime = WebV2Runtime(
        bootstrapper=Bootstrapper(),
        capture_runtime=SimpleNamespace(),
        budget_ledger=Ledger(),
        navigation_context=registry,
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-v2-workflow",
        target_url="http://91porna.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC),
    )

    result = await runtime.start(command)

    assert result == "home-coordination"
    assert events == [
        "session.register_active",
        "ledger.bootstrap",
        "home.coordinate",
    ]


@pytest.mark.asyncio
async def test_persistent_runtime_seals_budget_after_menu_explorer_returns() -> None:
    """正式收口只能在完整菜单探索返回后看到 sealed 预算。"""

    events: list[str] = []

    class Explorer:
        async def explore(self, command, *, initial_capture_id, initial_page_type=None):
            del command, initial_capture_id, initial_page_type
            events.append("explorer.completed")
            return SimpleNamespace(page_timeline=("capture-home",))

    class Ledger:
        async def seal(self, *, command, site_id):
            del command
            events.append(f"ledger.sealed:{site_id}")

    registry = __import__(
        "services.api.app.domain.page",
        fromlist=["WebSiteMenuRegistry"],
    ).WebSiteMenuRegistry()
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-v2-workflow",
        target_url="http://91porna.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC),
    )
    runtime = WebV2Runtime(
        bootstrapper=SimpleNamespace(),
        capture_runtime=SimpleNamespace(),
        explorer=Explorer(),
        budget_ledger=Ledger(),
        navigation_context=registry,
    )

    await runtime.explore(command, initial_capture_id="capture-home")

    assert events == ["explorer.completed", "ledger.sealed:91porna_com"]


@pytest.mark.asyncio
async def test_detail_binding_failure_preserves_menu_and_detail_lineage() -> None:
    command = SimpleNamespace(session_id="web:run-web")
    entry = SimpleNamespace(entry_id="entry-1")

    class Navigator:
        async def visit(self, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id="capture-menu-probe",
                page_type=None,
                menu_action_count=1,
                menu_intent_id="web-menu:reserved",
                menu_action_execution_id="actual-menu-action",
            )

    class Readiness:
        async def prepare(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-menu-ready",
                evidence_capture_ids=("capture-menu-probe", "capture-menu-ready"),
                dismissed_layers=(),
            )

    class ReadyReader:
        async def read(self, **kwargs):
            del kwargs
            return SimpleNamespace()

    class Classifier:
        async def classify(self, capture):
            del capture
            return SimpleNamespace(page_type="content_list", needs_review_reason=None)

    class EvidenceSource:
        async def read_list(self, *, capture_id):
            assert capture_id == "capture-menu-ready"
            return SimpleNamespace()

    class DetailNavigator:
        async def capture_one(self, evidence, **kwargs):
            del evidence, kwargs
            return WebListDetailResult(
                status=WebMenuTerminalStatus.DETAIL_CAPTURED,
                detail_capture_id="capture-detail-ready",
                page_type="content_detail",
                probe_capture_id="capture-detail-probe",
                detail_action_count=1,
                detail_intent_id="web-detail:reserved",
                detail_action_execution_id="actual-detail-action",
            )

    class CandidateSource:
        def bind_latest_navigation_capture(self, session_id, capture_id):
            assert session_id == "web:run-web"
            if capture_id == "capture-detail-ready":
                raise RuntimeError("binding failed")

    result = await WebOrchestratedMenuPageVisitor(
        navigator=Navigator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyReader(),
        classifier=Classifier(),
        evidence_source=EvidenceSource(),
        detail_navigator=DetailNavigator(),
        navigation_candidate_source=CandidateSource(),
    ).visit(
        command=command,
        entry=entry,
        candidate=SimpleNamespace(),
        source_capture_id="capture-home",
        menu_intent_id="web-menu:reserved",
    )

    assert result.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert result.page_type == "content_list"
    assert result.menu_page_type == "content_list"
    assert result.menu_probe_capture_id == "capture-menu-probe"
    assert result.menu_ready_capture_id == "capture-menu-ready"
    assert result.menu_action_execution_id == "actual-menu-action"
    assert result.detail_probe_capture_id == "capture-detail-probe"
    assert result.detail_ready_capture_id == "capture-detail-ready"
    assert result.detail_intent_id == "web-detail:reserved"
    assert result.detail_action_execution_id == "actual-detail-action"


@pytest.mark.asyncio
async def test_unclassified_menu_is_review_terminal_not_navigation_failure() -> None:
    class Navigator:
        async def visit(self, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id="capture-menu-probe",
                page_type=None,
                menu_action_count=1,
                menu_intent_id="web-menu:reserved",
                menu_action_execution_id="actual-menu-action",
            )

    class Readiness:
        async def prepare(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-menu-ready",
                evidence_capture_ids=("capture-menu-probe", "capture-menu-ready"),
                dismissed_layers=(),
            )

    class ReadyReader:
        async def read(self, **kwargs):
            del kwargs
            return SimpleNamespace()

    class Classifier:
        async def classify(self, capture):
            del capture
            return SimpleNamespace(
                page_type=None,
                raw_page_type="content_list",
                proposed_page_type="content_list",
                needs_review_reason="WEB_PAGE_TYPE_LOW_CONFIDENCE",
                diagnostic_message="模型候选仍需人工复核",
            )

    class NeverCalled:
        async def read_list(self, **kwargs):
            raise AssertionError(kwargs)

        async def capture_one(self, *args, **kwargs):
            raise AssertionError((args, kwargs))

    result = await WebOrchestratedMenuPageVisitor(
        navigator=Navigator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyReader(),
        classifier=Classifier(),
        evidence_source=NeverCalled(),
        detail_navigator=NeverCalled(),
    ).visit(
        command=SimpleNamespace(session_id="web:run-web"),
        entry=SimpleNamespace(entry_id="entry-1", classification_hint=None),
        candidate=SimpleNamespace(),
        source_capture_id="capture-home",
        menu_intent_id="web-menu:reserved",
    )

    assert result.status is WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
    assert result.error_code == "WEB_PAGE_TYPE_UNCLASSIFIED"
    assert result.menu_page_type == "content_list"
    assert result.diagnostic_type == "WEB_PAGE_TYPE_LOW_CONFIDENCE"
    assert result.diagnostic_message == "模型候选仍需人工复核"


@pytest.mark.asyncio
async def test_unknown_formal_page_type_is_unclassified_menu_terminal() -> None:
    """正式分类的 unknown 是未判定，不得伪装为菜单页采集成功。"""

    class Navigator:
        async def visit(self, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id="capture-menu-probe",
                page_type=None,
                menu_action_count=1,
                menu_intent_id="web-menu:reserved",
                menu_action_execution_id="actual-menu-action",
            )

    class Readiness:
        async def prepare(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-menu-ready",
                evidence_capture_ids=("capture-menu-probe", "capture-menu-ready"),
                dismissed_layers=(),
            )

    class ReadyReader:
        async def read(self, **kwargs):
            del kwargs
            return SimpleNamespace()

    class Classifier:
        async def classify(self, capture):
            del capture
            return SimpleNamespace(
                page_type="unknown",
                raw_page_type="unknown",
                proposed_page_type="unknown",
                needs_review_reason="WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT",
                diagnostic_message="页面类型证据不足",
            )

    class NeverCalled:
        async def read_list(self, **kwargs):
            raise AssertionError(kwargs)

        async def capture_one(self, *args, **kwargs):
            raise AssertionError((args, kwargs))

    result = await WebOrchestratedMenuPageVisitor(
        navigator=Navigator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyReader(),
        classifier=Classifier(),
        evidence_source=NeverCalled(),
        detail_navigator=NeverCalled(),
    ).visit(
        command=SimpleNamespace(session_id="web:run-web"),
        entry=SimpleNamespace(entry_id="entry-1", classification_hint=None),
        candidate=SimpleNamespace(),
        source_capture_id="capture-home",
        menu_intent_id="web-menu:reserved",
    )

    assert result.status is WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
    assert result.error_code == "WEB_PAGE_TYPE_UNCLASSIFIED"
    assert result.page_type == "unknown"
    assert result.menu_page_type == "unknown"
    assert result.diagnostic_type == "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT"


@pytest.mark.asyncio
async def test_nonready_menu_keeps_latest_capture_for_next_frozen_href_goto() -> None:
    """菜单页未就绪只关闭本页判定，不应阻断下一个已冻结同源 href。"""

    class Navigator:
        async def visit(self, **_kwargs):
            return WebMenuVisitResult(
                final_capture_id="capture-menu-probe",
                page_type=None,
                menu_action_count=1,
                menu_intent_id="web-menu:reserved",
                menu_action_execution_id="actual-menu-action",
            )

    class Readiness:
        async def prepare(self, **_kwargs):
            return SimpleNamespace(
                status="timeout",
                ready_capture_id=None,
                evidence_capture_ids=("capture-menu-probe", "capture-current-nonready"),
                dismissed_layers=(),
            )

    class ReadyReader:
        async def read_current(self, **_kwargs):
            raise RuntimeError("非就绪页没有正式分类基线")

    class NeverCalled:
        async def read(self, **kwargs):
            raise AssertionError(kwargs)

        async def classify(self, *args, **kwargs):
            raise AssertionError((args, kwargs))

        async def read_list(self, **kwargs):
            raise AssertionError(kwargs)

        async def capture_one(self, *args, **kwargs):
            raise AssertionError((args, kwargs))

    result = await WebOrchestratedMenuPageVisitor(
        navigator=Navigator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyReader(),
        classifier=NeverCalled(),
        evidence_source=NeverCalled(),
        detail_navigator=NeverCalled(),
        primary_content_capture=object(),
    ).visit(
        command=SimpleNamespace(session_id="web:run-web"),
        entry=SimpleNamespace(entry_id="entry-1"),
        candidate=SimpleNamespace(),
        source_capture_id="capture-home",
        menu_intent_id="web-menu:reserved",
    )

    assert result.status is WebMenuTerminalStatus.POPUP_BLOCKED
    assert result.error_code == "WEB_MENU_POPUP_BLOCKED"
    assert result.navigation_current_capture_id == "capture-current-nonready"
    assert result.navigation_handoff_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_code"),
    (
        (WebMenuTerminalStatus.NAVIGATION_FAILED, "WEB_DETAIL_READINESS_FAILED"),
        (WebMenuTerminalStatus.POPUP_BLOCKED, "WEB_DETAIL_POPUP_BLOCKED"),
    ),
)
async def test_detail_readiness_failure_recovers_real_detail_capture_for_next_href(
    status: WebMenuTerminalStatus,
    error_code: str,
) -> None:
    """详情已导航但就绪失败时，应从真实详情 Capture 恢复下一菜单来源。"""

    detail_outcome = SimpleNamespace(
        after_capture_id="capture-detail-probe",
        command_accepted=True,
        effect_observed=True,
        postcondition_satisfied=True,
        transition_arrived=False,
        failure_reason="readiness_not_accepted",
    )

    class Visitor(WebOrchestratedMenuPageVisitor):
        async def _visit(self, _kwargs):
            return WebMenuVisitResult(
                final_capture_id="capture-menu-ready",
                page_type="video_list",
                status=status,
                error_code=error_code,
                detail_probe_capture_id="capture-detail-probe",
                detail_action_count=1,
                detail_interaction_outcome=detail_outcome,
            )

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            raise AssertionError((source_capture_id, page_role))

    class ReadyReader:
        async def read_current(self, **_kwargs):
            raise RuntimeError("详情未就绪，不能作为菜单分类基线读取")

        async def read(self, *, capture_id, page_role):
            raise AssertionError((capture_id, page_role))

    bound = []

    class CandidateSource:
        def bind_latest_navigation_capture(self, session_id, capture_id):
            bound.append((session_id, capture_id))

    visitor = Visitor(
        navigator=SimpleNamespace(),
        readiness=Readiness(),
        ready_capture_reader=ReadyReader(),
        classifier=SimpleNamespace(),
        evidence_source=SimpleNamespace(),
        detail_navigator=SimpleNamespace(),
        primary_content_capture=object(),
        navigation_candidate_source=CandidateSource(),
    )

    result = await visitor.visit(command=SimpleNamespace(session_id="web:run-web"))

    assert result.status is status
    assert result.error_code == error_code
    assert result.navigation_current_capture_id == "capture-detail-probe"
    assert result.navigation_handoff_error is None
    assert bound == [("web:run-web", "capture-detail-probe")]


@pytest.mark.asyncio
async def test_primary_content_scroll_failure_recovers_real_menu_capture_for_next_href() -> None:
    """菜单主体补图已滚动但就绪失败时，应恢复真实菜单 Capture 继续冻结 href。"""

    class Visitor(WebOrchestratedMenuPageVisitor):
        async def _visit(self, _kwargs):
            return WebMenuVisitResult(
                final_capture_id="capture-menu-ready",
                page_type="content_list",
                status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                error_code="WEB_MENU_POST_NAVIGATION_FAILED",
                primary_content_diagnostics=(
                    {
                        "reason_code": "WEB_PRIMARY_CONTENT_AFTER_UNTRUSTED",
                        "interaction_outcomes": [
                            {
                                "after_capture_id": "capture-menu-scrolled",
                                "command_accepted": True,
                                "effect_observed": True,
                                "postcondition_satisfied": True,
                                "transition_arrived": False,
                                "failure_reason": "readiness_not_accepted",
                                "evidence_mode": "natural",
                            }
                        ],
                    },
                ),
            )

    class Readiness:
        async def prepare(self, *, source_capture_id, page_role):
            raise AssertionError((source_capture_id, page_role))

    class ReadyReader:
        async def read_current(self, **_kwargs):
            raise RuntimeError("主体补图失败后没有正式当前帧")

        async def read(self, *, capture_id, page_role):
            raise AssertionError((capture_id, page_role))

    bound = []

    class CandidateSource:
        def bind_latest_navigation_capture(self, session_id, capture_id):
            bound.append((session_id, capture_id))

    result = await Visitor(
        navigator=SimpleNamespace(),
        readiness=Readiness(),
        ready_capture_reader=ReadyReader(),
        classifier=SimpleNamespace(),
        evidence_source=SimpleNamespace(),
        detail_navigator=SimpleNamespace(),
        primary_content_capture=object(),
        navigation_candidate_source=CandidateSource(),
    ).visit(command=SimpleNamespace(session_id="web:run-web"))

    assert result.navigation_current_capture_id == "capture-menu-scrolled"
    assert result.navigation_handoff_error is None
    assert bound == [("web:run-web", "capture-menu-scrolled")]


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_page_type", ("search_page", "activity"))
async def test_menu_landing_on_auxiliary_type_closes_as_mismatch_without_writing_menu_type(
    actual_page_type: str,
) -> None:
    class Navigator:
        async def visit(self, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id="capture-menu-probe",
                page_type=None,
                menu_action_count=1,
                menu_intent_id="web-menu:reserved",
            )

    class Readiness:
        async def prepare(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-menu-ready",
                evidence_capture_ids=("capture-menu-probe", "capture-menu-ready"),
                dismissed_layers=(),
            )

    class ReadyReader:
        async def read(self, **kwargs):
            del kwargs
            return SimpleNamespace()

    class Classifier:
        async def classify(self, capture):
            del capture
            return SimpleNamespace(
                page_type=actual_page_type,
                raw_page_type=actual_page_type,
                needs_review_reason=None,
            )

    class NeverCalled:
        async def read_list(self, **kwargs):
            raise AssertionError(kwargs)

        async def capture_one(self, *args, **kwargs):
            raise AssertionError((args, kwargs))

    result = await WebOrchestratedMenuPageVisitor(
        navigator=Navigator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyReader(),
        classifier=Classifier(),
        evidence_source=NeverCalled(),
        detail_navigator=NeverCalled(),
    ).visit(
        command=SimpleNamespace(session_id="web:run-web"),
        entry=SimpleNamespace(entry_id="entry-1", classification_hint=None),
        candidate=SimpleNamespace(),
        source_capture_id="capture-home",
        menu_intent_id="web-menu:reserved",
    )

    assert result.status is WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
    assert result.error_code == "WEB_MENU_PAGE_TYPE_MISMATCH"
    assert result.page_type == actual_page_type
    assert result.menu_page_type is None


@pytest.mark.parametrize("failed_capture", ("capture-menu-probe", "capture-menu-ready"))
@pytest.mark.asyncio
async def test_menu_binding_failure_preserves_actual_action_and_existing_capture(
    failed_capture: str,
) -> None:
    class Navigator:
        async def visit(self, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id="capture-menu-probe",
                page_type=None,
                menu_action_count=1,
                menu_intent_id="web-menu:reserved",
                menu_action_execution_id="actual-menu-action",
            )

    class Readiness:
        async def prepare(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-menu-ready",
                evidence_capture_ids=("capture-menu-probe", "capture-menu-ready"),
                dismissed_layers=(),
            )

    class NeverCalled:
        async def read(self, **kwargs):
            raise AssertionError(kwargs)

        async def classify(self, *args, **kwargs):
            raise AssertionError((args, kwargs))

        async def read_list(self, **kwargs):
            raise AssertionError(kwargs)

        async def capture_one(self, *args, **kwargs):
            raise AssertionError((args, kwargs))

    class CandidateSource:
        def bind_latest_navigation_capture(self, session_id, capture_id):
            assert session_id == "web:run-web"
            if capture_id == failed_capture:
                raise RuntimeError("binding failed")

    result = await WebOrchestratedMenuPageVisitor(
        navigator=Navigator(),
        readiness=Readiness(),
        ready_capture_reader=NeverCalled(),
        classifier=NeverCalled(),
        evidence_source=NeverCalled(),
        detail_navigator=NeverCalled(),
        navigation_candidate_source=CandidateSource(),
    ).visit(
        command=SimpleNamespace(session_id="web:run-web"),
        entry=SimpleNamespace(entry_id="entry-1"),
        candidate=SimpleNamespace(),
        source_capture_id="capture-home",
        menu_intent_id="web-menu:reserved",
    )

    assert result.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert result.menu_probe_capture_id == "capture-menu-probe"
    assert result.menu_ready_capture_id == (
        "capture-menu-ready" if failed_capture == "capture-menu-ready" else None
    )
    assert result.menu_intent_id == "web-menu:reserved"
    assert result.menu_action_execution_id == "actual-menu-action"


@pytest.mark.asyncio
async def test_bootstrap_order_home_failure_preserves_full_persisted_catalog_summary() -> None:
    ledger = tuple(
        WebMenuLedgerEntry(
            entry_id=f"entry-{index}",
            menu_path=(f"菜单 {index}",),
            canonical_url=f"https://example.test/{index}",
            status=WebMenuTerminalStatus.NAVIGATION_FAILED,
            source_capture_id="",
            final_capture_id=None,
            page_type=None,
            detail_capture_id=None,
            error_code="WEB_BUDGET_RESERVED_NEEDS_REVIEW",
        )
        for index in range(3)
    )
    persisted = WebMenuExplorationSummary(
        site_id="example",
        catalog_version="catalog-v1",
        menu_total=3,
        menu_ledger=ledger,
        status_counts={"navigation_failed": 3},
        page_timeline=(),
        page_type_keys=(),
    )

    class Runtime:
        async def prepare_home(self, command, *, adult_self_attestation):
            del command, adult_self_attestation
            raise RuntimeError("HOME_COORDINATION_FAILED")

        async def read_persisted_exploration(self, command):
            del command
            return persisted

        async def close(self, *, task_id, session_id):
            del task_id, session_id

    record = _web_record()
    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="web-worker-1").run(
        Runtime(), record=record, settings=Settings()
    )

    assert result.termination_reason is TerminationReason.ERROR
    assert result.web_exploration_summary["site_id"] == "example"
    assert result.web_exploration_summary["menu_total"] == 3
    assert len(result.web_exploration_summary["menu_ledger"]) == 3


@pytest.mark.parametrize(
    ("menu_page_type", "expected_detail_count", "expected_detail_action_id"),
    (
        ("content_detail", 0, None),
        ("content_list", 1, "actual-detail-action"),
    ),
)
@pytest.mark.asyncio
async def test_action_execution_direct_and_list_keep_distinct_lineage(
    menu_page_type: str,
    expected_detail_count: int,
    expected_detail_action_id: str | None,
) -> None:
    command = SimpleNamespace(session_id="web:run-web")
    entry = SimpleNamespace(entry_id="entry-1")

    class Navigator:
        async def visit(self, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id="capture-menu-probe",
                page_type=None,
                menu_action_count=1,
                menu_intent_id="web-menu:reserved-entry",
                menu_action_execution_id="actual-menu-action",
            )

    class Readiness:
        async def prepare(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-menu-ready",
                evidence_capture_ids=("capture-menu-probe", "capture-menu-ready"),
                dismissed_layers=(),
            )

    class ReadyReader:
        async def read(self, **kwargs):
            del kwargs
            return SimpleNamespace()

    class Classifier:
        async def classify(self, capture):
            del capture
            return SimpleNamespace(page_type=menu_page_type, needs_review_reason=None)

    class EvidenceSource:
        async def read_list(self, *, capture_id):
            assert capture_id == "capture-menu-ready"
            return SimpleNamespace()

    class DetailNavigator:
        async def capture_one(self, evidence, *, list_page_type, command, entry):
            del evidence
            assert list_page_type == "content_list"
            assert command is command_value
            assert entry is entry_value
            return WebListDetailResult(
                status=WebMenuTerminalStatus.DETAIL_CAPTURED,
                detail_capture_id="capture-detail-ready",
                page_type="content_detail",
                probe_capture_id="capture-detail-probe",
                detail_action_count=1,
                detail_intent_id="web-detail:reserved-entry",
                detail_action_execution_id="actual-detail-action",
            )

    command_value = command
    entry_value = entry
    result = await WebOrchestratedMenuPageVisitor(
        navigator=Navigator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyReader(),
        classifier=Classifier(),
        evidence_source=EvidenceSource(),
        detail_navigator=DetailNavigator(),
    ).visit(
        command=command,
        entry=entry,
        candidate=SimpleNamespace(),
        source_capture_id="capture-home",
        menu_intent_id="web-menu:reserved-entry",
    )

    assert result.menu_action_count == 1
    assert result.menu_intent_id == "web-menu:reserved-entry"
    assert result.menu_action_execution_id == "actual-menu-action"
    assert result.detail_action_count == expected_detail_count
    assert result.detail_action_execution_id == expected_detail_action_id
    if expected_detail_count == 0:
        assert result.detail_ready_capture_id == "capture-menu-ready"
        assert result.detail_intent_id is None


@pytest.mark.asyncio
async def test_menu_classification_uses_one_primary_content_supplement_after_review() -> None:
    classifier_capture_ids: list[str] = []
    primary_calls: list[dict[str, object]] = []

    class Navigator:
        async def visit(self, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id="capture-menu-probe",
                page_type=None,
                status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
                menu_action_count=1,
                menu_intent_id="web-menu:entry-1",
                menu_action_execution_id="menu-action-1",
            )

    class Readiness:
        async def prepare(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-menu-ready",
                evidence_capture_ids=("capture-menu-probe", "capture-menu-ready"),
                dismissed_layers=(),
            )

    class ReadyReader:
        async def read_current(self, *, page_role):
            return await self.read(capture_id="capture-menu-primary", page_role=page_role)

        async def read(self, *, capture_id, page_role):
            return SimpleNamespace(
                capture_id=capture_id,
                page_role=page_role,
                url="https://example.test/articles",
                capture_purpose=(
                    "web_primary_content_evidence"
                    if capture_id == "capture-menu-primary"
                    else "web_page_ready_baseline"
                ),
            )

    class Classifier:
        async def classify(self, capture):
            classifier_capture_ids.append(capture.capture_id)
            if capture.capture_id == "capture-menu-ready":
                return SimpleNamespace(
                    page_type=None,
                    raw_page_type="content_list",
                    needs_review_reason="WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT",
                )
            return SimpleNamespace(
                page_type="content_list",
                raw_page_type="content_list",
                needs_review_reason=None,
            )

    class PrimaryCapture:
        async def capture(self, **kwargs):
            primary_calls.append(kwargs)
            return SimpleNamespace(
                status="captured",
                evidence_capture_id="capture-menu-primary",
            )

    class EvidenceSource:
        async def read_list(self, *, capture_id):
            assert capture_id == "capture-menu-primary"
            return SimpleNamespace()

    class DetailNavigator:
        async def capture_one(self, evidence, *, list_page_type, command, entry):
            del evidence, command, entry
            assert list_page_type == "content_list"
            return WebListDetailResult(
                status=WebMenuTerminalStatus.DETAIL_CAPTURED,
                detail_capture_id="capture-detail-ready",
                page_type="content_detail",
            )

    command = SimpleNamespace(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        deadline=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
    )
    result = await WebOrchestratedMenuPageVisitor(
        navigator=Navigator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyReader(),
        classifier=Classifier(),
        evidence_source=EvidenceSource(),
        detail_navigator=DetailNavigator(),
        primary_content_capture=PrimaryCapture(),
    ).visit(
        command=command,
        entry=SimpleNamespace(entry_id="entry-1", classification_hint=None),
        candidate=SimpleNamespace(),
        source_capture_id="capture-home",
        menu_intent_id="web-menu:entry-1",
    )

    assert classifier_capture_ids == ["capture-menu-ready", "capture-menu-primary"]
    assert [call["capture_id"] for call in primary_calls].count("capture-menu-ready") == 1
    assert primary_calls[0]["page_type"] == "content_list"
    assert result.page_type == "content_list"
    assert result.status is WebMenuTerminalStatus.DETAIL_CAPTURED


@pytest.mark.asyncio
async def test_web_executor_home_only_without_catalog_ledger_requires_review() -> None:
    calls: dict[str, object] = {}

    class Bootstrapper:
        async def start(self, command):
            calls["command"] = command
            return SimpleNamespace(
                before_capture=SimpleNamespace(capture_id="capture-before"),
                after_capture=SimpleNamespace(capture_id="capture-after"),
            )

    class CaptureRuntime:
        async def close_exploration_session(self, *, task_id, session_id):
            calls["closed"] = (task_id, session_id)

    runtime = WebV2Runtime(
        bootstrapper=Bootstrapper(),
        capture_runtime=CaptureRuntime(),
    )
    executor = ProductionWebV2RuntimeExecutor(
        worker_instance_id="web-worker-1",
        now=lambda: datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
    )

    result = await executor.run(
        runtime,
        record=_web_record(),
        settings=Settings(),
    )

    command = calls["command"]
    assert command.session_id == "web:run-web"
    assert command.session_owner == "web-v2-workflow"
    assert command.target_url == "https://example.test/dashboard"
    assert command.browser_profile_id == "desktop_fhd_1920x1080_v1"
    assert calls["closed"] == ("task-web", "web:run-web")
    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.exploration_status == "partial_evidence"
    assert result.evidence_index_id == "capture-after"
    assert result.page_timeline == ("capture-after",)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_executor_drives_durable_stages_and_does_not_reenter_home_after_report(expired):
    calls = []

    class Stages:
        async def initialize(self, command, *, adult_self_attestation):
            calls.append("initialize")
            self.command = command

        async def next_due(self, run_id):
            assert run_id == "run-web"
            if "session_close" not in calls:
                return SimpleNamespace(logical_page_key="home")
            return None

        async def advance_once(self, run_id, logical_page_key):
            assert (run_id, logical_page_key) == ("run-web", "home")
            calls.append("session_close")

        async def final_result(self, run_id):
            return SimpleNamespace(
                preparation=None,
                exploration=SimpleNamespace(
                    page_timeline=("capture-report",),
                    page_type_keys=("home",),
                    failed_urls=(),
                ),
            )

    class Runtime:
        stage_orchestrator = Stages()
        deadline = datetime(2020 if expired else 2099, 1, 1, tzinfo=UTC)

        async def prepare_home(self, *_args, **_kwargs):
            raise AssertionError("报告证据已完成，禁止重新进入首页")

        async def close(self, **_kwargs):
            raise AssertionError("关闭动作必须由 checkpoint 驱动")

    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="worker").run(
        Runtime(),
        record=_web_record(),
        settings=Settings(),
    )
    assert calls == ["initialize", "session_close"]
    assert result.page_timeline == ("capture-report",)


@pytest.mark.asyncio
async def test_web_executor_accepts_locked_full_inspection_without_waiting_for_judge() -> None:
    calls: list[str] = []

    class Runtime:
        async def prepare_home(self, command, *, adult_self_attestation):
            del command, adult_self_attestation
            calls.append("home")
            return SimpleNamespace(
                coordination=SimpleNamespace(),
                ready_capture_id="capture-home",
                age_gate_result=None,
                readiness_result=None,
            )

        async def explore(self, command, *, initial_capture_id, initial_page_type=None):
            del command
            assert initial_capture_id == "capture-home"
            assert initial_page_type is None
            calls.append("second-menu")
            summary = _successful_web_summary(executed=1, skipped=0)
            return SimpleNamespace(
                **summary,
                page_timeline=("capture-home", "capture-second-menu"),
                page_type_keys=("content_list",),
                failed_urls=(),
                to_dict=lambda: summary,
            )

        async def close(self, *, task_id, session_id):
            assert (task_id, session_id) == ("task-web", "web:run-web")
            calls.append("close")

    executor = ProductionWebV2RuntimeExecutor(worker_instance_id="web-worker-1")

    result = await executor.run(
        Runtime(),  # type: ignore[arg-type]
        record=_web_record(execution_mode="full_inspection"),
        settings=Settings(),
    )

    assert calls == ["home", "second-menu"]
    assert result.evidence_index_id == "capture-second-menu"


@pytest.mark.asyncio
async def test_web_executor_adds_navbar_pages_to_result_timeline() -> None:
    class Bootstrapper:
        async def start(self, command):
            del command
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-home"))

    class Explorer:
        async def explore(self, command, *, initial_capture_id, initial_page_type=None):
            assert command.session_id == "web:run-web"
            assert initial_capture_id == "capture-home"
            assert initial_page_type is None
            return SimpleNamespace(
                page_timeline=("capture-home", "capture-dashboard", "capture-reports"),
                page_type_keys=(
                    "content_list",
                    "nav-class:dashboard",
                    "nav-class:报表",
                ),
                failed_urls=(),
            )

    class CaptureRuntime:
        async def close_exploration_session(self, *, task_id, session_id):
            del task_id, session_id

    executor = ProductionWebV2RuntimeExecutor(worker_instance_id="web-worker-1")

    result = await executor.run(
        WebV2Runtime(
            bootstrapper=Bootstrapper(),
            capture_runtime=CaptureRuntime(),
            explorer=Explorer(),
        ),
        record=_web_record(),
        settings=Settings(),
    )

    assert result.evidence_index_id == "capture-reports"
    assert result.page_timeline == (
        "capture-home",
        "capture-dashboard",
        "capture-reports",
    )


@pytest.mark.asyncio
async def test_web_executor_marks_partial_when_one_navbar_page_fails() -> None:
    class Bootstrapper:
        async def start(self, command):
            del command
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-home"))

    class Explorer:
        async def explore(self, command, *, initial_capture_id, initial_page_type=None):
            del command, initial_capture_id, initial_page_type
            return SimpleNamespace(
                page_timeline=("capture-home", "capture-dashboard"),
                page_type_keys=("content_list", "nav-class:dashboard"),
                failed_urls=("https://example.test/reports",),
            )

    class CaptureRuntime:
        async def close_exploration_session(self, *, task_id, session_id):
            del task_id, session_id

    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="web-worker-1").run(
        WebV2Runtime(
            bootstrapper=Bootstrapper(),
            capture_runtime=CaptureRuntime(),
            explorer=Explorer(),
        ),
        record=_web_record(),
        settings=Settings(),
    )

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.exploration_status == "partial_evidence"
    assert result.evidence_gap == ("WEB_PAGE_NAVIGATION_PARTIAL",)


@pytest.mark.asyncio
async def test_web_full_inspection_keeps_partial_navigation_as_reportable_gap() -> None:
    """正式 Web 菜单缺口必须交给 Coverage，而不是把主 Run 误报为基础失败。"""

    class Bootstrapper:
        async def start(self, command):
            del command
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-home"))

    class Explorer:
        async def explore(self, command, *, initial_capture_id, initial_page_type=None):
            del command, initial_capture_id, initial_page_type
            return SimpleNamespace(
                page_timeline=("capture-home", "capture-dashboard"),
                page_type_keys=("content_list",),
                failed_urls=("https://example.test/reports",),
            )

    class CaptureRuntime:
        async def close_exploration_session(self, *, task_id, session_id):
            del task_id, session_id

    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="web-worker-1").run(
        WebV2Runtime(
            bootstrapper=Bootstrapper(),
            capture_runtime=CaptureRuntime(),
            explorer=Explorer(),
        ),
        record=_web_record(execution_mode="full_inspection"),
        settings=Settings(),
    )

    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.exploration_status == "partial_evidence"
    assert result.evidence_gap == ("WEB_PAGE_NAVIGATION_PARTIAL",)


@pytest.mark.asyncio
async def test_close_failure_preserves_completed_web_exploration_summary() -> None:
    class Bootstrapper:
        async def start(self, command):
            del command
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-home"))

    class Explorer:
        async def explore(self, command, *, initial_capture_id, initial_page_type=None):
            del command
            assert initial_capture_id == "capture-home"
            assert initial_page_type is None
            return SimpleNamespace(
                page_timeline=("capture-home", "capture-menu-ready"),
                page_type_keys=("content_list",),
                failed_urls=(),
                to_dict=lambda: {
                    "site_id": "example_test",
                    "menu_total": 2,
                    "frontier_remaining": 0,
                    "menu_ledger": [
                        {"entry_id": "home", "status": "home_baseline"},
                        {"entry_id": "menu-1", "status": "menu_page_captured"},
                    ],
                    "status_counts": {
                        "home_baseline": 1,
                        "menu_page_captured": 1,
                    },
                    "page_timeline": ["capture-home", "capture-menu-ready"],
                    "page_type_keys": ["content_list"],
                },
            )

    class CaptureRuntime:
        async def close_exploration_session(self, *, task_id, session_id):
            del task_id, session_id
            raise RuntimeError("close failed")

    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="web-worker-1").run(
        WebV2Runtime(
            bootstrapper=Bootstrapper(),
            capture_runtime=CaptureRuntime(),
            explorer=Explorer(),
        ),
        record=_web_record(),
        settings=Settings(),
    )

    assert result.termination_reason is TerminationReason.ERROR
    assert result.page_timeline == ("capture-home", "capture-menu-ready")
    assert result.evidence_index_id == "capture-menu-ready"
    assert result.web_exploration_summary["menu_total"] == 2
    assert len(result.web_exploration_summary["menu_ledger"]) == 2
    assert result.web_exploration_summary["session_close"] == {
        "status": "failed",
        "error_code": "WEB_SESSION_CLOSE_FAILED",
        "diagnostic_type": "RuntimeError",
    }
    assert result.evidence_gap == ("WEB_SESSION_CLOSE_FAILED",)


@pytest.mark.asyncio
@pytest.mark.parametrize("guide_factory_error", [False, True])
async def test_runtime_composes_age_readiness_catalog_and_full_menu_services(
    guide_factory_error,
) -> None:
    capture_runtime_kwargs: dict[str, object] = {}
    browser = SimpleNamespace()
    persistence = SimpleNamespace()
    command_gate = SimpleNamespace()

    class CaptureRuntime:
        coordinator = SimpleNamespace()
        surface_driver = SimpleNamespace()
        surface_sessions = SimpleNamespace()
        web_desktop_capture_policy = SimpleNamespace()
        trusted_ui_snapshot_reader = SimpleNamespace()
        web_ad_landing_capture_port = SimpleNamespace()
        web_full_page_capture_browser = browser
        web_page_capture_persistence = persistence
        web_guide_capture_persistence = persistence
        session_command_gate = command_gate

        def build_web_guide_browser(self):
            if guide_factory_error:
                raise RuntimeError("optional browser unavailable")
            return browser

    def _capture_runtime_builder(**kwargs):
        capture_runtime_kwargs.update(kwargs)
        return CaptureRuntime()

    provider = PostgresWebRuntimeProvider(
        capture_runtime_builder=_capture_runtime_builder,
        assessment_agent=SimpleNamespace(),
        policy_repository=_PolicyRepository(),
    )
    record = _web_record()
    record.task_config["_task_target"]["target_url"] = "http://91porna.com/"

    runtime = await provider.build(record=record, settings=Settings())

    assert runtime.age_gate_handler is not None
    assert runtime.page_readiness is not None
    if guide_factory_error:
        assert runtime.web_guide_dispatcher is None
    else:
        assert runtime.web_guide_dispatcher is not None
        assert runtime.web_guide_dispatcher._branch._observer is browser
        assert runtime.web_guide_dispatcher._branch._writer._persistence is persistence
    assert isinstance(
        runtime.budget_ledger,
        PostgresWebExplorationBudgetLedgerRepository,
    )
    assert isinstance(
        capture_runtime_kwargs["evidence_exit_gate"],
        NoopEvidenceExitGate,
    )
    assert capture_runtime_kwargs["h5_visual_coordinate_mode"] == "off"
    assert capture_runtime_kwargs["web_desktop_policy_enabled"] is True
    context = runtime.navigation_context.resolve_site_context(
        record.task_config["_task_target"]["target_url"]
    )
    assert context.source_kind == "runtime"
    assert len(context.site.entries) == 1
    assert context.site.origin == "http://91porna.com"
    assert isinstance(
        runtime.page_type_classifier._classifier,
        WebPageTypeAssessmentRecordingClassifier,
    )
    assert runtime.explorer._page_visitor._classifier is runtime.page_type_classifier
    assert (
        runtime.explorer._page_visitor._detail_navigator._classifier is runtime.page_type_classifier
    )
    from services.api.app.application.web_auxiliary_navigation import (
        WebAuxiliaryNavigationExplorer,
    )
    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementalCaptureService,
        WebFullPageSupplementingReadyCaptureReader,
    )
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebAuxiliaryNavigationClaimRepository,
        PostgresWebFullPageSupplementalClaimRepository,
    )

    assert runtime.policy_snapshot is not None
    assert isinstance(runtime.pre_gate_supplemental_capture, WebFullPageSupplementalCaptureService)
    assert runtime.pre_gate_supplemental_capture._browser is browser
    assert runtime.pre_gate_supplemental_capture._persistence is persistence
    assert runtime.pre_gate_supplemental_capture._session_command_gate is command_gate
    assert isinstance(
        runtime.pre_gate_supplemental_capture._claims,
        PostgresWebFullPageSupplementalClaimRepository,
    )
    assert isinstance(runtime.home_ready_capture_reader, WebFullPageSupplementingReadyCaptureReader)
    assert runtime.explorer._page_visitor._ready_capture_reader is runtime.home_ready_capture_reader
    assert (
        runtime.explorer._page_visitor._detail_navigator._ready_capture_reader
        is runtime.home_ready_capture_reader
    )
    assert isinstance(runtime.auxiliary_navigation_explorer, WebAuxiliaryNavigationExplorer)
    assert (
        runtime.auxiliary_navigation_explorer._ready_capture_reader
        is runtime.home_ready_capture_reader
    )
    assert isinstance(
        runtime.auxiliary_navigation_explorer._claims,
        PostgresWebAuxiliaryNavigationClaimRepository,
    )
    evidence_repository = runtime.page_type_classifier._classifier._recorder._repository
    catalog_identity = runtime.page_type_classifier._classifier._recorder._catalog_identity
    assert evidence_repository._catalog_identity is catalog_identity
    assert runtime.age_gate_handler._page_type_recorder._catalog_identity is catalog_identity


@pytest.mark.asyncio
async def test_composition_gates_dom_candidates_through_frozen_frontier_policy() -> None:
    from services.api.app.application.web_runtime_discovery import WebCapture

    events: list[str] = []

    class RawCandidateSource:
        def bind_latest_navigation_capture(self, session_id, capture_id):
            events.append(f"bind:{session_id}:{capture_id}")

        def navigation_candidates(self, session_id):
            events.append(f"dom:{session_id}")
            return tuple(
                WebNavigationCandidate(
                    entry_id=f"news-{index}",
                    label=f"资讯{index}",
                    url=f"https://example.test/news/{index}",
                    menu_path=(f"资讯{index}",),
                    dom_fingerprint=f"href-and-order-sensitive-{index}",
                    source_signal="semantic_nav",
                    page_type_key="unknown",
                    source_capture_id="capture-home",
                    menu_position=index,
                )
                for index in (4, 3, 2, 1)
            )

    raw_source = RawCandidateSource()

    class CaptureRuntime:
        coordinator = SimpleNamespace()
        surface_driver = SimpleNamespace()
        surface_sessions = SimpleNamespace()
        web_desktop_capture_policy = raw_source
        trusted_ui_snapshot_reader = SimpleNamespace()
        web_ad_landing_capture_port = SimpleNamespace()
        web_full_page_capture_browser = SimpleNamespace()
        web_page_capture_persistence = SimpleNamespace()
        session_command_gate = SimpleNamespace()

    class ReadyReader:
        async def read(self, *, capture_id, page_role):
            events.append(f"trusted:{capture_id}:{page_role}")
            return SimpleNamespace(
                capture_id=capture_id,
                url="https://example.test/",
                requested_url="https://example.test/",
                redirect_chain=(),
                finalization_epoch=7,
                capture_state_fingerprint="state-home",
                dom_artifact_id="dom-home",
                page_role="home",
            )

    class MetadataReader:
        async def read(self, *, capture_id, depth):
            events.append(f"metadata:{capture_id}:{depth}")
            return WebCapture(
                capture_id=capture_id,
                session_id="web:run-web",
                finalization_epoch=7,
                requested_url="https://example.test/",
                final_url="https://example.test/",
                current=True,
                ready=True,
                capture_state_fingerprint="state-home",
                depth=depth,
            )

    provider = PostgresWebRuntimeProvider(
        capture_runtime_builder=lambda **_kwargs: CaptureRuntime(),
        assessment_agent=SimpleNamespace(),
        policy_repository=_PolicyRepository(),
        frontier_capture_metadata_reader=MetadataReader(),
        frontier_ready_capture_reader=ReadyReader(),
    )
    record = _web_record()
    record.task_config["_task_target"]["target_url"] = "https://example.test/"
    runtime = await provider.build(record=record, settings=Settings())
    source = runtime.navigation_candidate_source
    source.bind_latest_navigation_capture("web:run-web", "capture-home")

    candidates = await source.navigation_candidates("web:run-web")

    assert events == [
        "bind:web:run-web:capture-home",
        "metadata:capture-home:0",
        "trusted:capture-home:home",
        "dom:web:run-web",
    ]
    assert [candidate.url for candidate in candidates] == [
        "https://example.test/news/4",
        "https://example.test/news/3",
        "https://example.test/news/2",
    ]
    assert candidates[0].frontier_route_shape == "/news/:id"
    assert candidates[0].frontier_structure_cluster_key.startswith("web-frontier-structure.v1:")
    assert runtime.explorer._candidate_source is source
    assert runtime.explorer._page_visitor._navigation_candidate_source is source


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_state", ["cross_origin", "non_current"])
async def test_default_composition_rejects_untrusted_metadata_before_authorized_discovery(
    monkeypatch: pytest.MonkeyPatch,
    capture_state: str,
) -> None:
    from services.api.app.application.web_runtime_discovery import (
        UntrustedWebCaptureError,
    )
    from services.api.app.infrastructure.browser import web_desktop_capture_policy as policy_module
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureArtifact,
        PageCaptureMode,
        PageCaptureRequest,
        PageCaptureResult,
    )
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        WebCaptureArtifactIndex,
    )
    from services.api.tests.test_web_capture_readiness_metrics import (
        _stable_capture_metrics,
    )

    calls = Counter()

    class CaptureIndex:
        async def matches_active_capture(self, **kwargs):
            calls["metadata_identity"] += 1
            return not (capture_state == "non_current" and kwargs["current_only"] is True)

        async def read(self, **kwargs):
            calls["metadata"] += 1
            return WebCaptureArtifactIndex(
                capture_id=kwargs["capture_id"],
                url=(
                    "https://outside.test/landing"
                    if capture_state == "cross_origin"
                    else "https://example.test/menu"
                ),
                artifact_ids={
                    "screenshot": "shot",
                    "dom": "dom",
                    "visible_text": "text",
                },
                captured_at=datetime.now(UTC),
                pipeline_metrics={
                    "web_readiness": _stable_capture_metrics(page_settle_complete=True)
                },
                requested_url="https://example.test/menu",
                redirect_chain=(
                    (
                        "https://example.test/menu",
                        "https://outside.test/landing",
                    )
                    if capture_state == "cross_origin"
                    else ()
                ),
                finalization_epoch=3,
            )

    class SnapshotReader:
        async def read_ui_snapshot(self, **_kwargs):
            calls["snapshot"] += 1
            raise AssertionError("跨域 metadata 被拒绝前不得读取 UI Snapshot")

    class Browser:
        async def capture(self, request):
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url=(
                    "https://outside.test/landing"
                    if capture_state == "cross_origin"
                    else "https://example.test/menu"
                ),
                title="outside",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact(
                        artifact_type="dom",
                        artifact_id=None,
                        content='<a href="https://example.test/back">back</a>',
                    ),
                    PageCaptureArtifact(
                        artifact_type="ui_snapshot",
                        artifact_id=None,
                        content={"schema_version": "ui-snapshot.v2", "elements": []},
                    ),
                ],
            )

    raw_policy = WebDesktopCapturePolicyBrowser(browser=Browser())
    original_candidates = raw_policy.navigation_candidates

    def observe_cache(session_id):
        calls["candidate_cache"] += 1
        return original_candidates(session_id)

    monkeypatch.setattr(raw_policy, "navigation_candidates", observe_cache)
    original_extract = policy_module.extract_navigation_candidates

    def observe_extract(*args, **kwargs):
        calls["dom_extract"] += 1
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(policy_module, "extract_navigation_candidates", observe_extract)
    capture_runtime = SimpleNamespace(
        coordinator=SimpleNamespace(),
        surface_driver=SimpleNamespace(),
        surface_sessions=SimpleNamespace(),
        web_desktop_capture_policy=raw_policy,
        trusted_ui_snapshot_reader=SnapshotReader(),
        web_ad_landing_capture_port=SimpleNamespace(),
        web_full_page_capture_browser=SimpleNamespace(),
        web_page_capture_persistence=SimpleNamespace(),
        session_command_gate=SimpleNamespace(),
    )
    monkeypatch.setattr(
        web_runtime_module,
        "PostgresWebCaptureEvidenceRepository",
        lambda **_kwargs: CaptureIndex(),
    )
    record = _web_record()
    record.task_config["_task_target"]["target_url"] = "https://example.test/"
    runtime = await PostgresWebRuntimeProvider(
        capture_runtime_builder=lambda **_kwargs: capture_runtime,
        assessment_agent=SimpleNamespace(),
        policy_repository=_PolicyRepository(),
    ).build(record=record, settings=Settings())
    await raw_policy.capture(
        PageCaptureRequest(
            mode=PageCaptureMode.GOTO,
            url="https://example.test/menu",
            session_id="web:run-web",
        )
    )
    source = runtime.navigation_candidate_source
    source.bind_latest_navigation_capture("web:run-web", "capture-cross-origin")
    # 密封前只解析观察事实；未经 authority 放行，不得发布可执行候选。
    extraction_before_authorization = calls["dom_extract"]
    assert original_candidates("web:run-web") == ()

    with pytest.raises(
        UntrustedWebCaptureError,
        match=(
            "WEB_CANONICAL_SITE_SCOPE_INVALID|WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED"
            "|WEB_DISCOVERY_SOURCE_NOT_CURRENT"
        ),
    ):
        await source.navigation_candidates("web:run-web")

    assert calls["metadata_identity"] >= 1
    assert calls["metadata"] == 1
    assert calls["snapshot"] == 0
    assert calls["dom_extract"] == extraction_before_authorization
    assert calls["candidate_cache"] == 0


@pytest.mark.asyncio
async def test_new_empty_capture_discards_older_deferred_cross_origin_dom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.application.web_runtime_discovery import WebCapture
    from services.api.app.infrastructure.browser import web_desktop_capture_policy as policy_module
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureArtifact,
        PageCaptureMode,
        PageCaptureRequest,
        PageCaptureResult,
    )
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    extraction_count = 0
    original_extract = policy_module.extract_navigation_candidates

    def observe_extract(*args, **kwargs):
        nonlocal extraction_count
        extraction_count += 1
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(policy_module, "extract_navigation_candidates", observe_extract)

    class Browser:
        calls = 0

        async def capture(self, request):
            self.calls += 1
            if self.calls == 1:
                return PageCaptureResult(
                    execution_status="success",
                    mode=request.mode.value,
                    reachable=True,
                    final_url="https://outside.test/landing",
                    title="outside",
                    status_code=200,
                    artifacts=[
                        PageCaptureArtifact(
                            artifact_type="dom",
                            artifact_id=None,
                            content=('<nav><a href="https://example.test/leaked">leaked</a></nav>'),
                        ),
                        PageCaptureArtifact(
                            artifact_type="ui_snapshot",
                            artifact_id=None,
                            content={
                                "schema_version": "ui-snapshot.v2",
                                "elements": [
                                    {
                                        "tag": "a",
                                        "text": "leaked",
                                        "href": "https://example.test/leaked",
                                        "interactive": True,
                                        "visible": True,
                                        "hit_test": {"center_hits_target": True},
                                    }
                                ],
                            },
                        ),
                    ],
                )
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://example.test/safe",
                title="safe",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact(
                        artifact_type="dom",
                        artifact_id=None,
                        content="",
                    ),
                    PageCaptureArtifact(
                        artifact_type="ui_snapshot",
                        artifact_id=None,
                        content={"schema_version": "ui-snapshot.v2", "elements": []},
                    ),
                ],
            )

    class MetadataReader:
        async def read(self, *, capture_id, depth):
            return WebCapture(
                capture_id=capture_id,
                session_id="web:run-web",
                finalization_epoch=7,
                requested_url="https://example.test/safe",
                final_url="https://example.test/safe",
                current=True,
                ready=True,
                capture_state_fingerprint="state-safe",
                depth=depth,
            )

    class ReadyReader:
        async def read(self, *, capture_id, page_role):
            return SimpleNamespace(capture_id=capture_id, page_role=page_role)

    raw_policy = WebDesktopCapturePolicyBrowser(browser=Browser())
    capture_runtime = SimpleNamespace(
        coordinator=SimpleNamespace(),
        surface_driver=SimpleNamespace(),
        surface_sessions=SimpleNamespace(),
        web_desktop_capture_policy=raw_policy,
        trusted_ui_snapshot_reader=SimpleNamespace(),
        web_ad_landing_capture_port=SimpleNamespace(),
        web_full_page_capture_browser=SimpleNamespace(),
        web_page_capture_persistence=SimpleNamespace(),
        session_command_gate=SimpleNamespace(),
    )
    record = _web_record()
    record.task_config["_task_target"]["target_url"] = "https://example.test/"
    runtime = await PostgresWebRuntimeProvider(
        capture_runtime_builder=lambda **_kwargs: capture_runtime,
        assessment_agent=SimpleNamespace(),
        policy_repository=_PolicyRepository(),
        frontier_capture_metadata_reader=MetadataReader(),
        frontier_ready_capture_reader=ReadyReader(),
    ).build(record=record, settings=Settings())
    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO,
        url="https://example.test/",
        session_id="web:run-web",
    )
    await raw_policy.capture(request)
    await raw_policy.capture(request)
    source = runtime.navigation_candidate_source
    source.bind_latest_navigation_capture("web:run-web", "capture-safe")
    extraction_before_authorization = extraction_count

    candidates = await source.navigation_candidates("web:run-web")

    assert candidates == ()
    assert extraction_count == extraction_before_authorization


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
async def test_default_assessor_builds_with_every_web_gate_mode(monkeypatch, mode):
    """默认评估器必须真实构建，不能用注入评估器掩盖后置导入错误。"""
    from services.api.app.agents.explorer import web_assessment_agent as agent_module

    class Agent:
        def __init__(self, **kwargs):
            self.instructions = kwargs["instructions"]

    monkeypatch.setattr(agent_module, "Agent", Agent)
    monkeypatch.setattr(web_runtime_module, "build_chat_client", lambda *args, **kwargs: object())
    capture = SimpleNamespace(
        coordinator=SimpleNamespace(),
        surface_driver=SimpleNamespace(),
        surface_sessions=SimpleNamespace(),
        web_desktop_capture_policy=SimpleNamespace(),
        trusted_ui_snapshot_reader=SimpleNamespace(),
        web_ad_landing_capture_port=SimpleNamespace(),
        web_full_page_capture_browser=SimpleNamespace(),
        web_page_capture_persistence=SimpleNamespace(),
        session_command_gate=SimpleNamespace(),
    )
    provider = PostgresWebRuntimeProvider(
        capture_runtime_builder=lambda **kwargs: capture,
        policy_repository=_PolicyRepository(),
    )
    record = _web_record()
    record.task_config["_task_target"]["target_url"] = "http://91porna.com/"
    settings = Settings()
    settings.web_evidence_exit_gate.mode = mode
    runtime = await provider.build(record=record, settings=settings)
    assert isinstance(
        runtime.age_gate_handler._assessor, agent_module.WebExplorationAssessmentAgent
    )
    assert runtime.age_gate_handler._assessor.framework_agent.instructions
    assert runtime.page_readiness._assessor is runtime.age_gate_handler._assessor


@pytest.mark.asyncio
async def test_formal_session_closer_rebuilds_explicit_web_desktop_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重启后的正式 Session closer 也必须保持 Web 专属组合参数。"""
    capture_runtime_kwargs: dict[str, object] = {}
    closed: list[tuple[str, str]] = []

    class Runtime:
        async def close_session(self, *, task_id: str, session_id: str) -> None:
            closed.append((task_id, session_id))

    def build_runtime(**kwargs):
        capture_runtime_kwargs.update(kwargs)
        return Runtime()

    monkeypatch.setattr(
        web_runtime_module,
        "build_default_web_capture_runtime",
        build_runtime,
    )
    closer = web_runtime_module.build_web_formal_session_closer(Settings())

    await closer.close(task_id="task-web", session_id="web:run-web")

    assert closed == [("task-web", "web:run-web")]
    assert capture_runtime_kwargs["session_owner"] == "web-v2-workflow"
    assert capture_runtime_kwargs["include_h5_exploration"] is False
    assert capture_runtime_kwargs["web_desktop_policy_enabled"] is True


@pytest.mark.asyncio
async def test_notifying_classifier_runs_after_bridge_without_waiting_for_judge() -> None:
    events: list[str] = []

    class BridgeClassifier:
        async def classify(self, capture):
            del capture
            events.append("bridge-committed")
            return SimpleNamespace(page_type="content_list")

    class Notification:
        async def notify_ready_capture(self, **kwargs):
            assert kwargs == {
                "task_id": "task-web",
                "agent_run_id": "run-web",
                "session_id": "web:run-web",
                "capture_id": "capture-menu",
            }
            events.append("publication-wakeup-returned")
            return SimpleNamespace(status="published")

    classifier = WebPageInspectionNotifyingClassifier(
        classifier=BridgeClassifier(),
        notification=Notification(),
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
    )

    assessment = await classifier.classify(SimpleNamespace(capture_id="capture-menu"))
    events.append("second-menu-navigation")

    assert assessment.page_type == "content_list"
    assert events == [
        "bridge-committed",
        "publication-wakeup-returned",
        "second-menu-navigation",
    ]


@pytest.mark.asyncio
async def test_notifying_classifier_finalizes_unrecognized_capture_without_publishing() -> None:
    events: list[str] = []

    class BridgeClassifier:
        async def classify(self, capture):
            del capture
            events.append("review-assessment-committed")
            return SimpleNamespace(
                page_type=None,
                needs_review_reason="WEB_PAGE_TYPE_LOW_CONFIDENCE",
            )

    class Notification:
        async def notify_ready_capture(self, **kwargs):
            assert kwargs["capture_id"] == "capture-review"
            events.append("manifest-finalization-attempted")
            return SimpleNamespace(
                status="blocked",
                reason_code="WEB_PAGE_TYPE_ASSESSMENT_NOT_RECOGNIZED",
            )

    classifier = WebPageInspectionNotifyingClassifier(
        classifier=BridgeClassifier(),
        notification=Notification(),  # type: ignore[arg-type]
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
    )

    assessment = await classifier.classify(SimpleNamespace(capture_id="capture-review"))

    assert assessment.page_type is None
    assert events == [
        "review-assessment-committed",
        "manifest-finalization-attempted",
    ]


@pytest.mark.asyncio
async def test_provider_adds_incremental_notification_only_for_formal_web_mode(monkeypatch) -> None:
    from unittest.mock import AsyncMock, call

    from services.api.app.infrastructure.db.web_popup_blocked_repository import (
        PostgresWebPopupBlockedRepository,
    )

    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)
    popup_record = AsyncMock(return_value=True)
    monkeypatch.setattr(PostgresWebPopupBlockedRepository, "record", popup_record)
    class CaptureRuntime:
        coordinator = SimpleNamespace()
        surface_driver = SimpleNamespace()
        surface_sessions = SimpleNamespace()
        web_desktop_capture_policy = SimpleNamespace()
        trusted_ui_snapshot_reader = SimpleNamespace()
        web_ad_landing_capture_port = SimpleNamespace()
        web_full_page_capture_browser = SimpleNamespace()
        web_page_capture_persistence = SimpleNamespace()
        session_command_gate = SimpleNamespace()

    class LocalMenuRegistry:
        def resolve_site(self, target_url: str) -> object:
            assert target_url == "https://example.test/dashboard"
            return SimpleNamespace(site_id="example-test")

    class FormalSessionCloser:
        def __init__(self) -> None:
            self.bound_runtimes: list[object] = []

        def bind_live_runtime(self, *, session_id: str, runtime: object) -> None:
            assert session_id == "web:run-web"
            self.bound_runtimes.append(runtime)

    notification = SimpleNamespace(notify_ready_capture=lambda **_kwargs: None)
    formal_session_closer = FormalSessionCloser()
    provider = PostgresWebRuntimeProvider(
        capture_runtime_builder=lambda **_kwargs: CaptureRuntime(),
        assessment_agent=SimpleNamespace(),
        policy_repository=_PolicyRepository(),
        page_inspection_notification=notification,
        formal_session_closer=formal_session_closer,  # type: ignore[arg-type]
    )

    formal_record = _web_record(execution_mode="full_inspection")
    exploration_record = _web_record()
    for record in (formal_record, exploration_record):
        record.task_config["_task_target"]["target_url"] = "http://91porna.com/"
    formal = await provider.build(
        record=formal_record,
        settings=Settings(),
    )
    exploration = await provider.build(record=exploration_record, settings=Settings())

    assert formal.stage_orchestrator is not None
    assert exploration.stage_orchestrator is not None
    assert callable(formal.stage_orchestrator.initialize)
    assert callable(exploration.stage_orchestrator.advance_once)

    assert isinstance(formal.page_type_classifier._classifier, WebPageInspectionNotifyingClassifier)
    assert formal.explorer._page_visitor._classifier is formal.page_type_classifier
    assert (
        formal.explorer._page_visitor._detail_navigator._classifier is formal.page_type_classifier
    )
    assert isinstance(
        exploration.page_type_classifier._classifier,
        WebPageTypeAssessmentRecordingClassifier,
    )
    assert formal_session_closer.bound_runtimes == [formal.capture_runtime]
    assert formal.page_readiness._failure_sequence_notifier is not None
    assert exploration.page_readiness._failure_sequence_notifier is None
    assert formal.page_readiness._interaction_sequence_notifier is not None
    assert exploration.page_readiness._interaction_sequence_notifier is None
    for runtime in (formal, exploration):
        await runtime.page_readiness._popup_blocked_recorder(
            capture_id="blocked-capture", blocking_reasons=("popup_blocked",),
        )
    expected = call(
        task_id=formal_record.task_id, agent_run_id=formal_record.run_id,
        session_id=f"web:{formal_record.run_id}", capture_id="blocked-capture",
        blocking_reasons=("popup_blocked",),
    )
    assert popup_record.await_args_list == [expected, expected]


@pytest.mark.asyncio
async def test_popup_blocked_recorder_does_not_fail_run_when_evidence_is_stale(caplog) -> None:
    """弹窗阻断只是诊断标记：围栏失效时返回 False 并记下原因，不再让整条 Web Run 失败。"""
    import logging

    from services.api.app.application.web_runtime_composition import (
        _record_popup_blocked_best_effort,
    )
    from services.api.app.infrastructure.db.web_popup_blocked_repository import (
        WebPopupBlockedPersistenceError,
    )

    class StaleRepository:
        async def record(self, **kwargs):
            raise WebPopupBlockedPersistenceError(
                "Web 弹窗阻断证据已失效，拒绝写入（session_draining）",
                reason_code="session_draining",
            )

    with caplog.at_level(logging.WARNING):
        recorded = await _record_popup_blocked_best_effort(
            StaleRepository(), task_id="task-1", agent_run_id="run-1",
            session_id="web:run-1", capture_id="capture-1", blocking_reasons=("popup_blocked",),
        )
    assert recorded is False
    assert "reason=session_draining" in caplog.text
    assert "capture_id=capture-1" in caplog.text


@pytest.mark.asyncio
async def test_executor_reports_menu_terminal_counts_not_page_type_coverage() -> None:
    class Bootstrapper:
        async def start(self, command):
            del command
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-home"))

    class Explorer:
        async def explore(self, command, *, initial_capture_id, initial_page_type=None):
            del command, initial_capture_id, initial_page_type
            return SimpleNamespace(
                site_id="91porna_com",
                catalog_version="2026-08-30.web-menu.v1",
                menu_total=65,
                frontier_remaining=0,
                executed_same_origin_count=62,
                status_counts={
                    "home_baseline": 1,
                    "menu_page_captured": 62,
                    "cross_origin_blocked": 2,
                },
                menu_ledger=(),
                page_timeline=("capture-home", "capture-last"),
                page_type_keys=("content_list", "video_list"),
                failed_urls=(),
                to_dict=lambda: {
                    "site_id": "91porna_com",
                    "menu_total": 65,
                    "frontier_remaining": 0,
                    "status_counts": {
                        "home_baseline": 1,
                        "menu_page_captured": 62,
                        "cross_origin_blocked": 2,
                    },
                },
            )

    class CaptureRuntime:
        async def close_exploration_session(self, *, task_id, session_id):
            del task_id, session_id

    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="web-worker-1").run(
        WebV2Runtime(
            bootstrapper=Bootstrapper(),
            capture_runtime=CaptureRuntime(),
            explorer=Explorer(),
        ),
        record=_web_record(),
        settings=Settings(),
    )

    assert result.web_exploration_summary["menu_total"] == 65
    assert result.web_exploration_summary["frontier_remaining"] == 0
    assert "65 个菜单终态" in result.final_text


@pytest.mark.asyncio
async def test_menu_visitor_schedules_ad_evidence_before_unclassified_terminal() -> None:
    """已就绪菜单即使未定型也必须先完成广告证据尝试。"""

    class Navigator:
        async def visit(self, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id="capture-menu-probe",
                page_type=None,
                menu_action_count=1,
            )

    class Readiness:
        async def prepare(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-menu-ready",
                evidence_capture_ids=("capture-popup", "capture-menu-ready"),
                dismissed_layers=("popup",),
            )

    class ReadyReader:
        async def read(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                capture_id="capture-menu-ready",
                url="https://example.test/menu",
                interaction_facts=None,
            )

    class Classifier:
        async def classify(self, capture):
            del capture
            return SimpleNamespace(
                page_type=None,
                raw_page_type="needs_review",
                proposed_page_type="content_list",
                needs_review_reason="WEB_PAGE_TYPE_LOW_CONFIDENCE",
                diagnostic_message="待复核",
            )

    class EvidenceSource:
        async def read_snapshot(self, *, capture_id):
            return {
                "elements": [
                    {
                        "href": "https://ads.example.net/landing",
                        "class_name": (
                            "popup-ad" if capture_id == "capture-popup" else "banner-ad"
                        ),
                    }
                ]
            }

    class Scheduler:
        def __init__(self) -> None:
            self.pages = []

        async def attempt(self, page):
            self.pages.append(page)
            return SimpleNamespace(
                to_dict=lambda: {
                    "status": "observed",
                    "logical_page_id": page.logical_page_id,
                }
            )

    scheduler = Scheduler()
    result = await WebOrchestratedMenuPageVisitor(
        navigator=Navigator(),
        readiness=Readiness(),
        ready_capture_reader=ReadyReader(),
        classifier=Classifier(),
        evidence_source=EvidenceSource(),
        detail_navigator=SimpleNamespace(),
        ad_landing_scheduler=scheduler,
        ad_identity_policy=SimpleNamespace(logical_id=lambda url: "logical-menu:" + url),
        ad_registered_domain="example.test",
    ).visit(
        command=SimpleNamespace(
            task_id="task-web",
            agent_run_id="run-web",
            session_id="web:run-web",
        ),
        entry=SimpleNamespace(entry_id="entry-menu", classification_hint=None),
    )

    assert result.status is WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
    assert result.ad_landing_evidence["status"] == "observed"
    assert len(scheduler.pages) == 1
    assert scheduler.pages[0].page_type is None


@pytest.mark.asyncio
async def test_home_preparation_schedules_ad_before_unclassified_result() -> None:
    class Bootstrapper:
        async def start(self, command):
            del command
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-home-entry"))

    class AgeReader:
        async def read(self, *, capture_id):
            return SimpleNamespace(capture_id=capture_id)

    class AgeHandler:
        async def handle(self, evidence, *, adult_self_attestation):
            del evidence, adult_self_attestation
            return SimpleNamespace(after_capture_id=None, gate_disappeared=False)

    class Readiness:
        async def prepare(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                status="ready",
                ready_capture_id="capture-home-ready",
                evidence_capture_ids=("capture-home-popup", "capture-home-ready"),
                dismissed_layers=("popup",),
            )

    class ReadyReader:
        async def read(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                capture_id="capture-home-ready",
                url="https://example.test/",
                structure_facts=None,
            )

    class Classifier:
        async def classify(self, capture):
            del capture
            return SimpleNamespace(
                page_type=None,
                raw_page_type="home",
                needs_review_reason="WEB_PAGE_TYPE_LOW_CONFIDENCE",
            )

    class SnapshotReader:
        async def read_snapshot(self, *, capture_id):
            return {
                "elements": [
                    {
                        "href": "https://ads.example.net/landing",
                        "class_name": (
                            "popup-ad" if capture_id == "capture-home-popup" else "banner-ad"
                        ),
                    }
                ]
            }

    class Scheduler:
        def __init__(self) -> None:
            self.pages = []

        async def attempt(self, page):
            self.pages.append(page)
            return SimpleNamespace(
                to_dict=lambda: {
                    "status": "observed",
                    "logical_page_id": page.logical_page_id,
                }
            )

    scheduler = Scheduler()
    runtime = WebV2Runtime(
        bootstrapper=Bootstrapper(),
        capture_runtime=SimpleNamespace(),
        age_gate_handler=AgeHandler(),
        age_evidence_reader=AgeReader(),
        page_readiness=Readiness(),
        home_ready_capture_reader=ReadyReader(),
        page_type_classifier=Classifier(),
        ad_landing_scheduler=scheduler,
        ad_snapshot_reader=SnapshotReader(),
        ad_identity_policy=SimpleNamespace(logical_id=lambda url: "logical-home:" + url),
        ad_registered_domain="example.test",
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-v2-workflow",
        target_url="https://example.test/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC),
    )

    result = await runtime.prepare_home(
        command,
        adult_self_attestation=SimpleNamespace(),
    )

    assert result.page_type is None
    assert result.ad_landing_evidence["status"] == "observed"
    assert len(scheduler.pages) == 1
    assert scheduler.pages[0].page_role == "home"
