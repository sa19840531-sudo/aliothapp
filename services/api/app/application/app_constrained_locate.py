"""③ 约束输出契约：无状态辨识结果校验（不操作设备）。

status 仅允许 found / not_found / uncertain。
found 必须带允许的 target 与合法归一化坐标；其余 status 的 coordinate 必须为 null。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

LocateStatus = Literal["found", "not_found", "uncertain"]
_ALLOWED_STATUS = frozenset({"found", "not_found", "uncertain"})


@dataclass(frozen=True, kw_only=True)
class ConstrainedLocateResult:
    status: LocateStatus
    target: str | None = None
    label: str | None = None
    coordinate: tuple[float, float] | None = None
    reason: str | None = None
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _ALLOWED_STATUS:
            raise ConstrainedLocateError("invalid status")
        if self.candidate_id is not None and (
            not isinstance(self.candidate_id, str) or not self.candidate_id.strip()
        ):
            raise ConstrainedLocateError("invalid candidate_id")
        if self.status == "found":
            if not self.target or (self.coordinate is None and self.candidate_id is None):
                raise ConstrainedLocateError("found requires target and locator")
            if self.coordinate is not None and not _in_unit_square(self.coordinate):
                raise ConstrainedLocateError("coordinate out of bounds")
        elif self.coordinate is not None or self.candidate_id is not None:
            raise ConstrainedLocateError("non-found result cannot carry locator")

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target,
            "label": self.label,
            "coordinate": (
                [float(self.coordinate[0]), float(self.coordinate[1])]
                if self.coordinate is not None
                else None
            ),
            "reason": self.reason,
            **({"candidate_id": self.candidate_id} if self.candidate_id else {}),
        }


class ConstrainedLocateError(ValueError):
    """契约校验失败：主流程不得据此点击。"""


def validate_constrained_locate(
    payload: object,
    *,
    allowed_targets: Sequence[str],
) -> ConstrainedLocateResult:
    """严格校验模型／规则输出；拒绝未知字段与非法坐标。"""

    if not isinstance(payload, Mapping):
        raise ConstrainedLocateError("payload must be object")
    allowed = {
        str(item).strip() for item in allowed_targets if str(item or "").strip()
    }
    if not allowed:
        raise ConstrainedLocateError("allowed_targets required")
    known = {"status", "target", "label", "coordinate", "reason", "candidate_id"}
    extra = set(payload) - known
    if extra:
        raise ConstrainedLocateError(f"unknown fields: {sorted(extra)}")
    status_raw = payload.get("status")
    if status_raw not in _ALLOWED_STATUS:
        raise ConstrainedLocateError("status must be found|not_found|uncertain")
    status: LocateStatus = status_raw  # type: ignore[assignment]
    target = _optional_text(payload.get("target"))
    label = _optional_text(payload.get("label"))
    reason = _optional_text(payload.get("reason"))
    coordinate = _parse_coordinate(payload.get("coordinate"))
    candidate_id = payload.get("candidate_id")
    if candidate_id is not None and (
        not isinstance(candidate_id, str) or not candidate_id.strip()
    ):
        raise ConstrainedLocateError("invalid candidate_id")
    if status == "found":
        if target is None or target not in allowed:
            raise ConstrainedLocateError("found requires allowed target")
        if coordinate is None and candidate_id is None:
            raise ConstrainedLocateError("found requires coordinate or candidate_id")
        if coordinate is not None and not _in_unit_square(coordinate):
            raise ConstrainedLocateError("coordinate out of bounds")
        return ConstrainedLocateResult(
            status=status,
            target=target,
            label=label,
            coordinate=coordinate,
            reason=reason,
            candidate_id=candidate_id,
        )
    if coordinate is not None or candidate_id is not None:
        raise ConstrainedLocateError(f"{status} requires coordinate=null")
    if status == "uncertain" and not reason:
        raise ConstrainedLocateError("uncertain requires reason")
    return ConstrainedLocateResult(
        status=status,
        target=target if target in allowed else None,
        label=label,
        coordinate=None,
        reason=reason,
    )


def center_from_normalized_bounds(
    bounds: Mapping[str, object] | None,
) -> tuple[float, float] | None:
    if not isinstance(bounds, Mapping):
        return None
    try:
        left = float(bounds["left"])
        top = float(bounds["top"])
        right = float(bounds["right"])
        bottom = float(bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    return ((left + right) / 2.0, (top + bottom) / 2.0)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_coordinate(value: object) -> tuple[float, float] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConstrainedLocateError("coordinate must be [x, y] or null")
    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, ValueError) as exc:
        raise ConstrainedLocateError("coordinate must be numeric") from exc
    return (x, y)


def _in_unit_square(coordinate: tuple[float, float]) -> bool:
    x, y = coordinate
    return 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
