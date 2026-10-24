"""E11：把当前页命中规则收敛为离场证据需求集（EvidenceExitRequirementResolver）。

冻结依据：
- E01 条款 3.3：需求角色→供给角色映射与 artifact 别名归一只在本服务实现（缺陷 D1/C3 收口）
- E02 条款 1.6：required_artifacts 与规则级 quality_gate 是需求声明，由本服务收敛为需求集

本服务只输出需求声明（全部 pending），不输出任何质量结论；
齐全性裁决属清单侧（E07/E14），门禁与清单裁决读取同一需求集。
E12 由 EvidenceExitRequirementService 接通真实候选查询；
E13 由 publish_for_capture 写库并推进 revision：既有成员追加式保留（含状态），
新成员按 pending 追加，收敛结果无变化时幂等返回、不产生新 revision。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from services.api.app.application.evidence_exit_gate import (
    EvidenceExitRequirementRepository,
    EvidenceExitRequirementRevisionConflict,
    PersistedEvidenceExitRequirementSet,
)
from services.api.app.application.task_query import CandidateRule
from services.api.app.domain.evidence.vocabulary import (
    EvidenceVocabularyError,
    normalize_artifact_type,
    normalize_manifest_capture_role,
    normalize_requirement_capture_role,
)
from services.api.app.domain.exploration.evidence_exit_requirements import (
    ActiveSessionEvidenceRequirement,
    EvidenceExitRequirementSet,
    EvidenceExitRequirementStatus,
    build_active_session_evidence_requirement,
    build_evidence_exit_requirement_set,
)
from services.api.app.domain.protocol import stable_protocol_hash

MATCHER_SNAPSHOT_PROTOCOL_VERSION = "evidence-exit-requirement-matcher.v1"

# 规则未声明 required_capture_roles 时的缺省需求：判定至少要有主采集帧（E01 取值表 2.2）
_DEFAULT_REQUIRED_CAPTURE_ROLES = ("primary",)

_REQUIREMENT_PUBLISH_MAX_ATTEMPTS = 3

# E02 条款 1.6：规则级 quality_gate 键集封闭
_QUALITY_GATE_KEYS = frozenset({"min_screenshot_count", "allow_empty_visible_text"})


class EvidenceExitRequirementResolutionError(ValueError):
    """候选规则契约违反 E01/E02 冻结词汇或形状时抛出。"""


class EvidenceExitRequirementResolver:
    """把候选规则的证据契约收敛为一份可追溯到规则的需求快照。"""

    def resolve(
        self,
        *,
        task_id: str,
        session_id: str,
        capture_id: str,
        candidate_rules: Sequence[CandidateRule],
        created_at: datetime,
        revision: int = 1,
        capture_role: str | None = None,
    ) -> EvidenceExitRequirementSet:
        supply_role = _resolve_current_capture_role(capture_role)
        entries: list[dict[str, Any]] = []
        seen_rule_ids: set[str] = set()
        for rule in candidate_rules:
            rule_id = _require_text(rule.rule_id, "rule_id")
            if rule_id in seen_rule_ids:
                raise EvidenceExitRequirementResolutionError(f"候选规则 {rule_id} 重复")
            seen_rule_ids.add(rule_id)
            rule_code = _require_text(rule.rule_code, "rule_code")
            contract = _mapping(rule.input_contract, f"规则 {rule_code} 的 input_contract")
            entries.append(
                {
                    "rule_id": rule_id,
                    "rule_code": rule_code,
                    "required_artifact_types": _resolve_artifacts(contract, rule_code=rule_code),
                    "required_capture_roles": _resolve_roles(
                        contract,
                        rule_code=rule_code,
                        current_capture_role=supply_role,
                    ),
                    **_resolve_quality_gate(contract, rule_code=rule_code),
                }
            )
        entries.sort(key=lambda item: (item["rule_code"], item["rule_id"]))
        requirements = tuple(
            build_active_session_evidence_requirement(
                task_id=task_id,
                capture_id=capture_id,
                status=EvidenceExitRequirementStatus.PENDING,
                **entry,
            )
            for entry in entries
        )
        return build_evidence_exit_requirement_set(
            revision=revision,
            task_id=task_id,
            session_id=session_id,
            capture_id=capture_id,
            matcher_snapshot_hash=_matcher_snapshot_hash(entries),
            requirements=requirements,
            created_at=created_at,
        )


@runtime_checkable
class CandidateRuleSource(Protocol):
    """E12：需求集生成消费的规则候选查询端口（TaskQueryService 与其仓储均满足）。"""

    async def list_candidate_rules(self, *, task_id: str, page_type: str) -> list[CandidateRule]:
        """查询任务当前页面的候选规则。"""


class EvidenceExitRequirementService:
    """E12/E13：接通真实规则候选查询，生成并按 revision 写入需求集。"""

    def __init__(
        self,
        *,
        candidate_rules: CandidateRuleSource,
        resolver: EvidenceExitRequirementResolver | None = None,
        repository: EvidenceExitRequirementRepository | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._candidate_rules = candidate_rules
        self._resolver = resolver or EvidenceExitRequirementResolver()
        self._repository = repository
        self._clock = clock or _utc_now

    async def resolve_for_capture(
        self,
        *,
        task_id: str,
        session_id: str,
        capture_id: str,
        page_type: str,
        revision: int = 1,
        capture_role: str | None = None,
    ) -> EvidenceExitRequirementSet:
        candidates = await self._candidate_rules.list_candidate_rules(
            task_id=task_id,
            page_type=page_type,
        )
        return self._resolver.resolve(
            task_id=task_id,
            session_id=session_id,
            capture_id=capture_id,
            candidate_rules=candidates,
            created_at=self._clock(),
            revision=revision,
            capture_role=capture_role,
        )

    async def publish_for_capture(
        self,
        *,
        task_id: str,
        session_id: str,
        capture_id: str,
        page_type: str,
        capture_role: str | None = None,
    ) -> PersistedEvidenceExitRequirementSet:
        """E13：解析当前候选并按 revision 幂等写库。

        既有成员（含状态与终态结果）追加式保留，需求内容对本次 Capture 冻结：
        同 requirement_id 以库内为准，新成员按 pending 追加；
        收敛结果与最新快照完全一致时直接返回、不产生新 revision。
        """
        if self._repository is None:
            raise RuntimeError("EvidenceExitRequirementService 未配置需求集仓储，无法写库")
        for attempt in range(_REQUIREMENT_PUBLISH_MAX_ATTEMPTS):
            latest = await self._repository.get_latest(
                task_id=task_id,
                session_id=session_id,
                capture_id=capture_id,
            )
            resolved = await self.resolve_for_capture(
                task_id=task_id,
                session_id=session_id,
                capture_id=capture_id,
                page_type=page_type,
                revision=1 if latest is None else latest.revision + 1,
                capture_role=capture_role,
            )
            if latest is None:
                pending = resolved
            else:
                merged_members = _merge_requirement_members(
                    latest=latest.requirements,
                    resolved=resolved.requirements,
                )
                if (
                    merged_members == latest.requirements
                    and resolved.matcher_snapshot_hash == latest.matcher_snapshot_hash
                ):
                    return PersistedEvidenceExitRequirementSet(
                        requirement_set=latest,
                        created=False,
                    )
                pending = build_evidence_exit_requirement_set(
                    revision=latest.revision + 1,
                    task_id=task_id,
                    session_id=session_id,
                    capture_id=capture_id,
                    matcher_snapshot_hash=resolved.matcher_snapshot_hash,
                    requirements=merged_members,
                    created_at=resolved.created_at,
                )
            try:
                return await self._repository.add(requirement_set=pending)
            except EvidenceExitRequirementRevisionConflict:
                if attempt + 1 >= _REQUIREMENT_PUBLISH_MAX_ATTEMPTS:
                    raise
        raise AssertionError('需求集发布重试循环未返回')


def _merge_requirement_members(
    *,
    latest: Sequence[ActiveSessionEvidenceRequirement],
    resolved: Sequence[ActiveSessionEvidenceRequirement],
) -> tuple[ActiveSessionEvidenceRequirement, ...]:
    carried_ids = {item.requirement_id for item in latest}
    members = [
        *latest,
        *(item for item in resolved if item.requirement_id not in carried_ids),
    ]
    members.sort(key=lambda item: (item.rule_code, item.rule_id, item.requirement_id))
    return tuple(members)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _matcher_snapshot_hash(entries: Sequence[Mapping[str, Any]]) -> str:
    """匹配视图哈希：与 revision 无关，供 E13 判断候选收敛结果是否变化。"""
    return stable_protocol_hash(
        {
            "protocol_version": MATCHER_SNAPSHOT_PROTOCOL_VERSION,
            "rules": [
                {
                    "rule_id": item["rule_id"],
                    "rule_code": item["rule_code"],
                    "required_artifact_types": list(item["required_artifact_types"]),
                    "required_capture_roles": list(item["required_capture_roles"]),
                    "min_screenshot_count": item["min_screenshot_count"],
                    "allow_empty_visible_text": item["allow_empty_visible_text"],
                }
                for item in entries
            ],
        }
    )


def _resolve_artifacts(contract: Mapping[str, Any], *, rule_code: str) -> tuple[str, ...]:
    values = _string_list(
        contract.get("required_artifacts"),
        f"规则 {rule_code} 的 required_artifacts",
    )
    try:
        return tuple(sorted({normalize_artifact_type(item) for item in values}))
    except EvidenceVocabularyError as exc:
        raise EvidenceExitRequirementResolutionError(f"规则 {rule_code}: {exc}") from exc


def _resolve_current_capture_role(value: str | None) -> str | None:
    """校验调用方给出的本次采集角色；缺省时不做主帧归一。"""
    if value is None:
        return None
    try:
        return normalize_manifest_capture_role(value)
    except EvidenceVocabularyError as exc:
        raise EvidenceExitRequirementResolutionError(str(exc)) from exc


def _resolve_roles(
    contract: Mapping[str, Any],
    *,
    rule_code: str,
    current_capture_role: str | None = None,
) -> tuple[str, ...]:
    values = _string_list(
        contract.get("required_capture_roles"),
        f"规则 {rule_code} 的 required_capture_roles",
    )
    if not values:
        values = list(_DEFAULT_REQUIRED_CAPTURE_ROLES)
    try:
        roles = {normalize_requirement_capture_role(item) for item in values}
    except EvidenceVocabularyError as exc:
        raise EvidenceExitRequirementResolutionError(f"规则 {rule_code}: {exc}") from exc
    if current_capture_role is not None:
        # E01 条款 3.3「需求可由复用既有采集满足」：E14 按单 Capture 对账，本帧清单
        # 只可能携带本帧这一个供给角色，离场需求因此只能要求「本帧必须齐」。
        # 跨帧角色不是离场前置：来源/动作前帧由采集配对与交接库存对账保证，
        # 重载帧、attempt_N 等由后续动作产生——拿未来的帧阻止当前离场是死锁；
        # 判定期的跨帧取证由 Judge 侧角色解析负责，缺帧落证据不足终态。
        roles = {current_capture_role}
    return tuple(sorted(roles))


def _resolve_quality_gate(contract: Mapping[str, Any], *, rule_code: str) -> dict[str, Any]:
    raw = contract.get("quality_gate")
    if raw is None:
        gate: Mapping[str, Any] = {}
    elif isinstance(raw, Mapping):
        gate = raw
    else:
        raise EvidenceExitRequirementResolutionError(f"规则 {rule_code} 的 quality_gate 必须为对象")
    unknown = set(gate) - _QUALITY_GATE_KEYS
    if unknown:
        raise EvidenceExitRequirementResolutionError(
            f"规则 {rule_code} 的 quality_gate 键超出 E02 冻结键集: {sorted(unknown)}"
        )
    min_count = gate.get("min_screenshot_count", 0)
    allow_empty = gate.get("allow_empty_visible_text", True)
    if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 0:
        raise EvidenceExitRequirementResolutionError(
            f"规则 {rule_code} 的 min_screenshot_count 必须为非负整数"
        )
    if not isinstance(allow_empty, bool):
        raise EvidenceExitRequirementResolutionError(
            f"规则 {rule_code} 的 allow_empty_visible_text 必须为布尔值"
        )
    return {"min_screenshot_count": min_count, "allow_empty_visible_text": allow_empty}


def _require_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EvidenceExitRequirementResolutionError(f"{field_name} 不能为空")
    return text


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceExitRequirementResolutionError(f"{field_name} 必须为对象")
    return value


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EvidenceExitRequirementResolutionError(f"{field_name} 必须为数组")
    items = tuple(str(item).strip() for item in value)
    if any(not item for item in items):
        raise EvidenceExitRequirementResolutionError(f"{field_name} 不能包含空值")
    return items
