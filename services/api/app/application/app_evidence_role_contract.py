"""App 证据角色合同：长图序列、广告外跳、收银台链、播放器连续拖动。

角色码供 Capture／Action／Judge 共用；拼接总览与模型裁剪只能是派生角色，
不得冒充视口主证据或独立业务到达。本模块只定义合同与校验，不打开生产 Gate。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

APP_EVIDENCE_ROLE_CONTRACT_VERSION = "app-evidence-role.v1"

# —— 入口层／稳定业务（与 capture readiness 推荐角色对齐）——
ROLE_STABLE_BUSINESS_CAPTURE = "stable_business_capture"
ROLE_OVERLAY_OCCURRENCE = "overlay_occurrence"
ROLE_EXTERNAL_LANDING = "external_landing"
ROLE_TRANSITION_WITNESS = "transition_witness"
ROLE_DIAGNOSTIC_ONLY = "diagnostic_only"

# —— 长图／视口 ——
ROLE_VIEWPORT_PRIMARY = "viewport_primary"
ROLE_LONG_PAGE_SCROLL_SERIES = "long_page_scroll_series"
ROLE_STITCHED_OVERVIEW_DERIVED = "stitched_overview_derived"

# —— 广告外跳三联 ——
ROLE_AD_OUTBOUND_SOURCE = "ad_outbound_source"
ROLE_AD_OUTBOUND_LANDING = "ad_outbound_landing"
ROLE_AD_OUTBOUND_RETURN = "ad_outbound_return"

# —— 收银台店内长链 ——
ROLE_CHECKOUT_CHAIN_STEP = "checkout_chain_step"

# —— 播放器连续拖动 ——
ROLE_PLAYER_SCRUB_SERIES = "player_scrub_series"
ROLE_DEEP_LINK_DIRECT_OBSERVATION = "deep_link_direct_observation"

APP_EVIDENCE_ROLES = frozenset(
    {
        ROLE_STABLE_BUSINESS_CAPTURE,
        ROLE_OVERLAY_OCCURRENCE,
        ROLE_EXTERNAL_LANDING,
        ROLE_TRANSITION_WITNESS,
        ROLE_DIAGNOSTIC_ONLY,
        ROLE_VIEWPORT_PRIMARY,
        ROLE_LONG_PAGE_SCROLL_SERIES,
        ROLE_STITCHED_OVERVIEW_DERIVED,
        ROLE_AD_OUTBOUND_SOURCE,
        ROLE_AD_OUTBOUND_LANDING,
        ROLE_AD_OUTBOUND_RETURN,
        ROLE_CHECKOUT_CHAIN_STEP,
        ROLE_PLAYER_SCRUB_SERIES,
        ROLE_DEEP_LINK_DIRECT_OBSERVATION,
    }
)

# 仅派生：不得当正式可见性／到达主证据
_DERIVED_ONLY_ROLES = frozenset({ROLE_STITCHED_OVERVIEW_DERIVED})

# 不得计为独立业务页到达
_NON_ARRIVAL_ROLES = frozenset(
    {
        ROLE_LONG_PAGE_SCROLL_SERIES,
        ROLE_STITCHED_OVERVIEW_DERIVED,
        ROLE_AD_OUTBOUND_LANDING,
        ROLE_TRANSITION_WITNESS,
        ROLE_DIAGNOSTIC_ONLY,
        ROLE_OVERLAY_OCCURRENCE,
        ROLE_PLAYER_SCRUB_SERIES,
        ROLE_DEEP_LINK_DIRECT_OBSERVATION,
    }
)

_CHECKOUT_STEP_KEYS = frozenset(
    {
        "cart_or_sku",
        "confirm_order",
        "in_app_cashier",
        "paying_or_processing",
        "result_or_cancel",
        "return_to_business",
    }
)


def is_known_app_evidence_role(role: str | None) -> bool:
    text = str(role or "").strip()
    return text in APP_EVIDENCE_ROLES


def role_is_derived_only(role: str | None) -> bool:
    return str(role or "").strip() in _DERIVED_ONLY_ROLES


def role_counts_as_business_arrival(role: str | None) -> bool:
    text = str(role or "").strip()
    if not text or text in _NON_ARRIVAL_ROLES or text in _DERIVED_ONLY_ROLES:
        return False
    return text in {
        ROLE_STABLE_BUSINESS_CAPTURE,
        ROLE_VIEWPORT_PRIMARY,
        ROLE_CHECKOUT_CHAIN_STEP,
        ROLE_AD_OUTBOUND_SOURCE,
        ROLE_AD_OUTBOUND_RETURN,
    }


def build_viewport_primary_evidence(
    *,
    capture_id: str,
    parent_capture_id: str | None = None,
) -> dict[str, Any]:
    """当前视口主证据：正式可见性与点击／遮挡判断的默认角色。"""

    capture = str(capture_id or "").strip()
    parent = str(parent_capture_id or "").strip() or None
    return {
        "protocol_version": APP_EVIDENCE_ROLE_CONTRACT_VERSION,
        "evidence_role": ROLE_VIEWPORT_PRIMARY,
        "capture_id": capture or None,
        "parent_capture_id": parent,
        "counts_as_independent_business_arrival": bool(capture),
        "derived_only": False,
    }


def build_long_page_scroll_series_evidence(
    *,
    parent_capture_id: str,
    frame_capture_ids: Sequence[str],
    gap_codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """长页滚动序列：每帧是视口原图；禁止用拼接图替代本序列。"""

    parent = str(parent_capture_id or "").strip()
    frames = [str(item).strip() for item in frame_capture_ids if str(item).strip()]
    gaps = [str(item).strip() for item in (gap_codes or ()) if str(item).strip()]
    return {
        "protocol_version": APP_EVIDENCE_ROLE_CONTRACT_VERSION,
        "evidence_role": ROLE_LONG_PAGE_SCROLL_SERIES,
        "parent_capture_id": parent or None,
        "frame_capture_ids": frames,
        "gap_codes": gaps,
        "counts_as_independent_business_arrival": False,
        "requires_parent_lineage": True,
        "derived_only": False,
        "forbids_stitched_as_primary": True,
    }


def build_stitched_overview_derived_evidence(
    *,
    parent_capture_id: str,
    source_frame_capture_ids: Sequence[str],
    derived_artifact_id: str | None = None,
    gap_codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """拼接长图总览：仅派生展示，不得当主证据或独立到达。"""

    parent = str(parent_capture_id or "").strip()
    frames = [
        str(item).strip() for item in source_frame_capture_ids if str(item).strip()
    ]
    gaps = [str(item).strip() for item in (gap_codes or ()) if str(item).strip()]
    derived = str(derived_artifact_id or "").strip() or None
    return {
        "protocol_version": APP_EVIDENCE_ROLE_CONTRACT_VERSION,
        "evidence_role": ROLE_STITCHED_OVERVIEW_DERIVED,
        "parent_capture_id": parent or None,
        "source_frame_capture_ids": frames,
        "derived_artifact_id": derived,
        "gap_codes": gaps,
        "counts_as_independent_business_arrival": False,
        "requires_parent_lineage": True,
        "derived_only": True,
        "forbids_as_judge_primary": True,
    }


def build_ad_outbound_triplet_evidence(
    *,
    source_capture_id: str,
    landing_capture_id: str | None = None,
    return_capture_id: str | None = None,
    external_activity_kind: str | None = None,
    attempt_index: int = 1,
    landing_url: str | None = None,
) -> dict[str, Any]:
    """广告外跳取证三联：源图 → 外部落地 → 回源；缺任一环记缺口。"""

    source = str(source_capture_id or "").strip()
    landing = str(landing_capture_id or "").strip() or None
    returned = str(return_capture_id or "").strip() or None
    kind = str(external_activity_kind or "").strip() or None
    url = str(landing_url or "").strip() or None
    if url and not url.startswith(("http://", "https://")):
        url = None
    gaps: list[str] = []
    if not source:
        gaps.append("ad_outbound_source_missing")
    if not landing:
        gaps.append("ad_outbound_landing_missing")
    if not returned:
        gaps.append("ad_outbound_return_missing")
    landing_payload: dict[str, Any] = {
        "evidence_role": ROLE_AD_OUTBOUND_LANDING,
        "capture_id": landing,
        "counts_as_independent_business_arrival": False,
        "counts_as_business_coverage": False,
    }
    if url:
        landing_payload["url"] = url
    return {
        "protocol_version": APP_EVIDENCE_ROLE_CONTRACT_VERSION,
        "evidence_bundle": "ad_outbound_triplet",
        "attempt_index": max(1, int(attempt_index or 1)),
        "external_activity_kind": kind,
        "source": {
            "evidence_role": ROLE_AD_OUTBOUND_SOURCE,
            "capture_id": source or None,
            "counts_as_independent_business_arrival": False,
        },
        "landing": landing_payload,
        "return_to_app": {
            "evidence_role": ROLE_AD_OUTBOUND_RETURN,
            "capture_id": returned,
            "counts_as_independent_business_arrival": False,
        },
        "gap_codes": gaps,
        "complete": not gaps,
        "lifetime_attempt_cap": 1,
    }


def build_checkout_chain_step_evidence(
    *,
    step_key: str,
    capture_id: str,
    chain_id: str,
    step_index: int,
    in_target_package: bool = True,
    high_risk_confirm: bool = False,
) -> dict[str, Any]:
    """收银台／下单店内长链单步：每步独立 Capture；外部支付页不入链。"""

    key = str(step_key or "").strip()
    capture = str(capture_id or "").strip()
    chain = str(chain_id or "").strip()
    gaps: list[str] = []
    if key not in _CHECKOUT_STEP_KEYS:
        gaps.append("checkout_step_key_unknown")
    if not capture:
        gaps.append("checkout_step_capture_missing")
    if not chain:
        gaps.append("checkout_chain_id_missing")
    if not in_target_package:
        gaps.append("checkout_step_outside_target_package")
    if high_risk_confirm:
        gaps.append("checkout_high_risk_confirm_not_advanced")
    return {
        "protocol_version": APP_EVIDENCE_ROLE_CONTRACT_VERSION,
        "evidence_role": ROLE_CHECKOUT_CHAIN_STEP,
        "chain_id": chain or None,
        "step_key": key or None,
        "step_index": max(0, int(step_index or 0)),
        "capture_id": capture or None,
        "in_target_package": bool(in_target_package),
        "high_risk_confirm": bool(high_risk_confirm),
        "counts_as_independent_business_arrival": bool(
            in_target_package and capture and key in _CHECKOUT_STEP_KEYS
        ),
        "external_payment_pages_excluded": True,
        "gap_codes": gaps,
        "complete": not gaps,
    }


def build_player_wake_before_capture_hint() -> dict[str, Any]:
    """采 media_state／播放器进度前先轻触画面，唤醒隐藏控件（43/44/ops_3/ops_9）。

    仅提供合同提示；执行端按 normalized_bounds 点一次，不得当成业务点击到达。
    """

    return {
        "protocol_version": APP_EVIDENCE_ROLE_CONTRACT_VERSION,
        "evidence_role": ROLE_PLAYER_SCRUB_SERIES,
        "wake_player_controls_before_capture": True,
        "counts_as_independent_business_arrival": False,
        "normalized_bounds": {
            "left": 0.42,
            "top": 0.42,
            "right": 0.58,
            "bottom": 0.58,
        },
        "purpose": "先轻触画面唤醒隐藏播放控件，再采 media_state／进度证据",
    }


def build_player_scrub_series_evidence(
    *,
    parent_capture_id: str,
    before_capture_id: str,
    during_capture_ids: Sequence[str],
    after_capture_id: str,
    control_semantic: str | None = None,
    gap_codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """播放器拖动／进度条连续证据：拖前／拖中／拖后多帧，单帧不得冒充完成。"""

    parent = str(parent_capture_id or "").strip()
    before = str(before_capture_id or "").strip()
    during = [str(item).strip() for item in during_capture_ids if str(item).strip()]
    after = str(after_capture_id or "").strip()
    gaps = [str(item).strip() for item in (gap_codes or ()) if str(item).strip()]
    if not parent:
        gaps.append("player_scrub_parent_missing")
    if not before:
        gaps.append("player_scrub_before_missing")
    if not during:
        gaps.append("player_scrub_during_missing")
    if not after:
        gaps.append("player_scrub_after_missing")
    if before and after and before == after and not during:
        gaps.append("player_scrub_single_frame_forbidden")
    return {
        "protocol_version": APP_EVIDENCE_ROLE_CONTRACT_VERSION,
        "evidence_role": ROLE_PLAYER_SCRUB_SERIES,
        "parent_capture_id": parent or None,
        "before_capture_id": before or None,
        "during_capture_ids": during,
        "after_capture_id": after or None,
        "control_semantic": str(control_semantic or "").strip() or None,
        "counts_as_independent_business_arrival": False,
        "single_frame_insufficient": True,
        "gap_codes": sorted(set(gaps)),
        "complete": not gaps,
    }


def validate_app_evidence_record(record: Mapping[str, Any] | None) -> tuple[str, ...]:
    """校验证据记录；返回缺口码（空＝通过）。未知角色记 gap，不抛。"""

    if not isinstance(record, Mapping):
        return ("evidence_record_missing",)
    gaps: list[str] = []
    version = str(record.get("protocol_version") or "").strip()
    if version and version != APP_EVIDENCE_ROLE_CONTRACT_VERSION:
        gaps.append("evidence_protocol_version_mismatch")
    role = str(record.get("evidence_role") or "").strip()
    bundle = str(record.get("evidence_bundle") or "").strip()
    if bundle == "ad_outbound_triplet":
        embedded = record.get("gap_codes")
        if isinstance(embedded, Sequence) and not isinstance(embedded, (str, bytes)):
            gaps.extend(str(item) for item in embedded if str(item).strip())
        if record.get("complete") is not True:
            if "ad_outbound_source_missing" not in gaps and not str(
                ((record.get("source") or {}) if isinstance(record.get("source"), Mapping) else {}).get(
                    "capture_id"
                )
                or ""
            ).strip():
                gaps.append("ad_outbound_source_missing")
        return tuple(dict.fromkeys(gaps))
    if not role:
        gaps.append("evidence_role_missing")
    elif role not in APP_EVIDENCE_ROLES:
        gaps.append("evidence_role_unknown")
    if role_is_derived_only(role) and record.get("forbids_as_judge_primary") is not True:
        gaps.append("derived_role_must_forbid_judge_primary")
    if record.get("counts_as_independent_business_arrival") is True and not role_counts_as_business_arrival(
        role
    ):
        gaps.append("evidence_role_forbids_business_arrival")
    if role == ROLE_LONG_PAGE_SCROLL_SERIES:
        if not str(record.get("parent_capture_id") or "").strip():
            gaps.append("long_page_parent_missing")
        frames = record.get("frame_capture_ids")
        if not isinstance(frames, Sequence) or isinstance(frames, (str, bytes)) or not frames:
            gaps.append("long_page_frames_missing")
    if role == ROLE_PLAYER_SCRUB_SERIES:
        embedded = record.get("gap_codes")
        if isinstance(embedded, Sequence) and not isinstance(embedded, (str, bytes)):
            gaps.extend(str(item) for item in embedded if str(item).strip())
        if record.get("complete") is not True and not gaps:
            gaps.append("player_scrub_incomplete")
    if role == ROLE_CHECKOUT_CHAIN_STEP:
        embedded = record.get("gap_codes")
        if isinstance(embedded, Sequence) and not isinstance(embedded, (str, bytes)):
            gaps.extend(str(item) for item in embedded if str(item).strip())
    embedded_gaps = record.get("gap_codes")
    if isinstance(embedded_gaps, Sequence) and not isinstance(embedded_gaps, (str, bytes)):
        for item in embedded_gaps:
            text = str(item).strip()
            if text:
                gaps.append(text)
    return tuple(dict.fromkeys(gaps))
