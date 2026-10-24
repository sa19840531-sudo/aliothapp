"""任务收口阶段封存 EvidenceInputSet 并发布聚合质检工作。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from services.api.app.application.aggregate_inspection_work_items import (
    PublishAggregateInspectionWorkItemCommand,
)
from services.api.app.application.aggregate_rule_config import (
    RuleEvidenceInputConfiguration,
    validate_enabled_aggregate_rule_configuration,
)
from services.api.app.application.evidence_presentation import (
    rule_accepts_diagnostic_evidence,
    rule_evidence_layer_target,
)
from services.api.app.application.inspection_work_items import (
    build_inspection_rule_contract_hash,
)
from services.api.app.domain.evidence import (
    EvidenceInputCandidate,
    EvidenceInputScope,
    seal_evidence_input_set,
)
from services.api.app.domain.evidence.presentation import (
    PRESENTATION_PROTOCOL_VERSION,
    PresentationAssessment,
    evaluate_presentation_for_rule,
)
from services.api.app.domain.inspection.rule_scope import (
    RuleScopeAuthority,
    RuleScopePlan,
    RuleScopeStatus,
    build_rule_scope_plan,
)
from services.api.app.domain.protocol import stable_protocol_hash


@dataclass(frozen=True, kw_only=True)
class AggregateRuleSource:
    """当前任务锁定规则集中一条显式聚合规则。"""

    rule_id: str
    rule_code: str
    rule_payload: Mapping[str, Any]
    applicable_page_types: tuple[str, ...]
    evidence: RuleEvidenceInputConfiguration


@dataclass(frozen=True, kw_only=True)
class AggregateEvidenceSource:
    """可进入聚合封存选择的一项 PASS 主证据。"""

    candidate: EvidenceInputCandidate
    page_type: str | None
    # 采集的展示层评估；None 表示历史采集没有该事实，封存时不因此拒绝。
    presentation: PresentationAssessment | None = None


@dataclass(frozen=True, kw_only=True)
class AggregatePublicationSnapshot:
    """一次 Finalization Epoch 的权威规则与主证据快照。"""

    task_id: str
    source_agent_run_id: str
    finalization_epoch: int
    ruleset_snapshot_hash: str
    rules: tuple[AggregateRuleSource, ...]
    evidence: tuple[AggregateEvidenceSource, ...]
    scope_authority: RuleScopeAuthority | None = None
    checkpoint_journeys: bool = False

    def __post_init__(self) -> None:
        authority = self.scope_authority
        if authority is None:
            return
        if (
            authority.task_id != self.task_id
            or authority.source_agent_run_id != self.source_agent_run_id
            or authority.finalization_epoch != self.finalization_epoch
        ):
            raise ValueError("规则范围权威来源必须绑定同一 Task/Run/Epoch")


@dataclass(frozen=True, kw_only=True)
class AggregateInspectionPublicationSummary:
    """收口预处理的幂等结果。"""

    expected_rule_count: int
    published_work_item_count: int
    not_applicable_count: int
    evidence_insufficient_count: int


class AggregateInspectionPublicationRepository(Protocol):
    async def load_snapshot(
        self,
        *,
        task_id: str,
        source_agent_run_id: str,
        finalization_epoch: int,
    ) -> AggregatePublicationSnapshot: ...

    async def persist_published(
        self,
        *,
        input_set: Any,
        command: PublishAggregateInspectionWorkItemCommand,
        expected_member_count: int,
        scope_plan: RuleScopePlan | None,
    ) -> bool: ...

    async def persist_disposition(
        self,
        *,
        snapshot: AggregatePublicationSnapshot,
        rule: AggregateRuleSource,
        status: str,
        expected_member_count: int,
        code: str,
        message: str,
        scope_plan: RuleScopePlan | None,
    ) -> None: ...


class AggregateInspectionPublicationService:
    """只在 session_draining 下产生聚合规则的期望候选和正式 Work。"""

    def __init__(
        self,
        *,
        repository: AggregateInspectionPublicationRepository,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须大于 0")
        self._repository = repository
        self._max_attempts = max_attempts

    async def prepare_app_recovery(
        self, *, task_id: str, agent_run_id: str, finalization_epoch: int,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> bool:
        """只在正式快照链已排空后补发聚合，不能借恢复延长缺证僵死任务。"""

        ready = getattr(self._repository, "is_app_recovery_ready", None)
        if not callable(ready) or not await ready(
            task_id=task_id, source_agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        ):
            return False
        await self.prepare(
            task_id=task_id, agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            recovery_lease_token=recovery_lease_token,
            recovery_generation=recovery_generation,
            recovery_lease_expires_at=recovery_lease_expires_at,
        )
        return True

    async def prepare(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> AggregateInspectionPublicationSummary:
        fence = _recovery_fence_kwargs(
            recovery_lease_token,
            recovery_generation,
            recovery_lease_expires_at,
        )
        snapshot = await self._repository.load_snapshot(
            task_id=task_id,
            source_agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        published = 0
        not_applicable = 0
        evidence_insufficient = 0
        for rule in snapshot.rules:
            journey_selection = None
            scope_plan = (
                build_rule_scope_plan(
                    authority=snapshot.scope_authority,
                    rule_id=rule.rule_id,
                    applicable_page_types=rule.applicable_page_types,
                )
                if snapshot.scope_authority is not None
                else None
            )
            if snapshot.checkpoint_journeys and rule.evidence.scope is EvidenceInputScope.JOURNEY:
                loader = getattr(self._repository, "load_journey_selection", None)
                journey_selection = await loader(snapshot=snapshot, rule=rule) if loader else None
                selected = tuple(journey_selection["evidence"]) if journey_selection else ()
                disposition = None if selected else (
                    "evidence_insufficient", "JOURNEY_CHECKPOINT_INPUT_INCOMPLETE",
                    "未取得同一冻结规则合同的完整检查点证据，不能借用普通页面路径",
                )
            else:
                selected, disposition = _select_rule_evidence(
                    snapshot.evidence, rule=rule, scope_plan=scope_plan,
                )
            if disposition is not None:
                status, code, message = disposition
                await self._repository.persist_disposition(
                    snapshot=snapshot,
                    rule=rule,
                    status=status,
                    expected_member_count=len(selected),
                    code=code,
                    message=message,
                    scope_plan=scope_plan,
                    **fence,
                )
                if status == "not_applicable":
                    not_applicable += 1
                else:
                    evidence_insufficient += 1
                continue
            rule_contract_hash = build_inspection_rule_contract_hash(rule.rule_payload)
            eligible_capture_ids = tuple(item.candidate.manifest.capture_id for item in selected)
            selection_payload = {
                "scope": rule.evidence.scope.value,
                "scope_key": rule.evidence.scope_key,
                "capture_ids": list(eligible_capture_ids),
                "manifest_hashes": [item.candidate.manifest.manifest_hash for item in selected],
                "rule_scope_plan_hash": (scope_plan.plan_hash if scope_plan is not None else None),
            }
            if journey_selection is not None:
                selection_payload["journey_selection_hash"] = journey_selection["selection_hash"]
            selection_hash = stable_protocol_hash(selection_payload)
            input_set = seal_evidence_input_set(
                task_id=snapshot.task_id,
                source_agent_run_id=snapshot.source_agent_run_id,
                rule_id=rule.rule_id,
                rule_code=rule.rule_code,
                rule_contract_hash=rule_contract_hash,
                ruleset_snapshot_hash=snapshot.ruleset_snapshot_hash,
                scope=rule.evidence.scope,
                scope_key=rule.evidence.scope_key,
                finalization_epoch=snapshot.finalization_epoch,
                eligibility_snapshot_id=(
                    f"aggregate-eligibility:{snapshot.task_id}:"
                    f"{snapshot.finalization_epoch}:{rule.rule_id}"
                ),
                eligibility_snapshot_hash=selection_hash,
                selection_policy_hash=stable_protocol_hash(
                    {
                        "protocol_version": "aggregate-selection-policy.v2",
                        "presentation_gate": PRESENTATION_PROTOCOL_VERSION,
                        "exclude_capture_roles": ["supplemental"],
                        "require_latest_manifest_quality": "pass",
                        "applicable_page_types": list(rule.applicable_page_types),
                        "scope": rule.evidence.scope.value,
                        "rule_scope_plan_hash": (
                            scope_plan.plan_hash if scope_plan is not None else None
                        ),
                    }
                ),
                eligible_capture_ids=eligible_capture_ids,
                candidates=tuple(item.candidate for item in selected),
                sealed_at=datetime.now(UTC),
            )
            created = await self._repository.persist_published(
                input_set=input_set,
                command=PublishAggregateInspectionWorkItemCommand(
                    task_id=snapshot.task_id,
                    evidence_input_set_id=input_set.input_set_id,
                    producer_agent_run_id=snapshot.source_agent_run_id,
                    rule_id=rule.rule_id,
                    rule_code=rule.rule_code,
                    rule_contract_hash=rule_contract_hash,
                    scope=rule.evidence.scope,
                    scope_key=rule.evidence.scope_key,
                    finalization_epoch=snapshot.finalization_epoch,
                    max_attempts=self._max_attempts,
                ),
                expected_member_count=len(selected),
                scope_plan=scope_plan,
                **(
                    {"journey_selection": journey_selection}
                    if journey_selection is not None else {}
                ),
                **fence,
            )
            published += int(created)
        return AggregateInspectionPublicationSummary(
            expected_rule_count=len(snapshot.rules),
            published_work_item_count=published,
            not_applicable_count=not_applicable,
            evidence_insufficient_count=evidence_insufficient,
        )


def _recovery_fence_kwargs(
    token: str | None,
    generation: int | None,
    expires_at: datetime | None,
) -> dict[str, object]:
    if token is None:
        return {}
    return {
        "recovery_lease_token": token,
        "recovery_generation": generation,
        "recovery_lease_expires_at": expires_at,
    }


def _select_rule_evidence(
    evidence: Sequence[AggregateEvidenceSource],
    *,
    rule: AggregateRuleSource,
    scope_plan: RuleScopePlan | None = None,
) -> tuple[
    tuple[AggregateEvidenceSource, ...],
    tuple[str, str, str] | None,
]:
    # 只有冻结的规则范围计划能证明「不适用」；应检却没有可信可用证据的，一律按「证据不足」
    # 收口（仓储同一事务写证据不足的规则执行与待复核项），不能借用不适用，也不能停在待复核。
    # 未识别页型的技术性捕获（导航前置壳）不携带可判定证据，一律不入选；
    # 未配置适用页型的通用聚合规则吃全部已识别页型。
    applicable = tuple(
        item
        for item in evidence
        if item.page_type is not None
        and (not rule.applicable_page_types or item.page_type in rule.applicable_page_types)
    )
    if not applicable:
        if scope_plan is not None and scope_plan.status is RuleScopeStatus.NOT_APPLICABLE:
            return (), (
                "not_applicable",
                "AGGREGATE_RULE_SCOPE_NOT_APPLICABLE",
                "冻结规则范围计划已证明该规则声明的页面类型均不适用于当前任务",
            )
        if scope_plan is not None and scope_plan.status is RuleScopeStatus.APPLICABLE:
            return (), (
                "evidence_insufficient",
                "AGGREGATE_APPLICABLE_EVIDENCE_MISSING",
                "这条规则本次需要检查，但没有拿到它适用页面的合格截图，已转人工确认",
            )
        return (), (
            "evidence_insufficient",
            "AGGREGATE_APPLICABILITY_UNPROVEN",
            "本次没有拿到这条规则适用页面的合格截图，也不能据此认定规则不适用，已转人工确认",
        )
    if scope_plan is not None and scope_plan.status is RuleScopeStatus.NOT_APPLICABLE:
        return (), (
            "evidence_insufficient",
            "AGGREGATE_RULE_SCOPE_CONFLICT",
            "页面范围认定这条规则不适用，但本次又拿到了它适用页面的截图，两边对不上，已转人工确认",
        )
    # 展示层门禁在封存前生效：被遮挡的底层页面与诊断材料不能成为聚合规则的自然证据成员。
    payload = rule.rule_payload
    layer_target = rule_evidence_layer_target(
        category=_optional_text(payload.get("category")),
        judge_config=payload.get("judge_config"),
        check_point=_optional_text(payload.get("check_point")),
        rule_name=_optional_text(payload.get("name")),
    )
    accepts_diagnostic = rule_accepts_diagnostic_evidence(payload.get("judge_config"))
    admitted = tuple(
        item
        for item in applicable
        if evaluate_presentation_for_rule(
            item.presentation,
            layer_target=layer_target,
            accepts_diagnostic=accepts_diagnostic,
        ).accepted
    )
    if not admitted:
        # 证据被遮挡或来自诊断操作不改变规则适用性，只说明本轮证据不足。
        return (), (
            "evidence_insufficient",
            "AGGREGATE_PRESENTATION_EVIDENCE_REJECTED",
            "这条规则要用的页面截图全部被弹窗挡住，或是检查时临时操作出来的画面，"
            "本次没有能直接判断的截图，已转人工确认",
        )
    applicable = admitted
    if rule.evidence.scope is EvidenceInputScope.TASK:
        return applicable, None
    if rule.evidence.scope is EvidenceInputScope.JOURNEY:
        groups: dict[str, list[AggregateEvidenceSource]] = defaultdict(list)
        for item in applicable:
            journey_id = item.candidate.journey_execution_id
            if journey_id:
                groups[journey_id].append(item)
        selected = max(groups.values(), key=len, default=[])
        if len(selected) < 2:
            return tuple(selected), (
                "evidence_insufficient",
                "AGGREGATE_JOURNEY_EVIDENCE_INSUFFICIENT",
                "这条规则要对比同一条操作路径上至少两个页面，本次拿到的合格截图不够，已转人工确认",
            )
        return tuple(selected), None
    matrix_groups: dict[int, list[AggregateEvidenceSource]] = defaultdict(list)
    for item in applicable:
        if item.candidate.environment_profile_id:
            matrix_groups[item.candidate.page_sequence].append(item)
    selected = max(
        matrix_groups.values(),
        key=lambda items: len({item.candidate.environment_profile_id for item in items}),
        default=[],
    )
    profiles = {item.candidate.environment_profile_id for item in selected}
    if len(profiles) < 2:
        return tuple(selected), (
            "evidence_insufficient",
            "AGGREGATE_ENVIRONMENT_MATRIX_INSUFFICIENT",
            "这条规则要对比同一页面在至少两种设备环境下的截图，本次拿到的不够，已转人工确认",
        )
    return tuple(selected), None


def build_aggregate_rule_source(row: Mapping[str, Any]) -> AggregateRuleSource:
    """把 PostgreSQL 规则行转换为显式聚合规则；page 规则由调用方过滤。"""

    payload = {
        key: row.get(key)
        for key in (
            "rule_id",
            "rule_code",
            "category",
            "executor_type",
            "severity",
            "check_point",
            "input_contract",
            "judge_config",
            "manual_review_policy",
            "steps",
        )
    }
    evidence = validate_enabled_aggregate_rule_configuration(
        row.get("judge_config") if isinstance(row, Mapping) else {}
    )
    return AggregateRuleSource(
        rule_id=str(row["rule_id"]),
        rule_code=str(row["rule_code"]),
        rule_payload=payload,
        applicable_page_types=tuple(str(item) for item in row["applicable_page_types"]),
        evidence=evidence,
    )


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
