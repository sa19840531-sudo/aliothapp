"""H5 同缺口收敛与只读缺制品取证的保守兼容判定。"""

import re
from dataclasses import dataclass

from services.api.app.domain.exploration import ProbeWork
from services.api.app.domain.inspection import (
    EvidenceRequest,
    EvidenceRequiredEquivalence,
    EvidenceSideEffectPolicy,
    build_evidence_request,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.domain.protocol import stable_protocol_hash, thaw_json_mapping


@dataclass(frozen=True, kw_only=True)
class H5GapEvidence:
    task_id: str
    agent_run_id: str
    finalization_epoch: int
    session_id: str
    logical_page_key: str
    state_fingerprint: str
    contract_hash: str
    content_hash: str
    identity_confirmed: bool
    page_state: str
    is_overlay: bool
    is_external: bool

    @property
    def usable(self) -> bool:
        return bool(
            self.identity_confirmed is True
            and self.is_overlay is False
            and self.is_external is False
            and self.page_state == "stable"
            and self.session_id.startswith("h5:")
            and all(
                (
                    self.task_id,
                    self.agent_run_id,
                    self.logical_page_key,
                    self.state_fingerprint,
                    self.contract_hash,
                    self.content_hash,
                )
            )
        )


def _snapshot_goal(request: EvidenceRequest) -> bool:
    invariants = request.target_invariants
    semantic = invariants.get("semantic", {})
    return bool(
        request.side_effect_policy is EvidenceSideEffectPolicy.READ_ONLY
        and request.required_interaction is None
        and request.required_equivalence is EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT
        and not semantic.get("capture_roles")
        and not any(key in semantic for key in ("timing", "sequence", "time_window"))
    )


def compatible_observation_key(request: EvidenceRequest) -> str | None:
    """只识别既有确定性缺制品说明，不把自由文本目标推测成零触碰观察。"""
    if not _snapshot_goal(request) or request.reason_code != "EVIDENCE_INSUFFICIENT":
        return None
    match = _missing_observation_artifact(request)
    invariants = thaw_json_mapping(request.target_invariants)
    if not match or invariants.get("structure", {}).get("layer_target") != "base_page":
        return None
    if match["artifact"] not in request.required_artifact_types:
        return None
    semantic = invariants.get("semantic", {})
    if semantic.get("region") != match["region"]:
        return None
    # 这里的区域是规范生成器写入的规则正文，不是页面上的局部取证区域。
    semantic.pop("region")
    return stable_protocol_hash(
        {
            "task_id": request.task_id,
            "target_capture_id": request.target_capture_id,
            "invariants": invariants,
            "artifacts": sorted(request.required_artifact_types),
            "dependency": request.session_dependency.value,
            "replay": request.replay_policy.value,
            "grade": request.minimum_source_grade.value,
        }
    )


def _missing_observation_artifact(request: EvidenceRequest):
    return re.fullmatch(
        r".+ 的「(?P<region>.+)」：缺来源 (?P<artifact>visible_text|dom|ui_snapshot)"
        r"\(non_empty\)。允许在等价状态下补采。",
        request.evidence_goal,
    )


def complete_observation_profile(request: EvidenceRequest) -> EvidenceRequest:
    """已校验的 H5 零触碰缺制品请求补齐制品合同，不推测自由文本或改变取证目标。"""
    match = _missing_observation_artifact(request)
    invariants = request.target_invariants
    if (
        not _snapshot_goal(request) or request.reason_code != "EVIDENCE_INSUFFICIENT"
        or not match or invariants.get("structure", {}).get("layer_target") != "base_page"
        or invariants.get("semantic", {}).get("region") != match["region"]
        or match["artifact"] in request.required_artifact_types
    ):
        return request
    payload = request.to_dict()
    for name in ("request_id", "requirement_hash", "protocol_version"):
        payload.pop(name)
    for name in (
        "session_dependency", "replay_policy", "required_equivalence",
        "side_effect_policy", "minimum_source_grade", "required_interaction", "expires_at",
    ):
        payload[name] = getattr(request, name)
    payload["required_artifact_types"] = (*request.required_artifact_types, match["artifact"])
    return build_evidence_request(**payload)


def unchanged_gap(
    prior: EvidenceRequest,
    prior_evidence: H5GapEvidence,
    current: EvidenceRequest,
    current_evidence: H5GapEvidence,
) -> bool:
    """只收敛相同规则合同、相同目标及完整相关制品内容下的旧缺口。"""
    return bool(
        _snapshot_goal(prior)
        and _snapshot_goal(current)
        and prior_evidence.usable
        and current_evidence.usable
        and prior.task_id == current.task_id == current_evidence.task_id
        and prior.rule_id == current.rule_id
        and prior.rule_code == current.rule_code
        and prior.requirement_hash == current.requirement_hash
        and prior_evidence == current_evidence
    )


def compatible_probe_pair(leader: ProbeWork, follower: ProbeWork) -> bool:
    """共享的是同一已恢复现场的只读采集，不共享规则结论或恢复证明。"""
    key = compatible_observation_key(leader.evidence_request)
    return bool(
        key is not None
        and key == compatible_observation_key(follower.evidence_request)
        and leader.work_id != follower.work_id
        and leader.surface_kind is follower.surface_kind is SurfaceKind.WEB
        and leader.session_id == follower.session_id
        and leader.session_id.startswith("h5:")
        and leader.source_capture_id == follower.source_capture_id
        and leader.source_grade == follower.source_grade
    )
