"""不明候选的身份错误不能中断真实观察 Writer。"""

import base64
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.capture_coordination import CapturePhase, CaptureWriteRequest
from services.api.app.domain.interaction import CaptureDirective, SurfaceKind
from services.api.app.infrastructure.browser.capture_runtime import (
    BrowserCaptureWriter,
    InMemoryActionEvidenceBuffer,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureResult,
)
from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
    WebDesktopCapturePolicyBrowser,
)
from services.api.tests.test_web_capture_runtime import RecordingPersistence
from services.api.tests.test_web_desktop_capture_policy import _navigation_snapshot, _png


@pytest.mark.asyncio
async def test_unknown_hash_candidate_preserves_writer_artifacts_and_blocks_only_candidate():
    dom = '<nav><a href="/#route=detail">不明路由</a><a href="/list">列表</a></nav>'
    ui_snapshot = _navigation_snapshot(("不明路由", "/#route=detail"), ("列表", "/list"))
    ui_snapshot["elements"][0]["selector"] = "#card a"

    class Browser:
        async def capture(self, request):
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://example.test/",
                title="页面",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact(artifact_type="dom", artifact_id=None, content=dom),
                    PageCaptureArtifact(
                        artifact_type="screenshot",
                        artifact_id=None,
                        content={
                            "encoding": "base64",
                            "data": base64.b64encode(_png(1920, 1080)).decode(),
                        },
                    ),
                    PageCaptureArtifact(
                        artifact_type="ui_snapshot",
                        artifact_id=None,
                        content=ui_snapshot,
                    ),
                ],
            )

    policy = WebDesktopCapturePolicyBrowser(browser=Browser())
    persistence = RecordingPersistence()
    writer = BrowserCaptureWriter(
        browser=policy,
        persistence=persistence,
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )
    ref = await writer.capture(
        CaptureWriteRequest(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            surface_kind=SurfaceKind.WEB,
            intent_id="observe",
            phase=CapturePhase.OBSERVATION,
            directive=CaptureDirective(
                role="observation", artifact_types=("dom", "screenshot", "ui_snapshot")
            ),
            parent_capture_id=None,
            action_execution_id=None,
            session_command_sequence=1,
            finalization_epoch=0,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            web_capture_stage="web_entry_probe",
        )
    )
    saved = persistence.calls[0]["result"]
    assert next(a.content for a in saved.artifacts if a.artifact_type == "dom") == dom
    assert {"dom", "screenshot", "ui_snapshot"} == {a.artifact_type for a in saved.artifacts}
    policy.bind_latest_navigation_capture("web:run-1", ref.capture_id)
    candidates = policy.navigation_candidates("web:run-1")
    blocked = next(c for c in candidates if c.target_hint == "/#route=detail")
    assert blocked.url is None and blocked.actionability == "blocked"
    assert blocked.source_capture_id == ref.capture_id
    assert (
        next(c for c in candidates if c.target_hint == "/list").url == "https://example.test/list"
    )
    navigation = next(a.content for a in saved.artifacts if a.artifact_type == "ui_snapshot")[
        "web_navigation"
    ]
    assert navigation["candidates"][0]["identity_review_reason"] == "WEB_URL_HASH_NEEDS_REVIEW"
    snapshot = next(a.content for a in saved.artifacts if a.artifact_type == "ui_snapshot")
    assert snapshot["web_url_identity_reviews"] == [
        {
            "href": "/#route=detail",
            "selector": "#card a",
            "reason_code": "WEB_URL_HASH_NEEDS_REVIEW",
            "source": "containing_capture",
        }
    ]
    assert blocked.to_dict()["identity_review_reason"] == "WEB_URL_HASH_NEEDS_REVIEW"


def test_detail_extraction_keeps_unknown_href_as_review_without_losing_valid_card():
    from services.api.app.application.web_detail_candidate_extraction import (
        detail_candidates_from_ready_snapshot,
    )
    from services.api.tests.test_web_list_detail_navigation import _element, _snapshot

    snapshot = _snapshot(
        _element(href="/#route=detail"),
        _element(href="/video/2", selector="#card-2 a"),
    )
    diagnostics = []
    candidates = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="video_list",
        origin="https://example.test",
        current_url="https://example.test/list",
        review_diagnostics=diagnostics,
    )
    assert len(candidates) == 1 and candidates[0].canonical_url == "https://example.test/video/2"
    assert diagnostics == [
        {
            "href": "/#route=detail",
            "source_capture_id": "capture-list",
            "reason_code": "WEB_URL_HASH_NEEDS_REVIEW",
        }
    ]


def test_menu_reconciliation_isolates_unreviewable_hash_from_other_candidates():
    from services.api.app.application.web_menu_exploration import reconcile_site_menu
    from services.api.app.domain.page import WebSiteMenuRegistry
    from services.api.tests.test_web_desktop_navigation import (
        _runtime_navigation_candidates as extract_navigation_candidates,
    )

    site = WebSiteMenuRegistry().resolve_site("http://91porna.com/")
    candidates = extract_navigation_candidates(
        '<nav><a href="/#route=detail">不明</a><a href="/">首页</a></nav>',
        base_url="http://91porna.com/",
    )
    result = reconcile_site_menu(site, candidates)
    blocked = next(c for c in result.runtime_unreviewed if c.target_hint == "/#route=detail")
    assert blocked.url is None and blocked.actionability == "blocked"
    assert blocked.to_dict()["identity_review_reason"] == "WEB_URL_HASH_NEEDS_REVIEW"
