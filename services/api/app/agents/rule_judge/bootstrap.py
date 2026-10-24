"""V2 Rule Judge Agent 的默认生产组装入口。"""

from __future__ import annotations

from functools import lru_cache

from services.api.app.agents.rule_judge.maf_agent import MafRuleJudgeAgent
from services.api.app.application.manifest_read_composition import (
    build_manifest_read_composition,
)
from services.api.app.application.manifest_read_cutover import ManifestReadMode
from services.api.app.application.observation_packet import ObservationPacketService
from services.api.app.application.rule_judge import RuleJudgeService
from services.api.app.core.config import get_settings
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
    LLMSemanticRuleJudge,
)
from services.api.app.infrastructure.rule_judge_evidence import (
    PostgresRuleJudgeEvidenceReader,
)


def _build_manifest_packet_loader():
    """规则引擎取证端口：与探索侧证据 Tool 共用同一套证据语义。"""
    from services.api.app.application.evidence_access import ManifestEvidenceAccess
    from services.api.app.infrastructure.db.evidence_manifest_repository import (
        PostgresEvidenceManifestRepository,
    )
    from services.api.app.infrastructure.storage.factory import (
        build_evidence_artifact_store,
    )

    return ManifestEvidenceAccess(
        manifests=PostgresEvidenceManifestRepository(),
        artifact_reader=build_evidence_artifact_store(get_settings().evidence_storage),
    )


def _build_web_sequence_reader(inner, *, h5_targeted_recapture: bool = False):
    """Web 显式 InputSet 始终精确读清单，普通请求保留模式裁决后的原读端口。"""
    from services.api.app.application.manifest_backed_packet import (
        ManifestBackedPacketLoader,
    )
    from services.api.app.application.web_sequence_reader import (
        WebSequenceObservationReader,
    )
    from services.api.app.infrastructure.db.evidence_manifest_repository import (
        PostgresEvidenceManifestRepository,
    )
    from services.api.app.infrastructure.db.observation_packet_repository import (
        PostgresObservationPacketRepository,
    )
    from services.api.app.infrastructure.db.web_sequence_binding_reader import (
        PostgresWebSequenceBindingReader,
    )
    from services.api.app.infrastructure.storage.factory import (
        build_evidence_artifact_store,
    )

    contexts = PostgresObservationPacketRepository()
    packet_loader = ManifestBackedPacketLoader(
        manifests=PostgresEvidenceManifestRepository(),
        artifact_reader=build_evidence_artifact_store(get_settings().evidence_storage),
    )
    from services.api.app.application.h5_page_input_reader import H5PageInputObservationReader
    from services.api.app.infrastructure.db.h5_page_binding_reader import (
        PostgresH5PageBindingReader,
    )

    reader = WebSequenceObservationReader(
        inner=H5PageInputObservationReader(
            inner=inner, contexts=contexts, bindings=PostgresH5PageBindingReader(),
            packet_loader=packet_loader,
        ),
        contexts=contexts, bindings=PostgresWebSequenceBindingReader(), packet_loader=packet_loader,
    )
    if not h5_targeted_recapture:
        return reader

    from services.api.app.application.h5_targeted_recapture_observation import (
        H5TargetedRecaptureObservationReader,
    )
    from services.api.app.application.manifest_backed_observation import (
        ManifestBackedObservationRepository,
    )

    return H5TargetedRecaptureObservationReader(
        inner=reader,
        exact_manifest_reader=ManifestBackedObservationRepository(
            inner=contexts,
            packet_loader=packet_loader,
        ),
    )


def _build_rule_judge_agent(*, h5_targeted_recapture: bool) -> MafRuleJudgeAgent:
    optimization = get_settings().quality_optimization
    manifest_read = build_manifest_read_composition()
    return MafRuleJudgeAgent(
        service=RuleJudgeService(
            repository=PostgresTaskRepository(),
            evidence_reader=PostgresRuleJudgeEvidenceReader(
                manifest_loader=(
                    _build_manifest_packet_loader()
                    if manifest_read.effective_mode is ManifestReadMode.ON
                    else None
                ),
            ),
            observation_packet_service=ObservationPacketService(
                repository=_build_web_sequence_reader(
                    manifest_read.reader,
                    h5_targeted_recapture=h5_targeted_recapture,
                ),
            ),
            semantic_judge=LLMSemanticRuleJudge(
                request_timeout_seconds=optimization.semantic_request_timeout_seconds,
                circuit_seconds=optimization.semantic_circuit_seconds,
            ),
            verdict_mode=optimization.verdict_mode,
            manifest_quality_authoritative=(
                manifest_read.effective_mode is ManifestReadMode.ON
            ),
        )
    )


@lru_cache
def build_default_rule_judge_agent() -> MafRuleJudgeAgent:
    """构建不依赖 V1 Agent Run 上下文的生产 Rule Judge。

    E24：证据读取来源按 `ALIOTH_EVIDENCE_MANIFEST_READ_MODE` 装配
    （off＝旧读取，shadow＝双读对账，on＝清单主路径），默认 off 与改造前一致。
    模式变更后需重建本工厂（进程重启或清 lru_cache）才生效。
    """
    return _build_rule_judge_agent(h5_targeted_recapture=False)


@lru_cache
def build_h5_targeted_recapture_rule_judge_agent() -> MafRuleJudgeAgent:
    """H5 补采判定支持精确 Manifest，其他读取策略保持不变。"""

    return _build_rule_judge_agent(h5_targeted_recapture=True)
