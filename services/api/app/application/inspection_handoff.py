"""质检工作项到 Rule Judge 的应用层交接端口。"""

from __future__ import annotations

from collections.abc import Mapping

from services.api.app.application.app_action_evidence import (
    AppEvidenceCaptureRoleResolver,
    PostgresAppActionEvidenceStore,
)
from services.api.app.application.inspection_work_items import InspectionWorkItemLease
from services.api.app.domain.inspection import (
    EvidenceBundleReference,
    InspectionHandoff,
    InspectionRuleTarget,
)


def build_inspection_handoff(
    lease: InspectionWorkItemLease,
    *,
    attempt_id: str,
    evidence_capture_roles: Mapping[str, str] | None = None,
    rule_evidence: Mapping[str, EvidenceBundleReference] | None = None,
) -> InspectionHandoff:
    """构造只读输入；Web 序列的私有读授权不进入公开序列化或模型提示。"""
    resolved_rule_evidence = dict(rule_evidence or {})
    if rule_evidence is None:
        baseline_roles = (
            dict(evidence_capture_roles) if evidence_capture_roles is not None else None
        )
        resolved_rule_evidence = {}
        for item in lease.rule_evidence:
            item_roles = dict(item.evidence_capture_roles) or {
                "primary": item.page_capture_id
            }
            # 补采 primary 与工作项主 Capture 一致时，保留 before/primary 角色血缘。
            if (
                baseline_roles
                and baseline_roles.get("primary") == item.page_capture_id
                and not (lease.target_platform == "h5" and item.evidence_capture_roles)
            ):
                item_roles = dict(baseline_roles)
            resolved_rule_evidence[item.rule_code] = EvidenceBundleReference(
                page_capture_id=item.page_capture_id,
                producer_agent_run_id=item.producer_agent_run_id,
                manifest_id=item.manifest_id,
                manifest_version=str(item.manifest_version),
                observation_packet_ids=item.observation_packet_ids,
                artifact_ids=item.artifact_ids,
                evidence_capture_roles=item_roles,
                evidence_manifest_refs={
                    role: {"manifest_id": manifest_id, "manifest_version": version}
                    for role, manifest_id, version in item.evidence_manifest_refs
                } or None,
            )
    return InspectionHandoff(
        work_item_id=lease.work_item_id,
        attempt_id=attempt_id,
        lease_token=lease.lease_token if any(rule.evidence_input_set_id for rule in lease.rules) else None,
        task_id=lease.task_id,
        agent_run_id=lease.consumer_agent_run_id,
        input_hash=lease.input_hash,
        batch_key=lease.batch_key,
        strategy=lease.strategy,
        category=lease.category,
        session_dependency=lease.session_dependency,
        evidence=EvidenceBundleReference(
            page_capture_id=lease.page_capture_id,
            producer_agent_run_id=lease.producer_agent_run_id,
            evidence_capture_roles=(
                dict(evidence_capture_roles) if evidence_capture_roles is not None else None
            ),
        ),
        rules=tuple(
            InspectionRuleTarget(
                rule_id=rule.rule_id,
                rule_code=rule.rule_code,
                ordinal=rule.ordinal,
                contract_hash=rule.contract_hash,
                evidence_input_set_id=rule.evidence_input_set_id,
            )
            for rule in sorted(lease.rules, key=lambda item: item.ordinal)
        ),
        original_page_input_sets={
            rule.rule_code: next((item.evidence_input_set_id for item in lease.rule_evidence
                                  if item.rule_code == rule.rule_code and item.evidence_input_set_id),
                                 rule.evidence_input_set_id)
            for rule in lease.rules
            if lease.target_platform == "h5" and rule.evidence_input_set_id
        },
        rule_evidence=resolved_rule_evidence,
    )


async def resolve_inspection_evidence_capture_roles(
    lease: InspectionWorkItemLease,
    *,
    resolver: AppEvidenceCaptureRoleResolver | None = None,
) -> dict[str, str]:
    """为 Snapshot 工作项解析 before/primary；解析失败时回退仅 primary。"""
    store = resolver or PostgresAppActionEvidenceStore()
    try:
        roles = await store.resolve(
            task_id=lease.task_id,
            page_capture_id=lease.page_capture_id,
        )
    except Exception:  # noqa: BLE001 - 角色解析失败不得阻断判定入口
        return {"primary": lease.page_capture_id}
    if not isinstance(roles, dict) or roles.get("primary") != lease.page_capture_id:
        return {"primary": lease.page_capture_id}
    return {str(key): str(value) for key, value in roles.items()}
