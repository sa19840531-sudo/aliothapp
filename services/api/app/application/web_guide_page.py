"""Web 应用引导页隔离观察判定；失败只形成支线结果。"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Mapping
from dataclasses import dataclass

from services.api.app.application.web_guide_candidate import WebGuideCandidate

_APP_TEXT = re.compile(r"(?<![a-z])app(?![a-z])|客户端|应用程序|移动端", re.I)
_DOWNLOAD_TEXT = re.compile(r"下载|安装|download|install", re.I)
_PLATFORM_TEXT = re.compile(r"安卓|android|苹果|ios|iphone|二维码|扫码", re.I)
_TAG = re.compile(r"<[^>]+>")
_MAIN = re.compile(r"<main\b[^>]*>(.*?)</main>", re.I | re.S)
_BODY = re.compile(r"<body\b[^>]*>(.*?)</body>", re.I | re.S)
_HEADING = re.compile(r"<h[1-3]\b[^>]*>(.*?)</h[1-3]>", re.I | re.S)


@dataclass(frozen=True, slots=True)
class WebGuideLanding:
    final_url: str
    title: str
    html: str
    screenshot: bytes
    ui_snapshot: Mapping[str, object]
    redirect_chain: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WebGuideAssessment:
    status: str
    page_type: str | None
    confidence: float
    reason_code: str


@dataclass(frozen=True, slots=True)
class WebGuideOutcome:
    status: str
    reason_code: str
    candidate_id: str
    source_capture_id: str
    capture_id: str | None = None
    page_type: str | None = None
    artifact_ids: tuple[str, ...] = ()


def assess_web_guide_landing(landing: WebGuideLanding) -> WebGuideAssessment:
    """只有落地页主体提供下载引导时才签发，来源链接不作分类证据。"""

    if not landing.final_url or not landing.screenshot or not landing.html.strip():
        return WebGuideAssessment("uncertain", None, 0.0, "WEB_GUIDE_LANDING_EVIDENCE_MISSING")
    fragment = (_MAIN.search(landing.html) or _BODY.search(landing.html))
    body = fragment.group(1) if fragment else landing.html
    plain = html.unescape(_TAG.sub(" ", body)).casefold()
    headings = " ".join(
        html.unescape(_TAG.sub(" ", match.group(1))) for match in _HEADING.finditer(body)
    )
    elements = landing.ui_snapshot.get("elements")
    actions = " ".join(
        str(item.get("text") or item.get("aria_label") or "")
        for item in elements if isinstance(item, Mapping)
    ) if isinstance(elements, list) else ""
    heading_app = bool(_APP_TEXT.search(headings))
    body_app = bool(_APP_TEXT.search(plain))
    download = bool(_DOWNLOAD_TEXT.search(plain) and _DOWNLOAD_TEXT.search(actions))
    platform = bool(_PLATFORM_TEXT.search(plain))
    if heading_app and body_app and download and platform:
        return WebGuideAssessment("confirmed", "guide_page", 0.85, "WEB_GUIDE_LANDING_CONFIRMED")
    return WebGuideAssessment("uncertain", None, 0.4, "WEB_GUIDE_LANDING_NEEDS_REVIEW")


class WebGuideBranch:
    """仅调用隔离观察和独立持久化端口，不持有主页面。"""

    def __init__(self, *, observer, writer, assessor=None, timeout_seconds: float = 20.0):
        self._observer = observer
        self._writer = writer
        self._assessor = assessor
        self._timeout_seconds = timeout_seconds

    async def attempt(
        self, candidate: WebGuideCandidate, *, task_id: str, agent_run_id: str,
    ) -> WebGuideOutcome:
        if candidate.kind == "download_resource":
            return WebGuideOutcome(
                "skipped", "WEB_GUIDE_DOWNLOAD_RESOURCE",
                candidate.candidate_id, candidate.source_capture_id,
            )
        try:
            landing = await asyncio.wait_for(
                self._observer.observe(candidate), timeout=self._timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - 目的站及浏览器失败只留下稳定支线结果
            return WebGuideOutcome(
                "incomplete", "WEB_GUIDE_OPEN_FAILED",
                candidate.candidate_id, candidate.source_capture_id,
            )
        assessment = assess_web_guide_landing(landing)
        assess_model = getattr(self._assessor, "assess_guide_page", None)
        try:
            if callable(assess_model):
                capture_id, refs = await self._writer.capture(
                    candidate=candidate, landing=landing,
                    task_id=task_id, agent_run_id=agent_run_id,
                )
                artifact_ids = tuple(refs.values())
                try:
                    model = await asyncio.wait_for(
                        assess_model(
                            screenshot_artifact_id=refs["screenshot"],
                            dom_artifact_id=refs["dom"],
                            ui_snapshot_artifact_id=refs["ui_snapshot"],
                            action_trace_artifact_id=refs["action_trace"],
                            final_url=landing.final_url,
                        ),
                        timeout=self._timeout_seconds,
                    )
                    confidence = getattr(model, "confidence", 0)
                    if (
                        getattr(model, "page_type", None) == "guide_page"
                        and isinstance(confidence, int | float)
                        and not isinstance(confidence, bool)
                        and confidence >= 0.7
                        and not getattr(model, "needs_review_reason", None)
                    ):
                        assessment = WebGuideAssessment(
                            "confirmed", "guide_page", float(confidence),
                            "WEB_GUIDE_MODEL_CONFIRMED",
                        )
                    else:
                        assessment = WebGuideAssessment(
                            "uncertain", None, 0.4, "WEB_GUIDE_MODEL_NEEDS_REVIEW",
                        )
                except Exception:  # noqa: BLE001 - 模型失败仍保存未确认落地证据
                    assessment = WebGuideAssessment(
                        "uncertain", None, 0.4, "WEB_GUIDE_MODEL_UNAVAILABLE",
                    )
                await self._writer.mark(
                    page_capture_id=capture_id,
                    source_capture_id=candidate.source_capture_id,
                    assessment=assessment, task_id=task_id, agent_run_id=agent_run_id,
                )
            else:
                capture_id, artifact_ids = await self._writer.persist(
                    candidate=candidate, landing=landing, assessment=assessment,
                    task_id=task_id, agent_run_id=agent_run_id,
                )
        except Exception:  # noqa: BLE001 - 可选证据失败不能使主任务失败
            return WebGuideOutcome(
                "incomplete", "WEB_GUIDE_CAPTURE_FAILED",
                candidate.candidate_id, candidate.source_capture_id,
            )
        return WebGuideOutcome(
            assessment.status, assessment.reason_code,
            candidate.candidate_id, candidate.source_capture_id,
            capture_id=capture_id, page_type=assessment.page_type,
            artifact_ids=tuple(artifact_ids),
        )
