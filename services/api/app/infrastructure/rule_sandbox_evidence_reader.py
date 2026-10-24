"""规则测试沙箱的确定性取证端口：从内存里的测试证据组装可信证据包，不读库、不落库。"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from services.api.app.application.observation_packet import ObservationPacket, ObservationSource
from services.api.app.application.rule_judge import (
    RuleJudgeEvidenceInsufficientError,
    RuleJudgeValidationError,
)
from services.api.app.infrastructure.rule_judge_evidence import (
    TrustedEvidenceArtifact,
    TrustedEvidenceBundle,
)


class SandboxRuleJudgeEvidenceReader:
    """与 PostgresRuleJudgeEvidenceReader 同一合同，但证据只有测试人员给的这一页，
    外加规则要、测试人员也补了的另一组采集（操作前页面、第二个价格位置）。"""

    def __init__(
        self,
        source: ObservationSource,
        role_sources: Mapping[str, ObservationSource] | None = None,
    ) -> None:
        self._source = source
        self._role_sources = dict(role_sources or {})

    async def prepare_observation_packet(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        source_agent_run_id: str | None = None,
        rule_id: str,
        page_capture_id: str,
        evidence_source_page_capture_id: str | None = None,
        evidence_manifest_id: str | None = None,
        evidence_capture_roles: dict[str, str] | None,
        evidence_manifest_refs: dict[str, dict[str, str]] | None = None,
        retained_baseline_artifact_ids: dict[str, tuple[str, ...]] | None = None,
        evidence_artifact_ids: tuple[str, ...],
        required_artifact_types: tuple[str, ...],
        frozen_observation_packet: ObservationPacket | None = None,
    ) -> TrustedEvidenceBundle:
        """按生产读取器的缺证据口径组装证据包：缺必需类型时报证据不足。"""
        if task_id != self._source.task_id or page_capture_id != self._source.page_capture_id:
            raise RuleJudgeValidationError("测试证据不属于本次测试")
        extra_roles = sorted(
            role for role in (evidence_capture_roles or {}) if role != "primary"
        )
        missing_roles = [role for role in extra_roles if role not in self._role_sources]
        if missing_roles:
            raise RuleJudgeEvidenceInsufficientError(
                "测试页只提供了一组页面证据，规则还需要采集角色：" + "、".join(missing_roles)
            )
        captures = [("primary", self._source)] + [
            (role, self._role_sources[role]) for role in extra_roles
        ]
        artifacts = tuple(
            TrustedEvidenceArtifact(
                artifact_id=artifact.artifact_id,
                artifact_type=artifact.artifact_type,
                page_capture_id=source.page_capture_id,
                capture_role=role,
                content_type=artifact.content_type,
                payload=artifact.payload,
            )
            for role, source in captures
            for artifact in source.artifacts
        )
        missing_types = sorted(
            set(required_artifact_types) - {artifact.artifact_type for artifact in artifacts}
        )
        if missing_types:
            raise RuleJudgeEvidenceInsufficientError(
                "规则判定缺少必需证据类型：" + ", ".join(missing_types)
            )
        if frozen_observation_packet is not None and (
            frozen_observation_packet.task_id != task_id
            or frozen_observation_packet.page_capture_id != page_capture_id
            or frozen_observation_packet.rule_id != rule_id
        ):
            raise RuleJudgeValidationError("沙箱观察包不属于当前任务、采集或规则")
        return TrustedEvidenceBundle(
            observation_packet_id=(
                frozen_observation_packet.observation_packet_id
                if frozen_observation_packet is not None
                else f"sandbox-evidence-{uuid4().hex[:12]}"
            ),
            page_capture_id=page_capture_id,
            capture_roles={role: source.page_capture_id for role, source in captures},
            artifacts=artifacts,
        )
