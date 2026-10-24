"""从当前持久化候选恢复 Official 执行边界的可信控件语义。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Protocol

from services.api.app.domain.exploration import AppVisualCandidate
from services.api.app.domain.interaction import (
    ActionIntent,
    CommandPolicyError,
    InteractionOperation,
    InteractionRiskLevel,
)


class AppResolvedTargetReader(Protocol):
    """仅从平台事实源读取同任务、同会话、同截图的候选。"""

    async def read_resolved_candidate(
        self,
        *,
        task_id: str,
        session_id: str,
        capture_id: str,
        candidate_id: str,
        package_name: str,
    ) -> AppVisualCandidate | None: ...


async def read_verified_target(
    *,
    reader: AppResolvedTargetReader | None,
    intent: ActionIntent,
    context: Mapping[str, object],
    package_name: str | None,
) -> AppVisualCandidate | None:
    """候选身份、文案和实际触点均匹配时才使用其受信角色。"""

    candidate_id = context.get("resolved_candidate_id")
    clicks = {InteractionOperation.CLICK, InteractionOperation.TAP}
    if (
        reader is None
        or intent.action_type not in clicks
        or not isinstance(candidate_id, str)
        or not candidate_id.strip()
        or not package_name
    ):
        return None
    # point 在设备参数归一化时优先于 resolved_target，不能校验一个点却执行另一个。
    if isinstance(intent.parameters.get("point"), Mapping):
        return None
    point = intent.parameters.get("resolved_target")
    if not isinstance(point, Mapping) or point.get("space") != "capture_px":
        return None
    candidate = await reader.read_resolved_candidate(
        task_id=intent.task_id,
        session_id=intent.session_id,
        capture_id=intent.source_capture_id,
        candidate_id=candidate_id,
        package_name=package_name,
    )
    if (
        candidate is None
        or candidate.candidate_id != candidate_id
        or candidate.source_capture_id != intent.source_capture_id
        or candidate.action_type not in clicks
        or candidate.normalized_bounds is None
        or (intent.semantic_target or "").strip().casefold()
        != candidate.semantic_name.strip().casefold()
    ):
        return None
    for key, expected in (
        ("base_width", candidate.source_width),
        ("base_height", candidate.source_height),
    ):
        value = point.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value != expected:
            return None
    x, y = point.get("x"), point.get("y")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in (x, y)
    ):
        return None
    bounds = candidate.normalized_bounds
    if not (
        bounds.left <= x / candidate.source_width <= bounds.right
        and bounds.top <= y / candidate.source_height <= bounds.bottom
    ):
        return None
    ranks = {
        InteractionRiskLevel.LOW: 0,
        InteractionRiskLevel.MEDIUM: 1,
        InteractionRiskLevel.HIGH: 2,
    }
    if ranks[intent.risk_level] < ranks[candidate.risk_level]:
        raise CommandPolicyError(
            "动作自报风险低于当前持久化候选风险",
            error_code="COMMAND_TARGET_RISK_UNDERRATED",
        )
    return candidate
