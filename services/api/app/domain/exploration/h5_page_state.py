"""H5 页面状态的通用语义分类。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_OVERLAY_MARKERS = frozenset({"popup", "modal", "overlay", "dialog", "obstructed"})
_CHALLENGE_MARKERS = frozenset({"captcha", "challenge", "mfa", "otp", "2fa", "verification"})
_ABSENCE_MARKERS = frozenset(
    {"absent", "cleared", "closed", "dismissed", "none", "not", "removed", "without"}
)


@dataclass(frozen=True, kw_only=True)
class H5PageStateSemantics:
    """保留模型原始状态，同时给编排层提供稳定的通用事实。"""

    raw: str
    normalized: str
    tokens: frozenset[str]
    has_overlay_marker: bool
    challenge: bool
    absent: bool

    @property
    def has_overlay(self) -> bool:
        """当前状态表达了仍存在的遮挡层。"""
        return self.has_overlay_marker and not self.absent

    @property
    def supports_popup_dismissal(self) -> bool:
        """页面状态不阻止执行已验签的低风险弹窗关闭能力。"""
        return self.has_overlay and not self.challenge


def classify_h5_page_state(value: object) -> H5PageStateSemantics:
    """把精确状态和由通用 token 组成的复合状态归入同一语义。"""
    raw = str(value or "").strip()
    normalized = unicodedata.normalize("NFKC", raw).strip().casefold()
    tokens = frozenset(item for item in re.split(r"[^a-z0-9]+", normalized) if item)
    return H5PageStateSemantics(
        raw=raw,
        normalized=normalized,
        tokens=tokens,
        has_overlay_marker=(
            normalized in _OVERLAY_MARKERS or bool(tokens & _OVERLAY_MARKERS)
        ),
        challenge=bool(tokens & _CHALLENGE_MARKERS),
        absent=bool(tokens & _ABSENCE_MARKERS),
    )


def h5_page_state_has_overlay(value: object) -> bool:
    """判断状态是否表达仍存在的遮挡层。"""
    return classify_h5_page_state(value).has_overlay


def h5_page_state_supports_popup_dismissal(value: object) -> bool:
    """判断状态是否允许继续复核受信弹窗关闭能力。"""
    return classify_h5_page_state(value).supports_popup_dismissal


__all__ = [
    "H5PageStateSemantics",
    "classify_h5_page_state",
    "h5_page_state_has_overlay",
    "h5_page_state_supports_popup_dismissal",
]
