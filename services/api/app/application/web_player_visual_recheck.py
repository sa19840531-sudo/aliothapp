"""Web 详情播放器的只读视觉补判，不签发播放器交互。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.api.app.application.web_interaction_facts import web_target_exposed
from services.api.app.application.web_logical_page import (
    canonical_web_logical_page_url,
    web_logical_page_id,
)
from services.api.app.application.web_primary_content_capture import (
    build_web_primary_content_targets,
)
from services.api.app.application.web_subject_region_scopes import SubjectRegions, safe_resource
from services.api.app.domain.page.web_url_identity import (
    DEFAULT_WEB_URL_IDENTITY_POLICY,
    WebUrlIdentityPolicy,
)


@dataclass(frozen=True, slots=True)
class WebPlayerTarget:
    candidate_id: str
    selector: str
    media_selector: str
    dom_fingerprint: str
    rect: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class WebPlayerVisualRecheckResult:
    status: str
    source_capture_id: str
    evidence_capture_id: str | None
    reason_code: str | None = None


class WebPlayerEvidenceSource(Protocol):
    async def read_snapshot(self, *, capture_id: str) -> Mapping[str, object]: ...

    async def read_media_state(self, *, capture_id: str) -> Mapping[str, object] | None: ...

    async def read_ready(self, *, capture_id: str, page_role: str) -> object: ...

    async def read_url(self, *, capture_id: str) -> str: ...


def build_web_player_targets(
    ui_snapshot: Mapping[str, object],
    *,
    media_state: Mapping[str, object] | None,
) -> tuple[WebPlayerTarget, ...]:
    """从 DOM 派生快照与媒体事实选唯一非次要原生播放器。"""

    raw = ui_snapshot.get("elements")
    if not isinstance(raw, list):
        return ()
    selector_counts = Counter(
        str(item.get("selector") or "") for item in raw if isinstance(item, Mapping)
    )
    regions = SubjectRegions([item for item in raw if isinstance(item, Mapping)])
    media_items = media_state.get("items") if isinstance(media_state, Mapping) else None
    if not isinstance(media_items, list) or media_state.get("items_truncated") is True:
        return ()
    videos = [
        item for item in media_items if isinstance(item, Mapping) and item.get("tag") == "video"
    ]
    if len(videos) != 1:
        return ()
    candidates = []
    for selector in sorted(regions.primary):
        item = regions.nodes[selector]
        if (
            item.get("tag") != "video"
            or item.get("visible") is not True
            or selector_counts[selector] != 1
            or not safe_resource(item.get("src"))
            or item.get("within_repeated_structure") is True
            or (isinstance(item.get("repeat_group_size"), int) and item["repeat_group_size"] >= 2)
        ):
            continue
        rect = _rect(item.get("rect"))
        if rect is None:
            continue
        media_selector = str(videos[0].get("selector") or "").strip()
        media_bounds = _rect(videos[0].get("bounds"))
        if (
            not media_selector
            or (media_bounds is not None and not _same_geometry(rect, media_bounds))
            or (media_selector != selector and media_bounds is None)
        ):
            continue
        fingerprint = _target_fingerprint(item)
        candidate_id = (
            "web-player:"
            + hashlib.sha256(
                json.dumps(
                    {"selector": selector, "dom_fingerprint": fingerprint},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        )
        candidates.append(
            WebPlayerTarget(
                candidate_id=candidate_id,
                selector=selector,
                media_selector=media_selector,
                dom_fingerprint=fingerprint,
                rect=rect,
            )
        )
    return tuple(candidates) if len(candidates) == 1 else ()


class WebPlayerVisualRecheckService:
    """保留详情 ready 基线，只在可信视口补图上识别播放器控件。"""

    def __init__(
        self,
        *,
        primary_capture: object,
        evidence_source: WebPlayerEvidenceSource,
        assessor: object,
        writer: object,
        identity_policy: WebUrlIdentityPolicy = DEFAULT_WEB_URL_IDENTITY_POLICY,
    ) -> None:
        self._primary_capture = primary_capture
        self._evidence_source = evidence_source
        self._assessor = assessor
        self._writer = writer
        self._identity_policy = identity_policy

    async def assess(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        page_url: str,
        page_type: str,
        deadline: datetime,
        allow_scroll: bool = True,
    ) -> WebPlayerVisualRecheckResult:
        if page_type not in {"content_detail", "video_detail"}:
            raise ValueError("播放器视觉补判只接受详情页型")
        source_ready = await self._evidence_source.read_ready(
            capture_id=capture_id, page_role="detail"
        )
        source_snapshot = await self._evidence_source.read_snapshot(capture_id=capture_id)
        try:
            source_media = await self._evidence_source.read_media_state(capture_id=capture_id)
            media_unavailable = False
        except Exception:  # noqa: BLE001 - 可选媒体事实读取失败按不确定留诊断
            source_media = None
            media_unavailable = True
        source_targets = build_web_player_targets(source_snapshot, media_state=source_media)
        logical_page_id = web_logical_page_id(page_url, identity_policy=self._identity_policy)
        evidence_capture_id = capture_id
        reason_code = None
        target = source_targets[0] if source_targets else None
        source_facts = getattr(source_ready, "interaction_facts", None)
        source_trusted = (
            getattr(source_ready, "capture_id", None) == capture_id
            and self._same_page(str(getattr(source_ready, "url", "") or ""), page_url)
            and getattr(source_facts, "identity_verified", False) is True
            and getattr(source_facts, "readiness_accepted", False) is True
        )
        if not source_trusted:
            reason_code = "WEB_PLAYER_VISUAL_SOURCE_UNTRUSTED"
        elif target is None:
            reason_code = (
                "WEB_PLAYER_VISUAL_MEDIA_UNAVAILABLE"
                if media_unavailable
                else "WEB_PLAYER_VISUAL_TARGET_UNCERTAIN"
            )
        elif not allow_scroll and not _target_centered(source_snapshot, target):
            reason_code = "WEB_PLAYER_VISUAL_SCROLL_SKIPPED_AFTER_PRIMARY_FAILURE"
        elif not _target_centered(source_snapshot, target):
            primary_targets = build_web_primary_content_targets(
                capture_id=capture_id,
                ui_snapshot=source_snapshot,
                expected_page_type="video_detail",
            )
            if not primary_targets or primary_targets[0].selector != target.selector:
                reason_code = "WEB_PLAYER_VISUAL_SCROLL_TARGET_UNAVAILABLE"
            else:
                result = await self._primary_capture.capture(
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    session_id=session_id,
                    capture_id=capture_id,
                    page_url=page_url,
                    page_type="video_detail",
                    deadline=deadline,
                )
                if result.status != "captured" or not result.evidence_capture_id:
                    reason_code = str(result.reason_code or "WEB_PLAYER_VISUAL_SCROLL_FAILED")
                elif (
                    getattr(result, "target_candidate_id", None) is not None
                    and result.target_candidate_id != primary_targets[0].candidate_id
                ):
                    reason_code = "WEB_PLAYER_VISUAL_SCROLL_TARGET_DRIFTED"
                else:
                    evidence_capture_id = result.evidence_capture_id

        observed = None
        after_ready = source_ready
        after_snapshot = source_snapshot
        after_media = source_media
        if reason_code is None:
            after_url = await self._evidence_source.read_url(capture_id=evidence_capture_id)
            if not self._same_page(after_url, page_url):
                reason_code = "WEB_PLAYER_VISUAL_PAGE_CHANGED"
            else:
                after_ready = await self._evidence_source.read_ready(
                    capture_id=evidence_capture_id, page_role="detail"
                )
                after_snapshot = await self._evidence_source.read_snapshot(
                    capture_id=evidence_capture_id
                )
                try:
                    after_media = await self._evidence_source.read_media_state(
                        capture_id=evidence_capture_id
                    )
                    media_unavailable = False
                except Exception:  # noqa: BLE001 - 后态媒体事实失败不可送模
                    after_media = None
                    media_unavailable = True
                rebound = build_web_player_targets(after_snapshot, media_state=after_media)
                facts = getattr(after_ready, "interaction_facts", None)
                if media_unavailable:
                    reason_code = "WEB_PLAYER_VISUAL_MEDIA_UNAVAILABLE"
                elif (
                    not rebound
                    or target is None
                    or rebound[0].candidate_id != target.candidate_id
                    or not _target_centered(after_snapshot, rebound[0])
                    or not getattr(facts, "identity_verified", False)
                    or not getattr(facts, "readiness_accepted", False)
                    or getattr(after_ready, "capture_id", None) != evidence_capture_id
                    or not self._same_page(str(getattr(after_ready, "url", "") or ""), page_url)
                ):
                    reason_code = "WEB_PLAYER_VISUAL_TARGET_DRIFTED"
                elif not isinstance(after_media, Mapping) or not self._same_page(
                    str(after_media.get("url") or ""), page_url
                ):
                    reason_code = "WEB_PLAYER_VISUAL_MEDIA_IDENTITY_MISMATCH"
                elif not _media_loaded(after_media, rebound[0].media_selector):
                    reason_code = "WEB_PLAYER_VISUAL_MEDIA_NOT_READY"
                else:
                    try:
                        observed = await self._assessor.assess_player_visual(
                            screenshot_artifact_id=after_ready.screenshot_artifact_id,
                            dom_artifact_id=after_ready.dom_artifact_id,
                            candidate_id=target.candidate_id,
                            target_rect=dict(rebound[0].rect),
                            viewport=dict(after_snapshot.get("viewport") or {}),
                        )
                    except Exception:  # noqa: BLE001 - 模型不可用仍保留可信图证与诊断
                        reason_code = "WEB_PLAYER_VISUAL_ASSESSMENT_FAILED"

        payload = {
            "schema_version": "web-player-visual-recheck.v1",
            "status": "uncertain" if reason_code else str(observed.status),
            "reason_code": reason_code,
            "page_type_at_source": page_type,
            "source_capture_id": capture_id,
            "evidence_capture_id": evidence_capture_id,
            "logical_page_id": logical_page_id,
            "source_capture_state_fingerprint": str(
                getattr(source_ready, "capture_state_fingerprint", "") or ""
            ),
            "evidence_capture_state_fingerprint": str(
                getattr(after_ready, "capture_state_fingerprint", "") or ""
            ),
            "screenshot_artifact_id": (
                str(after_ready.screenshot_artifact_id)
                if reason_code in {None, "WEB_PLAYER_VISUAL_ASSESSMENT_FAILED"}
                else None
            ),
            "candidate_id": target.candidate_id if target else None,
            "target_dom_fingerprint": target.dom_fingerprint if target else None,
            "target_selector_sha256": (
                hashlib.sha256(target.selector.encode("utf-8")).hexdigest() if target else None
            ),
            "media_selector_sha256": (
                hashlib.sha256(target.media_selector.encode("utf-8")).hexdigest()
                if target
                else None
            ),
            "target_rect": dict(target.rect) if target else None,
            "source_target_rect": dict(target.rect) if target else None,
            "viewport_width": (
                after_snapshot["viewport"].get("width")
                if isinstance(after_snapshot.get("viewport"), Mapping)
                else None
            ),
            "viewport_height": (
                after_snapshot["viewport"].get("height")
                if isinstance(after_snapshot.get("viewport"), Mapping)
                else None
            ),
            "screenshot_width": None,
            "screenshot_height": None,
            "confidence": None,
            "reason": None,
            "controls": [],
        }
        if observed is not None:
            payload.update(
                screenshot_width=observed.screenshot_width,
                screenshot_height=observed.screenshot_height,
                confidence=observed.confidence,
                reason=observed.reason,
                target_rect=dict(rebound[0].rect),
            )
            boxes = _validated_controls(observed, rebound[0].rect, after_snapshot)
            if boxes is None:
                payload["status"] = "uncertain"
                payload["reason_code"] = "WEB_PLAYER_VISUAL_CONTROL_OUTSIDE_TARGET"
            else:
                payload["controls"] = boxes
        await self._writer.record_player_visual_recheck(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            capture_id=(
                evidence_capture_id
                if reason_code in {None, "WEB_PLAYER_VISUAL_ASSESSMENT_FAILED"}
                else capture_id
            ),
            payload=payload,
        )
        return WebPlayerVisualRecheckResult(
            status=str(payload["status"]),
            source_capture_id=capture_id,
            evidence_capture_id=evidence_capture_id,
            reason_code=payload["reason_code"],
        )

    def _same_page(self, left: str, right: str) -> bool:
        return canonical_web_logical_page_url(
            left, identity_policy=self._identity_policy
        ) == canonical_web_logical_page_url(right, identity_policy=self._identity_policy)


def _target_centered(snapshot: Mapping[str, object], target: WebPlayerTarget) -> bool:
    if not web_target_exposed(snapshot, selector=target.selector):
        return False
    viewport = snapshot.get("viewport")
    if not isinstance(viewport, Mapping) or type(viewport.get("height")) not in {int, float}:
        return False
    height = float(viewport["height"])
    center = target.rect["y"] + target.rect["height"] / 2
    return height > 0 and height * 0.2 <= center <= height * 0.8


def _rect(value: object) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    raw = [value.get(key) for key in ("x", "y", "width", "height")]
    if any(type(number) not in {int, float} or not math.isfinite(number) for number in raw):
        return None
    x, y, width, height = (float(number) for number in raw)
    return {"x": x, "y": y, "width": width, "height": height} if width > 0 and height > 0 else None


def _target_fingerprint(item: Mapping[str, object]) -> str:
    """真实 UI 快照无 dom_fingerprint 字段时，从不随滚动改变的结构生成指纹。"""

    declared = str(item.get("dom_fingerprint") or "").strip()
    if declared:
        return declared
    material = {
        "selector": item.get("selector"),
        "tag": item.get("tag"),
        "parent_selector": item.get("parent_selector"),
        "class_tokens": item.get("class_tokens") or [],
        "src": item.get("src"),
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _same_geometry(left: Mapping[str, float], right: Mapping[str, float]) -> bool:
    return all(abs(left[key] - right[key]) <= 2 for key in ("x", "y", "width", "height"))


def _media_loaded(media_state: Mapping[str, object] | None, selector: str) -> bool:
    items = media_state.get("items") if isinstance(media_state, Mapping) else None
    matching = (
        [
            item
            for item in items
            if isinstance(item, Mapping)
            and item.get("selector") == selector
            and item.get("tag") == "video"
        ]
        if isinstance(items, list)
        else []
    )
    return (
        len(matching) == 1
        and type(matching[0].get("ready_state")) is int
        and matching[0]["ready_state"] >= 2
    )


def _validated_controls(
    observed: object, target_rect: Mapping[str, float], snapshot: Mapping[str, object]
) -> list[dict[str, object]] | None:
    viewport = snapshot.get("viewport")
    if not isinstance(viewport, Mapping):
        return None
    width = getattr(observed, "screenshot_width", None)
    height = getattr(observed, "screenshot_height", None)
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        return None
    if width != viewport.get("width") or height != viewport.get("height"):
        return None
    controls = getattr(observed, "controls", ())
    if not isinstance(controls, (tuple, list)) or len(controls) > 4:
        return None
    result = []
    for control in controls:
        kind = getattr(control, "kind", None)
        normalized = [getattr(control, key, None) for key in ("x", "y", "width", "height")]
        if kind not in {"play", "pause"} or any(
            type(value) not in {int, float} or not math.isfinite(value) for value in normalized
        ):
            return None
        x, y, box_width, box_height = (float(value) for value in normalized)
        if (
            x < 0
            or y < 0
            or box_width <= 0
            or box_height <= 0
            or x + box_width > 1
            or y + box_height > 1
        ):
            return None
        px, py, pw, ph = x * width, y * height, box_width * width, box_height * height
        if (
            px < target_rect["x"]
            or py < target_rect["y"]
            or px + pw > target_rect["x"] + target_rect["width"]
            or py + ph > target_rect["y"] + target_rect["height"]
        ):
            return None
        pixel_box = {
            "kind": kind,
            "x": round(px),
            "y": round(py),
            "width": round(pw),
            "height": round(ph),
        }
        if pixel_box["width"] <= 0 or pixel_box["height"] <= 0:
            return None
        result.append(pixel_box)
    return result
