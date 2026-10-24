"""H5 业务页面弹窗处理轮次的纯应用策略。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.exploration.h5_frontier import (
    H5_POPUP_SIGNATURE_ATTEMPT_LIMIT,
    H5FrontierEntryStatus,
    H5TaskFrontierEntry,
    H5TaskFrontierSnapshot,
    canonical_h5_route_identity,
    retired_h5_popup_attempt_count,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)

# 页面累计失败的兜底，不限制已经确认成功关闭的弹层数量。
H5_BUSINESS_PAGE_POPUP_LIMIT = 20

# 同一签名弹窗（关闭被策略阻断/关闭后原样存在）连续处理达到该次数即判僵持：
# 阻断轮次的计数器随探索工作重发归零，必须从 Frontier 快照跨工作累计。
H5_SAME_POPUP_SIGNATURE_LIMIT = H5_POPUP_SIGNATURE_ATTEMPT_LIMIT

# 弹窗关闭在「失败重观察」路径上的跨后继工作连续上限：无关闭控件的弹窗
# （如无 X 的身份凭证卡/新人特惠底部弹层）关闭动作报 POPUP_DISMISSAL_NOT_FOUND，
# 走失败重观察另开后继工作，每次计数归零 → 无限重观察卡死（91PORN 线上实锤）。
# 达到该次数即放弃该页，不再重观察。
H5_POPUP_DISMISSAL_REOBSERVE_LIMIT = 3

# 签名无关的连续弹窗关闭失败上限：带倒计时/轮播素材的弹窗每次观察签名都漂移
# （91PORN「新人特惠」实锤：2551fbb7→2ba5f9fc→… 逐轮不同），按签名计数永远
# 到不了同签名上限；只要连续失败的都是「关闭当前弹窗」，达到该次数同样止损。
H5_POPUP_DISMISSAL_STREAK_LIMIT = 4
H5_POPUP_DISMISSAL_STALLED_REASON_CODE = "popup_dismissal_stalled"
H5_POPUP_LAYER_LIMIT_REASON_CODE = "popup_layer_limit_exceeded"

_TRANSIENT_PAGE_TYPES = frozenset({"line_loading", "startup_ad"})
_POPUP_DISMISSAL_TARGET = "关闭当前弹窗"
_OBSTRUCTION_SIGNATURE_PATTERN = re.compile(r"h5-obstruction:[0-9a-f]{64}")
_COUNTED_TERMINAL_STATUSES = frozenset(
    {
        H5FrontierEntryStatus.FAILED,
        H5FrontierEntryStatus.BLOCKED,
        H5FrontierEntryStatus.SKIPPED,
    }
)


@dataclass(frozen=True, slots=True)
class H5BusinessPagePopupLoopState:
    """同一业务页面上累计的真正无效弹窗处理轮次。

    reason_code：``popup_layer_limit_exceeded``=页面累计无效轮次超限；
    ``popup_dismissal_stalled``=同一签名弹窗连续处理无效（关闭能力对它失效）。
    """

    page_key: str
    completed_count: int
    limit: int
    limit_reached: bool
    reason_code: str = H5_POPUP_LAYER_LIMIT_REASON_CODE


def build_h5_business_page_key(location: str, page_type: str | None) -> str | None:
    """把稳定 HTTP(S) 地址与业务页类型组合为可审计页面身份。"""
    normalized_page_type = _normalized_page_type(page_type)
    if normalized_page_type is None or normalized_page_type in _TRANSIENT_PAGE_TYPES:
        return None
    normalized_location = _normalized_http_location(location)
    if normalized_location is None:
        return None
    return f"{normalized_location}|{normalized_page_type}"


def evaluate_h5_business_page_popup_loop(
    *,
    snapshot: H5TaskFrontierSnapshot,
    location: str,
    page_type: str | None,
    current_candidates: Sequence[CandidateAction],
) -> H5BusinessPagePopupLoopState | None:
    """仅在当前仍有可信弹窗关闭候选时计算页面级轮次。"""
    page_key = build_h5_business_page_key(location, page_type)
    if page_key is None:
        return None
    current_signatures = {
        _candidate_obstruction_signature(candidate)
        for candidate in current_candidates
        if _is_trusted_popup_candidate(candidate)
    }
    current_signatures.discard(None)
    if not current_signatures:
        return None
    page_attempts = [
        entry
        for entry in snapshot.entries
        if _is_completed_popup_attempt(entry) and _entry_page_key(entry) == page_key
    ]
    # 同签名僵持优先判定：关闭被阻断/关闭无效时，每轮重观察都会重新签发同一
    # 候选，页面总轮次上限之前就该止损（线上实测每轮约 20 秒）。
    stalled_count = max(
        (
            sum(
                1
                for entry in page_attempts
                if _candidate_obstruction_signature(entry.candidate) == signature
            )
            + retired_h5_popup_attempt_count(
                snapshot,
                page_key=page_key,
                obstruction_signature=signature,
            )
            for signature in current_signatures
        ),
        default=0,
    )
    if stalled_count >= H5_SAME_POPUP_SIGNATURE_LIMIT:
        return H5BusinessPagePopupLoopState(
            page_key=page_key,
            completed_count=stalled_count,
            limit=H5_SAME_POPUP_SIGNATURE_LIMIT,
            limit_reached=True,
            reason_code=H5_POPUP_DISMISSAL_STALLED_REASON_CODE,
        )
    completed_count = len(page_attempts) + retired_h5_popup_attempt_count(
        snapshot,
        page_key=page_key,
    )
    return H5BusinessPagePopupLoopState(
        page_key=page_key,
        completed_count=completed_count,
        limit=H5_BUSINESS_PAGE_POPUP_LIMIT,
        limit_reached=completed_count >= H5_BUSINESS_PAGE_POPUP_LIMIT,
    )


def _entry_page_key(entry: H5TaskFrontierEntry) -> str | None:
    source_page_type = entry.candidate.metadata.get("source_page_type")
    return build_h5_business_page_key(
        entry.source_location,
        source_page_type if isinstance(source_page_type, str) else None,
    )


def _is_completed_popup_attempt(entry: H5TaskFrontierEntry) -> bool:
    return (
        entry.status in _COUNTED_TERMINAL_STATUSES
        and entry.selected_at is not None
        and entry.finished_at is not None
        and _is_trusted_popup_candidate(entry.candidate)
        and not (entry.last_failure_reason or "").endswith(
            ("LOGICAL_SOURCE_STATE_STALE", "POPUP_MODAL_SCOPE_MISMATCH")
        )
    )


def _candidate_obstruction_signature(candidate: CandidateAction) -> str | None:
    """读取候选携带的遮挡签名；仅对可信弹窗候选有意义。"""
    signature = _optional_text(candidate.metadata.get("obstruction_signature"))
    if signature is not None:
        return signature
    parameters = candidate.metadata.get("action_parameters")
    if isinstance(parameters, Mapping):
        return _optional_text(parameters.get("obstruction_signature"))
    return None


def next_popup_dismissal_reobserve_state(
    *,
    prior_state: object,
    candidate: CandidateAction,
    error_code: str | None = None,
) -> dict[str, Any] | None:
    """给失败重观察候选算弹窗关闭的跨后继连续次数。

    仅对语义目标为「关闭当前弹窗」的候选计数。两个维度：
    - ``count``：同一遮挡签名的连续次数（签名变化则重置）——固定弹窗更快止损；
    - ``streak``：签名无关的连续弹窗关闭失败次数——倒计时/轮播弹窗签名逐轮漂移，
      只有这个维度数得到它。
    非弹窗关闭候选返回 None（调用方据此不写入状态，两个计数自然归零）。
    """
    if candidate.semantic_target != _POPUP_DISMISSAL_TARGET:
        return None
    if error_code in {"LOGICAL_SOURCE_STATE_STALE", "POPUP_MODAL_SCOPE_MISMATCH"}:
        # 未执行点击的刷新保留真实失败次数，另由来源刷新/调度预算限制。
        return dict(prior_state) if isinstance(prior_state, Mapping) else None
    signature = _candidate_obstruction_signature(candidate)
    prior_count = 0
    prior_streak = 0
    if isinstance(prior_state, Mapping):
        raw_streak = prior_state.get("streak", 0)
        if isinstance(raw_streak, int) and not isinstance(raw_streak, bool):
            prior_streak = raw_streak
        if prior_state.get("signature") == signature:
            raw = prior_state.get("count", 0)
            if isinstance(raw, int) and not isinstance(raw, bool):
                prior_count = raw
    return {
        "signature": signature,
        "count": prior_count + 1,
        "streak": prior_streak + 1,
    }


def popup_dismissal_reobserve_exhausted(state: object) -> tuple[int, int] | None:
    """弹窗关闭失败重观察是否已达放弃上限；已达返回（连续次数, 对应上限），否则 None。"""
    if not isinstance(state, Mapping):
        return None
    count = state.get("count", 0)
    if (
        isinstance(count, int)
        and not isinstance(count, bool)
        and count >= H5_POPUP_DISMISSAL_REOBSERVE_LIMIT
    ):
        return count, H5_POPUP_DISMISSAL_REOBSERVE_LIMIT
    streak = state.get("streak", 0)
    if (
        isinstance(streak, int)
        and not isinstance(streak, bool)
        and streak >= H5_POPUP_DISMISSAL_STREAK_LIMIT
    ):
        return streak, H5_POPUP_DISMISSAL_STREAK_LIMIT
    return None


def _is_trusted_popup_candidate(candidate: CandidateAction) -> bool:
    if (
        candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or candidate.semantic_target != _POPUP_DISMISSAL_TARGET
    ):
        return False
    metadata = candidate.metadata
    keywords = _text_set(metadata.get("target_keywords"))
    parameters = metadata.get("action_parameters")
    if not isinstance(parameters, Mapping):
        return False
    capability = _optional_text(parameters.get("popup_dismissal_capability"))
    signature = _optional_text(metadata.get("obstruction_signature"))
    parameter_signature = _optional_text(parameters.get("obstruction_signature"))
    return (
        parameters.get("target_mode") == "popup_dismissal"
        and capability is not None
        and {"popup_dismissal", "close_control", capability}.issubset(keywords)
        and signature is not None
        and signature == parameter_signature
        and _OBSTRUCTION_SIGNATURE_PATTERN.fullmatch(signature) is not None
    )


def _normalized_page_type(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized or None


def _normalized_http_location(value: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
        scheme = parts.scheme.casefold()
        hostname = parts.hostname
        _port = parts.port
    except ValueError:
        return None
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or not hostname
        or any(character.isspace() for character in hostname)
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    return canonical_h5_route_identity(value)


def _text_set(value: object) -> frozenset[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return frozenset()
    return frozenset(
        normalized for item in value if (normalized := _optional_text(item)) is not None
    )


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
