"""引导支线独立留证与失败跳过。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from services.api.app.application.web_guide_candidate import WebGuideCandidate
from services.api.app.application.web_guide_page import (
    WebGuideBranch,
    WebGuideLanding,
    assess_web_guide_landing,
)


def _candidate(kind: str = "page_candidate") -> WebGuideCandidate:
    return WebGuideCandidate(
        candidate_id="candidate-1",
        source_capture_id="source-1",
        source_url="https://site.example/",
        destination_url="https://site.example/app.html",
        label="官方 APP",
        kind=kind,
    )


def _landing(html: str) -> WebGuideLanding:
    return WebGuideLanding(
        final_url="https://site.example/app.html",
        title="APP 下载",
        html=html,
        screenshot=b"valid-screenshot",
        ui_snapshot={"elements": [
            {"tag": "a", "text": "安卓下载", "href": "https://site.example/app.apk"},
            {"tag": "div", "text": "扫码安装"},
        ]},
        redirect_chain=("https://site.example/app.html",),
    )


def test_guide_requires_landing_body_not_source_footer_link() -> None:
    confirmed = assess_web_guide_landing(
        _landing("<main><h1>下载官方 APP</h1><a>安卓下载</a><p>扫码安装 iOS 客户端</p></main>")
    )
    assert confirmed.status == "confirmed"
    assert confirmed.page_type == "guide_page"
    source_home = replace(
        _landing(
            "<main><h1>最新内容</h1><article>视频内容</article></main>"
            "<footer>APP 下载</footer>"
        ),
        title="首页",
        ui_snapshot={"elements": [{"tag": "a", "text": "APP 下载", "href": "/app"}]},
    )
    uncertain = assess_web_guide_landing(source_home)
    assert uncertain.page_type is None


@pytest.mark.asyncio
async def test_guide_branch_persists_confirmed_landing_without_touching_main() -> None:
    writes = []
    class Observer:
        async def observe(self, candidate):
            assert candidate.source_capture_id == "source-1"
            return _landing(
                "<main><h1>下载官方 APP</h1><a>安卓下载</a>"
                "<p>扫码安装 iOS 客户端</p></main>"
            )
    class Writer:
        async def persist(self, *, candidate, landing, assessment, task_id, agent_run_id):
            writes.append((candidate, landing, assessment, task_id, agent_run_id))
            return "guide-capture-1", ("artifact-1",)
    branch = WebGuideBranch(observer=Observer(), writer=Writer())
    result = await branch.attempt(_candidate(), task_id="task-1", agent_run_id="run-1")
    assert result.status == "confirmed"
    assert result.capture_id == "guide-capture-1"
    assert result.page_type == "guide_page"
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_guide_branch_skips_resource_and_observer_failure() -> None:
    class Observer:
        async def observe(self, candidate):
            raise TimeoutError("destination unavailable")
    class Writer:
        async def persist(self, **kwargs):
            raise AssertionError("must not persist failed landing")
    branch = WebGuideBranch(observer=Observer(), writer=Writer())
    resource = await branch.attempt(_candidate("download_resource"), task_id="t", agent_run_id="r")
    failed = await branch.attempt(_candidate(), task_id="t", agent_run_id="r")
    assert resource.status == "skipped"
    assert resource.reason_code == "WEB_GUIDE_DOWNLOAD_RESOURCE"
    assert failed.status == "incomplete"
    assert failed.reason_code == "WEB_GUIDE_OPEN_FAILED"


@pytest.mark.asyncio
async def test_guide_model_uses_persisted_landing_artifacts_before_signing_type() -> None:
    events = []

    class Observer:
        async def observe(self, _candidate):
            return _landing("<main><h1>官方 APP</h1><a>安卓下载</a><p>iOS 扫码</p></main>")

    class Writer:
        async def capture(self, **_kwargs):
            events.append("capture")
            return "guide-capture", {
                "screenshot": "shot", "dom": "dom", "ui_snapshot": "ui",
                "action_trace": "trace",
            }

        async def mark(self, **kwargs):
            events.append(("mark", kwargs["assessment"].page_type))

    class Assessor:
        async def assess_guide_page(self, **kwargs):
            assert kwargs["screenshot_artifact_id"] == "shot"
            assert kwargs["dom_artifact_id"] == "dom"
            events.append("model")
            return type("Result", (), {
                "page_type": "guide_page", "confidence": 0.9,
                "needs_review_reason": None,
            })()

    branch = WebGuideBranch(observer=Observer(), writer=Writer(), assessor=Assessor())
    outcome = await branch.attempt(_candidate(), task_id="task-1", agent_run_id="run-1")
    assert outcome.status == "confirmed"
    assert events == ["capture", "model", ("mark", "guide_page")]
