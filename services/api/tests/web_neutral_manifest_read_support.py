"""T10 中性 stable：真实 shadow 对账后由既有 Resolver 切主，模型仍为替身。"""

from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.manifest_shadow_comparison_repository import (
    PostgresManifestShadowComparisonRepository,
)
from services.api.app.infrastructure.db.observation_packet_repository import (
    PostgresObservationPacketRepository,
)
from services.api.app.infrastructure.db.web_sequence_binding_reader import (
    PostgresWebSequenceBindingReader,
)


async def build_neutral_stable_factory(pool, ids, artifacts, tasks, business, mode, monkeypatch):
    from services.api.app.agents.rule_judge import bootstrap
    from services.api.app.application import manifest_read_composition as composition
    from services.api.app.core.config import EvidenceManifestReadSettings
    from services.api.app.infrastructure.db import evidence_manifest_repository as manifests
    from services.api.app.infrastructure.db import observation_packet_repository as observations
    from services.api.app.infrastructure.db import web_sequence_binding_reader as bindings
    from services.api.app.infrastructure.storage import factory as storage

    contexts = PostgresObservationPacketRepository(pool=pool, artifact_store=artifacts)
    repository = PostgresEvidenceManifestRepository(pool=pool)
    shadow_repository = PostgresManifestShadowComparisonRepository(pool=pool)
    monkeypatch.setattr(composition, "PostgresObservationPacketRepository", lambda: contexts)
    monkeypatch.setattr(composition, "PostgresEvidenceManifestRepository", lambda: repository)
    monkeypatch.setattr(
        composition, "PostgresManifestShadowComparisonRepository", lambda: shadow_repository
    )
    monkeypatch.setattr(composition, "build_evidence_artifact_store", lambda *_: artifacts)
    monkeypatch.setattr(observations, "PostgresObservationPacketRepository", lambda: contexts)
    monkeypatch.setattr(manifests, "PostgresEvidenceManifestRepository", lambda **_: repository)
    monkeypatch.setattr(
        bindings,
        "PostgresWebSequenceBindingReader",
        lambda: PostgresWebSequenceBindingReader(pool=pool),
    )
    monkeypatch.setattr(storage, "build_evidence_artifact_store", lambda *_: artifacts)
    monkeypatch.setattr(bootstrap, "PostgresTaskRepository", lambda: tasks)
    monkeypatch.setattr(bootstrap, "LLMSemanticRuleJudge", lambda **_: business)
    summary = None
    if mode == "on":
        # 切主凭证来自实际双读，不造 matched 行或跳过 Resolver。
        async with pool.acquire() as connection:
            anchor = await connection.fetchrow(
                "SELECT p.page_capture_id::text,c.captured_at FROM qc_h5_inspection_publication p "
                "JOIN qc_capture c ON c.id=p.page_capture_id WHERE p.task_id=$1::uuid "
                "AND p.web_evidence_sequence_id IS NULL ORDER BY p.created_at,p.id LIMIT 1",
                ids["task"],
            )
        shadow = composition.build_manifest_read_composition(
            settings=EvidenceManifestReadSettings(mode="shadow", cutover_at=anchor["captured_at"])
        )
        await shadow.reader.load_source(
            task_id=ids["task"],
            page_capture_id=anchor["page_capture_id"],
            captured_at=anchor["captured_at"],
        )
        summary = shadow.current_summary()
        assert summary.zero_difference, summary
    configured = composition.build_manifest_read_composition(
        settings=EvidenceManifestReadSettings(mode=mode), summary=summary
    )
    assert configured.effective_mode.value == mode
    assert (
        type(configured.reader).__name__
        == {
            "off": "PostgresObservationPacketRepository",
            "shadow": "ShadowReadReconciler",
            "on": "ManifestBackedObservationRepository",
        }[mode]
    )
    monkeypatch.setattr(bootstrap, "build_manifest_read_composition", lambda: configured)
    bootstrap.build_default_rule_judge_agent.cache_clear()
    try:
        agent = bootstrap.build_default_rule_judge_agent()
        assert agent._service._observation_packet_service._repository._inner is configured.reader
        return agent
    finally:
        bootstrap.build_default_rule_judge_agent.cache_clear()
