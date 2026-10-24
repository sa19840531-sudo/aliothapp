"""实际已知详情 visitor 分支必须把补图审计传到终态持久化载荷。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.api.app.application.web_interaction_precision import WebInteractionOutcome
from services.api.app.application.web_menu_exploration import WebMenuVisitResult, _terminal_record
from services.api.app.application.web_primary_content_capture import WebPrimaryContentCaptureResult
from services.api.app.application.web_runtime_composition import WebOrchestratedMenuPageVisitor
from services.api.app.domain.exploration import WebMenuTerminalStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["captured", "blocked", "exception", "unready"])
async def test_known_detail_primary_diagnostic_reaches_terminal_payload(status):
    bound = []

    class Navigator:
        async def visit(self, **kwargs):
            return WebMenuVisitResult(
                final_capture_id="probe",
                page_type=None,
                menu_action_count=1,
                menu_intent_id="intent",
                menu_action_execution_id="menu-action",
            )

    class Readiness:
        async def prepare(self, **kwargs):
            return SimpleNamespace(
                status="ready",
                ready_capture_id="baseline",
                evidence_capture_ids=("probe", "baseline"),
                dismissed_layers=(),
            )

    class Reader:
        async def read_current(self, *, page_role):
            if status == "unready":
                raise RuntimeError("WEB_CAPTURE_NOT_FORMAL_EVIDENCE")
            return SimpleNamespace(capture_id="actual-current")

        async def read(self, **kwargs):
            return SimpleNamespace(capture_id="baseline", url="https://example.test/article")

    class Classifier:
        async def classify(self, capture):
            return SimpleNamespace(page_type="content_detail")

    class Primary:
        async def capture(self, **kwargs):
            if status == "exception":
                raise RuntimeError("不得把异常正文写到报告")
            return WebPrimaryContentCaptureResult(
                status="blocked" if status == "unready" else status,
                parent_capture_id="baseline",
                evidence_capture_id="primary" if status == "captured" else None,
                logical_page_id="web-logical:" + "1" * 64,
                target_candidate_id="target",
                scroll_count=1,
                before_ad_coverage=0.9,
                after_ad_coverage=0.1,
                reason_code=None if status == "captured" else "WEB_PRIMARY_CONTENT_AFTER_UNTRUSTED",
                interaction_outcomes=(
                    WebInteractionOutcome(
                        command_accepted=True,
                        effect_observed=True,
                        postcondition_satisfied=status == "captured",
                        transition_arrived=status == "captured",
                        after_capture_id="primary",
                        failure_reason=None if status == "captured" else "after_untrusted",
                    ),
                ),
                attempts=(
                    {
                        "attempt": 1,
                        "source_capture_id": "baseline",
                        "before_capture_id": "fresh-before",
                        "after_capture_id": "primary",
                        "action_execution_id": "scroll-action",
                    },
                ),
            )

    command = SimpleNamespace(
        task_id="task", agent_run_id="run", session_id="web:run", deadline=datetime.now(UTC)
    )
    entry = SimpleNamespace(entry_id="article", classification_hint=None)
    result = await WebOrchestratedMenuPageVisitor(
        navigator=Navigator(),
        readiness=Readiness(),
        ready_capture_reader=Reader(),
        classifier=Classifier(),
        evidence_source=object(),
        detail_navigator=object(),
        primary_content_capture=Primary(),
        navigation_candidate_source=SimpleNamespace(
            bind_latest_navigation_capture=lambda session, capture: bound.append(capture)
        ),
    ).visit(
        command=command,
        entry=entry,
        candidate=SimpleNamespace(),
        source_capture_id="home",
        menu_intent_id="intent",
    )
    assert result.status is WebMenuTerminalStatus.DETAIL_CAPTURED
    assert result.menu_ready_capture_id == "baseline"
    if status == "unready":
        assert result.navigation_current_capture_id is None
        assert result.navigation_handoff_error == "WEB_NAVIGATION_CURRENT_UNTRUSTED"
    else:
        assert result.navigation_current_capture_id == "actual-current"
        assert bound[-1] == "actual-current"
    assert len(result.primary_content_diagnostics) == 1
    diagnostic = result.primary_content_diagnostics[0]
    if status == "exception":
        assert diagnostic["status"] == "failed"
        assert diagnostic["diagnostic_type"] == "RuntimeError"
    else:
        assert diagnostic["status"] == ("blocked" if status == "unready" else status)
        assert diagnostic["attempts"][0]["action_execution_id"] == "scroll-action"
        assert diagnostic["interaction_outcomes"][0]["after_capture_id"] == "primary"
    terminal = _terminal_record(command=command, site_id="site", entry=entry, result=result)
    assert terminal.diagnostic["primary_content_diagnostics"] == list(
        result.primary_content_diagnostics
    )
    assert "不得把异常正文" not in str(terminal.diagnostic)


@pytest.mark.asyncio
async def test_human_verification_does_not_require_navigation_handoff():
    from unittest.mock import AsyncMock

    blocked = WebMenuVisitResult(
        final_capture_id="blocked-probe",
        page_type=None,
        status=WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
        error_code="WEB_MENU_HUMAN_VERIFICATION_BLOCKED",
    )
    reader = SimpleNamespace(read_current=AsyncMock(side_effect=RuntimeError("not-ready")))
    visitor = WebOrchestratedMenuPageVisitor(
        navigator=object(),
        readiness=object(),
        ready_capture_reader=reader,
        classifier=object(),
        evidence_source=object(),
        detail_navigator=object(),
        primary_content_capture=object(),
    )
    visitor._visit = AsyncMock(return_value=blocked)
    result = await visitor.visit(command=SimpleNamespace(session_id="web:run"))
    assert result is blocked
    reader.read_current.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_detail_scroll_rebaselines_current_page_before_menu_handoff():
    from unittest.mock import AsyncMock

    failed_scroll = WebInteractionOutcome(
        command_accepted=True,
        effect_observed=True,
        postcondition_satisfied=False,
        transition_arrived=False,
        after_capture_id="failed-scroll-after",
        failure_reason="postcondition_not_satisfied",
    )
    result = WebMenuVisitResult(
        final_capture_id="menu-ready",
        page_type="video_list",
        status=WebMenuTerminalStatus.NAVIGATION_FAILED,
        error_code="WEB_DETAIL_TARGET_BINDING_FAILED",
        detail_scroll_interaction_outcomes=(failed_scroll,),
    )

    class Reader:
        def __init__(self) -> None:
            self.current_reads = 0

        async def read_current(self, *, page_role):
            del page_role
            self.current_reads += 1
            if self.current_reads == 1:
                raise RuntimeError("WEB_CAPTURE_NOT_FORMAL_EVIDENCE")
            return SimpleNamespace(capture_id="recovered-ready")

        async def read(self, *, capture_id, page_role):
            del page_role
            return SimpleNamespace(capture_id=capture_id)

    readiness = SimpleNamespace(
        prepare=AsyncMock(
            return_value=SimpleNamespace(
                status="ready",
                ready_capture_id="recovered-ready",
                evidence_capture_ids=("failed-scroll-after", "recovered-ready"),
                dismissed_layers=(),
            )
        )
    )
    reader = Reader()
    visitor = WebOrchestratedMenuPageVisitor(
        navigator=object(),
        readiness=readiness,
        ready_capture_reader=reader,
        classifier=object(),
        evidence_source=object(),
        detail_navigator=object(),
        primary_content_capture=object(),
        navigation_candidate_source=SimpleNamespace(
            bind_latest_navigation_capture=lambda session, capture: None
        ),
    )
    visitor._visit = AsyncMock(return_value=result)

    recovered = await visitor.visit(command=SimpleNamespace(session_id="web:run"))

    assert recovered.navigation_handoff_error is None
    assert recovered.navigation_current_capture_id == "recovered-ready"
    readiness.prepare.assert_awaited_once_with(
        source_capture_id="failed-scroll-after",
        page_role="menu",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [True, False])
async def test_known_list_detail_primary_hands_off_actual_current_without_reclassification(
    selected,
):
    from unittest.mock import AsyncMock

    from services.api.app.application.web_list_detail_navigation import WebListDetailResult

    current = {"id": "list-baseline"}
    bindings = []
    list_sources = []

    async def read(*, capture_id, page_role):
        return SimpleNamespace(capture_id=capture_id, url="https://example.test/" + capture_id)

    async def read_current(*, page_role):
        return await read(capture_id=current["id"], page_role=page_role)

    async def capture(**kwargs):
        current["id"] = kwargs["capture_id"] + ":scroll"
        return WebPrimaryContentCaptureResult(
            status="captured" if selected else "blocked",
            parent_capture_id=kwargs["capture_id"],
            evidence_capture_id=current["id"] if selected else None,
            logical_page_id=(
                "web-logical:" + ("1" if kwargs["capture_id"] == "list-baseline" else "2") * 64
            ),
            target_candidate_id=f"target-{kwargs['capture_id']}",
            scroll_count=1,
            before_ad_coverage=0.9,
            after_ad_coverage=0.1 if selected else 0.9,
            reason_code=None if selected else "WEB_PRIMARY_CONTENT_AD_COVERAGE_NOT_IMPROVED",
        )

    async def read_list(*, capture_id):
        list_sources.append(capture_id)
        return object()

    async def detail(*args, **kwargs):
        current["id"] = "detail-baseline"
        return WebListDetailResult(
            status=WebMenuTerminalStatus.DETAIL_CAPTURED,
            detail_capture_id=current["id"],
            page_type="content_detail",
        )

    classifier = SimpleNamespace(
        classify=AsyncMock(return_value=SimpleNamespace(page_type="content_list"))
    )
    primary = SimpleNamespace(capture=AsyncMock(side_effect=capture))
    player_visual = SimpleNamespace(assess=AsyncMock())
    visitor = WebOrchestratedMenuPageVisitor(
        navigator=SimpleNamespace(
            visit=AsyncMock(
                return_value=WebMenuVisitResult(final_capture_id="probe", page_type=None)
            )
        ),
        readiness=SimpleNamespace(
            prepare=AsyncMock(
                return_value=SimpleNamespace(
                    status="ready",
                    ready_capture_id="list-baseline",
                    evidence_capture_ids=("probe", "list-baseline"),
                    dismissed_layers=(),
                )
            )
        ),
        ready_capture_reader=SimpleNamespace(read=read, read_current=read_current),
        classifier=classifier,
        evidence_source=SimpleNamespace(read_list=read_list),
        detail_navigator=SimpleNamespace(capture_one=detail),
        primary_content_capture=primary,
        player_visual_recheck=player_visual,
        navigation_candidate_source=SimpleNamespace(
            bind_latest_navigation_capture=lambda session, capture: bindings.append(capture)
        ),
    )
    result = await visitor.visit(
        command=SimpleNamespace(
            task_id="task", agent_run_id="run", session_id="web:run", deadline=datetime.now(UTC)
        ),
        entry=SimpleNamespace(entry_id="menu"),
    )
    assert result.status is WebMenuTerminalStatus.DETAIL_CAPTURED
    assert result.menu_ready_capture_id == "list-baseline"
    assert result.detail_ready_capture_id == "detail-baseline"
    assert result.navigation_current_capture_id == "detail-baseline:scroll"
    assert result.navigation_handoff_error is None
    assert list_sources == ["list-baseline:scroll"]
    assert bindings[-1] == "detail-baseline:scroll"
    assert classifier.classify.await_count == 1
    assert primary.capture.await_count == 2
    player_visual.assess.assert_awaited_once()
    assert player_visual.assess.await_args.kwargs["capture_id"] == (
        "detail-baseline:scroll" if selected else "detail-baseline"
    )
    assert player_visual.assess.await_args.kwargs["page_type"] == "content_detail"
    assert player_visual.assess.await_args.kwargs["allow_scroll"] is selected
    assert len(result.primary_content_diagnostics) == 2
    # 正常选中/未选中不能由夹具异常降级冒充；分别核对列表和详情两份实际诊断。
    for diagnostic, parent in zip(
        result.primary_content_diagnostics, ("list-baseline", "detail-baseline"), strict=True
    ):
        assert diagnostic["status"] == ("captured" if selected else "blocked"), diagnostic
        assert diagnostic["diagnostic_type"] is None
        assert diagnostic["reason_code"] == (
            None if selected else "WEB_PRIMARY_CONTENT_AD_COVERAGE_NOT_IMPROVED"
        )
        assert diagnostic["parent_capture_id"] == parent
        assert diagnostic["evidence_capture_id"] == (f"{parent}:scroll" if selected else None)
        assert diagnostic["before_ad_coverage"] == 0.9
        assert diagnostic["after_ad_coverage"] == (0.1 if selected else 0.9)
