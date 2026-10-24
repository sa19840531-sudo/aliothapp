"""真实菜单动作、Runtime 持久证据门和菜单结算的接线回归。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.web_desktop_exploration import WebDesktopMenuPageVisitor
from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_runtime_composition import WebOrchestratedMenuPageVisitor
from services.api.app.application.web_runtime_evidence import (
    WebReadyCaptureReaderAdapter,
    WebRuntimeEvidenceSource,
)
from services.api.app.domain.page import WebMenuDecision, WebSiteMenuRegistry
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    WebCaptureArtifactIndex,
)
from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics


@pytest.mark.parametrize(
    "scenario,arrived",
    [
        ("stable", True),
        ("ordinary_content", True),
        ("loading", False),
        ("captcha", False),
        ("diagnostic", False),
        ("old_capture", False),
        ("lease_lost", False),
        ("same_url_subject", True),
        ("same_url_unknown", False),
    ],
)
@pytest.mark.asyncio
async def test_menu_settlement_consumes_actual_ready_facts(scenario, arrived):
    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    before_url = entry.canonical_url if scenario.startswith("same_url") else "https://hl365.com/"
    current = ["home"]
    actions = []
    command = RunWebWorkflowCommand(
        task_id="task",
        agent_run_id="run",
        session_id="web:run",
        session_owner="web-v2-workflow",
        target_url="https://hl365.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )

    class IndexReader:
        async def matches_active_capture(self, **kwargs):
            return not kwargs["current_only"] or kwargs["capture_id"] == current[0]

        async def read(self, *, task_id, capture_id):
            metrics = {"web_readiness": {**_stable_capture_metrics(), "role": "classification"}}
            if scenario == "loading":
                metrics["web_readiness"]["visible_loader"] = True
            if scenario == "captcha":
                metrics["web_readiness"]["human_verification_present"] = True
            if scenario == "diagnostic":
                metrics["diagnostic_capture"] = True
            return WebCaptureArtifactIndex(
                capture_id=capture_id,
                url=before_url if capture_id == "home" else entry.canonical_url,
                captured_at=datetime.now(UTC),
                pipeline_metrics=metrics,
                artifact_ids={"screenshot": "s", "dom": "d", "visible_text": "t"},
            )

    class SnapshotReader:
        async def read_ui_snapshot(self, *, task_id, page_capture_id):
            if scenario == "ordinary_content":
                return {
                    "web_capture_stage": "web_page_ready_baseline",
                    "elements": [
                        {
                            "tag": "div",
                            "class_tokens": ["post-content"],
                            "selector": "#body",
                            "visible": True,
                            "text": "正文",
                        }
                    ],
                }
            return {
                "web_capture_stage": "web_page_ready_baseline",
                "elements": [
                    {
                        "tag": "main",
                        "selector": "#body",
                        "visible": True,
                        "text": "原正文" if page_capture_id == "home" else "动作后正文",
                    },
                    {
                        "tag": "video",
                        "selector": "#player",
                        "visible": True,
                        "src": entry.canonical_url
                        if scenario == "same_url_subject" and page_capture_id != "home"
                        else "/media/old.mp4",
                    },
                ],
            }

    source = WebRuntimeEvidenceSource(
        task_id="task",
        agent_run_id="run",
        session_id="web:run",
        deadline=command.deadline,
        index_reader=IndexReader(),
        snapshot_reader=SnapshotReader(),
    )

    class Coordinator:
        async def coordinate(self, action):
            actions.append(action.intent)
            current[0] = "probe"
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="probe"),
                interaction=SimpleNamespace(
                    action_execution_id="actual-menu",
                    execution_outcome="succeeded",
                    status="evidence_failed" if scenario == "lease_lost" else "succeeded",
                    error_code="lease_lost" if scenario == "lease_lost" else None,
                ),
            )

    class Readiness:
        async def prepare(self, **kwargs):
            current[0] = "other" if scenario == "old_capture" else "ready"
            return SimpleNamespace(
                status="ready",
                ready_capture_id="ready",
                evidence_capture_ids=("probe", "ready"),
                dismissed_layers=(),
            )

    class Classifier:
        async def classify(self, capture):
            return SimpleNamespace(page_type="content_detail")

    result = await WebOrchestratedMenuPageVisitor(
        navigator=WebDesktopMenuPageVisitor(coordinator=Coordinator(), capture_url_reader=source),
        readiness=Readiness(),
        ready_capture_reader=WebReadyCaptureReaderAdapter(source=source),
        classifier=Classifier(),
        evidence_source=source,
        detail_navigator=object(),
    ).visit(
        command=command,
        entry=entry,
        source_capture_id="home",
        menu_intent_id="menu-intent",
        candidate=WebNavigationCandidate(
            entry_id="candidate",
            label=entry.menu_path[-1],
            url=entry.canonical_url,
            menu_path=entry.menu_path,
            dom_fingerprint="dom",
            source_signal="semantic_nav",
            page_type_key="nav:test",
        ),
    )
    assert len(actions) == 1
    assert result.menu_interaction_outcome.command_accepted is True
    assert result.menu_interaction_outcome.transition_arrived is arrived
    assert (result.error_code is None) is arrived
