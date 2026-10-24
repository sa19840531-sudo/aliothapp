"""无 Driver、无写库 Tool 的 V2 Rule Judge Agent 应用适配器。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from services.api.app.agents.rule_judge.normalization import (
    normalize_rule_judge_outcome,
)
from services.api.app.application.rule_judge import (
    RuleJudgeRequest,
    RuleJudgeResult,
    RuleJudgeService,
)
from services.api.app.domain.inspection import (
    EvidenceBundleReference,
    InspectionHandoff,
    RuleJudgeOutcome,
)


class MafRuleJudgeAgent:
    """用 MAF 语义 Judge 消费可信证据，确定性规则仍由 Rule Engine 执行。"""

    def __init__(self, *, service: RuleJudgeService) -> None:
        self._service = service

    async def judge(self, handoff: InspectionHandoff) -> RuleJudgeOutcome:
        """执行锁定规则，但不持有结果 Writer、租约或环境操作能力。"""
        requests = []
        for rule in handoff.rules:
            evidence = handoff.evidence_for_rule(rule.rule_code)
            manifest_refs = {
                role: dict(reference)
                for role, reference in (evidence.evidence_manifest_refs or {}).items()
            }
            if evidence.manifest_id is not None:
                manifest_refs.setdefault(
                    "primary",
                    {
                        "manifest_id": evidence.manifest_id,
                        "manifest_version": evidence.manifest_version or "",
                    },
                )
            retained_baseline_artifact_ids = {
                role: tuple(artifact_ids)
                for role, artifact_ids in (
                    evidence.retained_baseline_artifact_ids or {}
                ).items()
            }
            # 补采 Capture/Manifest 必须成对；App/H5 普通基线常只有 page_capture_id，
            # 半套字段会触发「必须同时提供」并让 Snapshot 工作项耗尽重试。
            source_capture_id, source_manifest_id = _paired_evidence_source(evidence)
            requests.append(RuleJudgeRequest(
                task_id=handoff.task_id,
                rule_code=rule.rule_code,
                page_capture_id=handoff.evidence.page_capture_id,
                evidence_source_page_capture_id=source_capture_id,
                evidence_manifest_id=source_manifest_id,
                agent_run_id=handoff.agent_run_id,
                source_agent_run_id=evidence.producer_agent_run_id,
                evidence_artifact_ids=evidence.artifact_ids,
                evidence_capture_roles=evidence.evidence_capture_roles,
                evidence_manifest_refs=manifest_refs or None,
                retained_baseline_artifact_ids=(
                    retained_baseline_artifact_ids or None
                ),
                judge_input={
                    "manifest_id": evidence.manifest_id,
                    "manifest_version": evidence.manifest_version,
                    "observation_packet_ids": list(evidence.observation_packet_ids),
                    "evidence_manifest_refs": manifest_refs or None,
                },
                expected_contract_hash=rule.contract_hash,
                evidence_input_set_id=(None if rule.rule_code in handoff.original_page_input_sets
                                       else rule.evidence_input_set_id),
                original_page_input_set_id=handoff.original_page_input_sets.get(rule.rule_code),
                work_item_id=handoff.work_item_id,
                attempt_id=handoff.attempt_id,
                lease_token=handoff.lease_token,
            ))
        results = (
            [await self._service.judge(requests[0])]
            if len(requests) == 1
            else await self._service.judge_many(requests)
        )
        raw_items = [_result_payload(item) for item in results]
        return normalize_rule_judge_outcome(
            handoff=handoff,
            raw={"items": raw_items},
        )


def _paired_evidence_source(
    evidence: EvidenceBundleReference,
) -> tuple[str | None, str | None]:
    """仅在 Capture 与 Manifest 齐全时透传补采来源；否则交给基线 page_capture_id。"""

    if evidence.manifest_id is None or not evidence.page_capture_id:
        return None, None
    return evidence.page_capture_id, evidence.manifest_id


def _result_payload(result: RuleJudgeResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["evidence_artifact_ids"] = list(result.evidence_artifact_ids)
    return payload
