"""读取既有时序原件中的呈现事实；文案、状态码和媒体快照不决定业务状态。"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from services.api.app.application.green_evidence_contracts import GreenEvidenceSet


class PresentationKind(StrEnum):
    """只描述已观察对象，不声明真实空数据、业务失败或业务成功。"""

    EMPTY_COPY = "empty_copy"
    FAILURE_COPY = "failure_copy"
    REASON_COPY = "reason_copy"
    LOADING_COPY = "loading_copy"
    RETRY_TARGET = "retry_target"
    GUIDANCE_COPY = "guidance_copy"
    MEDIA_SNAPSHOT = "media_snapshot"
    REQUEST_FAILURE = "request_failure"
    TRANSIENT_MESSAGE = "transient_message"
    OTHER_COPY = "other_copy"


@dataclass(frozen=True)
class PresentationFact:
    """同一原件的可回溯观察；父级选择器仅表示呈现范围，不是业务模块认证。"""

    kind: PresentationKind
    capture_id: str
    artifact_id: str
    selector: str | None = None
    parent_selector: str | None = None


@dataclass(frozen=True)
class TemporalObservations:
    """已读事实与读取歧义分开保存，缺席不推导违规。"""

    facts: tuple[PresentationFact, ...] = ()
    issues: tuple[str, ...] = ()


_EMPTY = re.compile(r"(?:暂无(?:数据|内容|记录)|没有(?:数据|内容|记录)|no (?:data|results))", re.I)
_FAILURE = re.compile(
    r"(?:加载失败|请求失败|网络(?:连接)?异常|网络已断开|连接超时|请求超时|"
    r"network error|failed to load|request timed out)(?:[，,。.!！\s]*(?:请)?(?:刷新|重试))?",
    re.I,
)
_REASON = re.compile(
    r"(?:网络(?:连接)?异常|网络已断开|连接超时|请求超时|network error|request timed out)"
    r"(?:[，,。.!！\s]*(?:请)?(?:刷新|重试))?",
    re.I,
)
_LOADING = re.compile(r"(?:加载中|正在加载|请稍候|请稍等|loading)[.。…\s]*", re.I)
_RETRY = re.compile(r"(?:重试|重新加载|刷新|retry|reload|refresh)", re.I)
_GUIDANCE = re.compile(
    r"(?:请|点击|点此)(?:刷新|重试)|(?:加载失败|请求失败)[，,。\s]*(?:请)?(?:刷新|重试)"
)
_DEGRADED = (
    "capture_degraded", "collection_error", "budget_truncated", "element_limit_reached",
    "truncated", "elements_truncated",
)


def read_temporal_observations(evidence: GreenEvidenceSet) -> TemporalObservations:
    """仅消费仓储已核验原件；重复原件、重复定位或截断均不选择性采信。"""
    if not isinstance(evidence, GreenEvidenceSet) or not evidence.verified_read:
        raise ValueError("时序事实只接受已核验的正式输入原件")
    facts, issues = [], list(evidence.issues)
    for capture in evidence.captures:
        counts = Counter(item.artifact_type for item in capture.originals)
        for original in capture.originals:
            kind = original.artifact_type
            if kind not in {"ui_snapshot", "media_state", "har", "action_trace"}:
                continue
            if original.capture_id != capture.capture_id or counts[kind] != 1:
                issues.append("temporal_original_ambiguous:" + capture.capture_id)
                continue
            try:
                payload = json.loads(original.payload)
            except (UnicodeDecodeError, ValueError, TypeError):
                issues.append("temporal_original_unreadable:" + original.artifact_id)
                continue
            def make(value, selector=None, parent=None, c=capture, o=original):
                return PresentationFact(value, c.capture_id, o.artifact_id, selector, parent)
            if kind == "ui_snapshot":
                observed, missing = _ui_facts(payload, make)
                facts.extend(observed)
                issues.extend(missing)
            elif kind == "media_state" and isinstance(payload, dict):
                if payload.get("schema_version") in ("media-state.v1", "app-media-state.v1"):
                    if isinstance(payload.get("items"), list) and payload["items"]:
                        facts.append(make(PresentationKind.MEDIA_SNAPSHOT))
            elif kind == "har" and isinstance(payload, dict):
                entries = payload.get("entries")
                if payload.get("format") == "network_events" and isinstance(entries, list):
                    if any(isinstance(row, dict) and row.get("event") == "request_failed"
                           for row in entries):
                        facts.append(make(PresentationKind.REQUEST_FAILURE))
            elif kind == "action_trace" and isinstance(payload, list):
                if any(_has_message(row) for row in payload):
                    facts.append(make(PresentationKind.TRANSIENT_MESSAGE))
    return TemporalObservations(tuple(facts), tuple(dict.fromkeys(issues)))


def _ui_facts(payload, make):
    if not isinstance(payload, dict) or any(payload.get(key) for key in _DEGRADED):
        return (), ("temporal_ui_incomplete",)
    if not (payload.get("schema_version") == "ui-snapshot.v2"
            or payload.get("source") == "android_ui_hierarchy"):
        return (), ("temporal_ui_shape_unsupported",)
    rows = payload.get("elements")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return (), ("temporal_ui_elements_unreadable",)
    selectors = Counter(row["selector"] for row in rows if isinstance(row.get("selector"), str))
    issues = ("temporal_ui_locator_ambiguous",) if any(
        count > 1 for count in selectors.values()
    ) else ()
    facts = []
    for row in rows:
        selector = row.get("selector")
        if not isinstance(selector, str) or selectors[selector] != 1 or not _visible(row, payload):
            continue
        parent = row.get("parent_selector")
        parent = parent if isinstance(parent, str) and selectors[parent] == 1 else None
        text = str(
            row.get("text") or row.get("aria_label") or row.get("content_desc") or ""
        ).strip()
        kinds = []
        if _EMPTY.fullmatch(text):
            kinds.append(PresentationKind.EMPTY_COPY)
        if _FAILURE.fullmatch(text):
            kinds.append(PresentationKind.FAILURE_COPY)
        if _REASON.fullmatch(text):
            kinds.append(PresentationKind.REASON_COPY)
        if _LOADING.fullmatch(text):
            kinds.append(PresentationKind.LOADING_COPY)
        if _GUIDANCE.fullmatch(text):
            kinds.append(PresentationKind.GUIDANCE_COPY)
        if _RETRY.fullmatch(text) and _hittable(row):
            kinds.append(PresentationKind.RETRY_TARGET)
        if text and not kinds:
            kinds.append(PresentationKind.OTHER_COPY)
        facts.extend(make(kind, selector, parent) for kind in kinds)
    return tuple(facts), issues


def _visible(row, snapshot):
    if row.get("visible") is not True:
        return False
    rect, viewport = row.get("rect"), snapshot.get("viewport")
    if not isinstance(rect, dict) or not isinstance(viewport, dict):
        return False
    values = [rect.get(key) for key in ("x", "y", "width", "height")]
    values.extend(viewport.get(key) for key in ("width", "height"))
    if not all(type(value) in {int, float} and math.isfinite(value) for value in values):
        return False
    x, y, width, height, vw, vh = values
    style = row.get("style") if isinstance(row.get("style"), dict) else {}
    if any(style.get(key) is not None and not isinstance(style[key], str)
           for key in ("display", "visibility")):
        return False
    try:
        opacity = float(style.get("opacity", 1))
    except (ValueError, TypeError):
        return False
    return (width > 0 and height > 0 and vw > 0 and vh > 0 and x < vw and y < vh
            and x + width > 0 and y + height > 0 and style.get("display") != "none"
            and style.get("visibility") not in {"hidden", "collapse"}
            and math.isfinite(opacity) and opacity > 0)


def _hittable(row):
    hit = row.get("hit_test")
    return (row.get("interactive") is True and row.get("disabled") is not True
            and str(row.get("aria_disabled", "false")).lower() != "true"
            and isinstance(hit, dict) and hit.get("center_hits_target") is True)


def _has_message(row):
    if not isinstance(row, dict) or not isinstance(row.get("transient_messages"), list):
        return False
    return any(isinstance(item, dict) and isinstance(item.get("text"), str)
               and item["text"].strip() for item in row["transient_messages"])
