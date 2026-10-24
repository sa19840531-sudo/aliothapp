"""补采接纳后重封 EvidenceInputSet（原输入 + Supplement + 请求血缘）。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from services.api.app.application.evidence_input_sets import PersistedEvidenceInputSet
from services.api.app.application.evidence_manifests import EvidenceManifestRepository
from services.api.app.application.evidence_requests import StoredEvidenceRequest
from services.api.app.domain.evidence.contracts import EvidenceManifestReference
from services.api.app.domain.evidence.input_set import (
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceInputSet,
    EvidenceInputSetValidationError,
    seal_evidence_input_set,
)
from services.api.app.domain.protocol import freeze_json_mapping

SUPPLEMENT_RESEAL_SELECTION_POLICY = "supplement-reseal.v1"
RESEALED_INPUT_SET_MARKER = "resealed_input_set:"


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _stable_hash(payload: Mapping[str, object]) -> str:
    frozen = freeze_json_mapping(dict(payload), "supplement_reseal_selection")
    body = json.dumps(
        _jsonable(frozen),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(body.encode("utf-8")).hexdigest()


def format_resolution_reason_with_reseal(
    *,
    base_reason: str,
    input_set_id: str,
) -> str:
    """在既有 resolution_reason 上附加重封 InputSet 身份（幂等替换旧标记）。"""

    if not isinstance(base_reason, str) or not base_reason.strip():
        raise EvidenceInputSetValidationError("base_reason 必须为非空字符串")
    if (
        not isinstance(input_set_id, str)
        or not input_set_id.strip()
        or input_set_id != input_set_id.strip()
    ):
        raise EvidenceInputSetValidationError("input_set_id 必须为无首尾空白的非空字符串")
    marker = f"{RESEALED_INPUT_SET_MARKER}{input_set_id.strip()}"
    parts = [
        part
        for part in base_reason.split("|")
        if part and not part.startswith(RESEALED_INPUT_SET_MARKER)
    ]
    parts.append(marker)
    return "|".join(parts)


def parse_resealed_input_set_id(resolution_reason: str | None) -> str | None:
    """从 resolution_reason 解析补采重封后的 EvidenceInputSet id。"""

    if not isinstance(resolution_reason, str) or not resolution_reason.strip():
        return None
    for part in resolution_reason.split("|"):
        if part.startswith(RESEALED_INPUT_SET_MARKER):
            value = part[len(RESEALED_INPUT_SET_MARKER) :].strip()
            return value or None
    return None


def reseal_evidence_input_set_with_supplement(
    *,
    original: EvidenceInputSet,
    original_candidates: Sequence[EvidenceInputCandidate],
    supplement_candidate: EvidenceInputCandidate,
    request_id: str,
    restoration_attempt_id: str,
    equivalence_assessment_id: str,
    sealed_at: datetime,
    supplement_manifest_id: str | None = None,
) -> EvidenceInputSet:
    """在原 EvidenceInputSet 上追加补采 Capture，生成新的不可变 InputSet。

    新 Attempt 必须只引用返回集合的 `input_set_id`／`input_set_hash`。
    """

    if not isinstance(original, EvidenceInputSet):
        raise TypeError("original 必须为 EvidenceInputSet")
    if not isinstance(supplement_candidate, EvidenceInputCandidate):
        raise TypeError("supplement_candidate 必须为 EvidenceInputCandidate")
    for name, value in (
        ("request_id", request_id),
        ("restoration_attempt_id", restoration_attempt_id),
        ("equivalence_assessment_id", equivalence_assessment_id),
    ):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise EvidenceInputSetValidationError(f"{name} 必须为无首尾空白的非空字符串")

    base = tuple(original_candidates)
    if any(not isinstance(item, EvidenceInputCandidate) for item in base):
        raise EvidenceInputSetValidationError("original_candidates 类型不受支持")
    base_by_capture = {item.manifest.capture_id: item for item in base}
    if set(base_by_capture) != set(original.eligible_capture_ids):
        raise EvidenceInputSetValidationError(
            "original_candidates 必须完整覆盖原 eligible_capture_ids"
        )

    supplement_capture = supplement_candidate.manifest.capture_id
    if original.scope is EvidenceInputScope.PAGE:
        merged = [supplement_candidate]
        eligible = [supplement_capture]
    elif supplement_capture in base_by_capture:
        merged = [
            supplement_candidate
            if capture_id == supplement_capture
            else base_by_capture[capture_id]
            for capture_id in original.eligible_capture_ids
        ]
        eligible = list(original.eligible_capture_ids)
    else:
        merged = [base_by_capture[capture_id] for capture_id in original.eligible_capture_ids]
        merged.append(supplement_candidate)
        eligible = [*original.eligible_capture_ids, supplement_capture]

    manifest_id = (
        str(supplement_manifest_id).strip()
        if isinstance(supplement_manifest_id, str) and supplement_manifest_id.strip()
        else supplement_candidate.manifest.manifest_id
    )
    selection_policy_hash = _stable_hash(
        {
            "policy": SUPPLEMENT_RESEAL_SELECTION_POLICY,
            "parent_input_set_id": original.input_set_id,
            "parent_input_set_hash": original.input_set_hash,
            "request_id": request_id,
            "restoration_attempt_id": restoration_attempt_id,
            "equivalence_assessment_id": equivalence_assessment_id,
            "supplement_manifest_id": manifest_id,
            "supplement_capture_id": supplement_capture,
            "eligible_capture_ids": eligible,
        }
    )
    resealed = seal_evidence_input_set(
        task_id=original.task_id,
        source_agent_run_id=original.source_agent_run_id,
        rule_id=original.rule_id,
        rule_code=original.rule_code,
        rule_contract_hash=original.rule_contract_hash,
        ruleset_snapshot_hash=original.ruleset_snapshot_hash,
        scope=original.scope,
        scope_key=original.scope_key,
        finalization_epoch=original.finalization_epoch,
        eligibility_snapshot_id=original.eligibility_snapshot_id,
        eligibility_snapshot_hash=original.eligibility_snapshot_hash,
        selection_policy_hash=selection_policy_hash,
        eligible_capture_ids=eligible,
        candidates=merged,
        sealed_at=sealed_at,
    )
    if resealed.input_set_id == original.input_set_id:
        raise EvidenceInputSetValidationError(
            "补采重封后 InputSet 身份未变化；禁止 Attempt 继续绑定原集合"
        )
    return resealed


class EvidenceInputSetStore(Protocol):
    async def add(self, *, input_set: EvidenceInputSet) -> PersistedEvidenceInputSet: ...

    async def get(self, *, input_set_id: str) -> EvidenceInputSet | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkItemInputSetContext:
    """从工作项规则读取的封存上下文（App 常无原 InputSet）。"""

    evidence_input_set_id: str | None
    contract_hash: str
    producer_agent_run_id: str
    finalization_epoch: int
    ruleset_snapshot_hash: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SealedSupplementInputSet:
    """补采记录上挂着的输入集合（挂接后立即封存与领取重判共用同一个）。"""

    input_set_id: str
    input_set_hash: str
    created: bool


class WorkItemInputSetContextReader(Protocol):
    async def get_rule_input_set_context(
        self,
        *,
        work_item_id: str,
        rule_id: str,
    ) -> WorkItemInputSetContext | None: ...

    async def seal_supplement_input_set(
        self,
        *,
        request_id: str,
    ) -> SealedSupplementInputSet | None: ...


class EvidenceRequestResolutionAnnotator(Protocol):
    async def annotate_resealed_input_set(
        self,
        *,
        request_id: str,
        input_set_id: str,
    ) -> str | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class SupplementInputSetResealResult:
    input_set_id: str
    input_set_hash: str
    created: bool
    parent_input_set_id: str | None


def _as_manifest_reference(value: object) -> EvidenceManifestReference:
    if isinstance(value, EvidenceManifestReference):
        return value
    manifest = getattr(value, "manifest", None)
    if isinstance(manifest, EvidenceManifestReference):
        return manifest
    raise EvidenceInputSetValidationError("Manifest 类型不受支持")


def _manifest_capture_role(manifest: EvidenceManifestReference) -> str:
    """使用 Manifest 已验签的单一附件角色作为 InputSet Capture 角色。"""

    roles = {artifact.role for artifact in manifest.artifacts}
    if len(roles) != 1:
        raise EvidenceInputSetValidationError("Manifest 附件必须使用同一个 Capture 角色")
    return next(iter(roles))


class SupplementInputSetResealService:
    """Supplement 接纳后重封并持久化 EvidenceInputSet。

    页面级规则（工作项成员没有 Web 序列集合）与领取重判共用同一套封存：集合挂在补采记录上，
    挂接后和领取时谁先封就沿用谁的，重判的 Attempt 与规则执行都指回它。Web 序列规则在原集合上
    追加补采另封。两种情况都把集合编号写进 request.resolution_reason 的 ``resealed_input_set:``
    标记；不改写 ``qc_inspection_work_item_rule.evidence_input_set_id``（Web 序列绑定不可变，
    且非 web_sequence 范围会被触发器拒绝）。
    """

    def __init__(
        self,
        *,
        input_sets: EvidenceInputSetStore,
        manifests: EvidenceManifestRepository,
        work_items: WorkItemInputSetContextReader,
        requests: EvidenceRequestResolutionAnnotator,
    ) -> None:
        self._input_sets = input_sets
        self._manifests = manifests
        self._work_items = work_items
        self._requests = requests

    async def reseal_after_attach(
        self,
        *,
        stored: StoredEvidenceRequest,
        supplement_manifest_id: str,
        restoration_attempt_id: str,
        equivalence_assessment_id: str,
        sealed_at: datetime | None = None,
    ) -> SupplementInputSetResealResult | None:
        request = stored.request
        context = await self._work_items.get_rule_input_set_context(
            work_item_id=request.work_item_id,
            rule_id=request.rule_id,
        )
        if context is None:
            return None
        if not context.evidence_input_set_id:
            sealed = await self._work_items.seal_supplement_input_set(
                request_id=request.request_id,
            )
            if sealed is None:
                return None
            await self._requests.annotate_resealed_input_set(
                request_id=request.request_id,
                input_set_id=sealed.input_set_id,
            )
            return SupplementInputSetResealResult(
                input_set_id=sealed.input_set_id,
                input_set_hash=sealed.input_set_hash,
                created=sealed.created,
                parent_input_set_id=None,
            )

        supplement_manifest = await self._manifests.get(manifest_id=supplement_manifest_id)
        if supplement_manifest is None:
            raise EvidenceInputSetValidationError(
                f"补采 Manifest 不存在: {supplement_manifest_id}"
            )
        supplement_manifest_ref = _as_manifest_reference(supplement_manifest)
        supplement_candidate = EvidenceInputCandidate(
            manifest=supplement_manifest_ref,
            capture_role=_manifest_capture_role(supplement_manifest_ref),
            page_sequence=2,
        )

        original, original_candidates = await self._resolve_original(
            input_set_id=context.evidence_input_set_id,
        )
        when = sealed_at or datetime.now(UTC)
        resealed = reseal_evidence_input_set_with_supplement(
            original=original,
            original_candidates=original_candidates,
            supplement_candidate=supplement_candidate,
            request_id=request.request_id,
            restoration_attempt_id=restoration_attempt_id,
            equivalence_assessment_id=equivalence_assessment_id,
            sealed_at=when,
            supplement_manifest_id=supplement_manifest_id,
        )
        persisted = await self._input_sets.add(input_set=resealed)
        await self._requests.annotate_resealed_input_set(
            request_id=request.request_id,
            input_set_id=persisted.input_set.input_set_id,
        )
        return SupplementInputSetResealResult(
            input_set_id=persisted.input_set.input_set_id,
            input_set_hash=persisted.input_set.input_set_hash,
            created=bool(persisted.created),
            parent_input_set_id=original.input_set_id,
        )

    async def _resolve_original(
        self,
        *,
        input_set_id: str,
    ) -> tuple[EvidenceInputSet, tuple[EvidenceInputCandidate, ...]]:
        original = await self._input_sets.get(input_set_id=input_set_id)
        if original is None:
            raise EvidenceInputSetValidationError(
                f"原 EvidenceInputSet 不存在: {input_set_id}"
            )
        candidates: list[EvidenceInputCandidate] = []
        for member in original.members:
            manifest = await self._manifests.get(manifest_id=member.manifest_id)
            if manifest is None:
                raise EvidenceInputSetValidationError(
                    f"原 InputSet 成员 Manifest 缺失: {member.manifest_id}"
                )
            candidates.append(
                EvidenceInputCandidate(
                    manifest=_as_manifest_reference(manifest),
                    capture_role=member.capture_role,
                    page_sequence=member.page_sequence,
                    journey_execution_id=member.journey_execution_id,
                    environment_profile_id=member.environment_profile_id,
                )
            )
        return original, tuple(candidates)
