"""E07/E10：Web 清单写入服务的哈希复算、幂等、版本与裁决测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.evidence_manifest_writer import (
    PASS_QUALITY_REASON,
    PENDING_QUALITY_REASON,
    EvidenceManifestWriter,
    ManifestAdjudicationStatus,
    build_pending_page_capture_manifest,
    manifest_session_id,
)
from services.api.app.application.evidence_manifests import PersistedEvidenceManifest
from services.api.app.domain.evidence import (
    EvidenceContractValidationError,
    EvidenceManifestReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.exploration.evidence_exit_requirements import (
    EvidenceExitRequirementStatus,
    build_active_session_evidence_requirement,
    build_evidence_exit_requirement_set,
)

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "surface-session-1"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"


@dataclass(frozen=True)
class StubArtifact:
    artifact_id: str
    artifact_type: str
    storage_path: str
    checksum: str
    size_bytes: int
    content_type: str


def _artifact(
    *,
    artifact_id: str = "artifact-1",
    artifact_type: str = "screenshot",
    size_bytes: int = 10,
) -> StubArtifact:
    return StubArtifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        storage_path=f"{TASK_ID}/{CAPTURE_ID}/{artifact_id}.data",
        checksum=f"sha256:{artifact_id}",
        size_bytes=size_bytes,
        content_type="application/octet-stream",
    )


def _pending_manifest(
    artifacts: tuple[StubArtifact, ...] = (_artifact(),),
) -> EvidenceManifestReference:
    return build_pending_page_capture_manifest(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        capture_role="primary",
        artifacts=artifacts,
        created_at=NOW,
    )


def _requirement_set(
    *,
    required_artifact_types: tuple[str, ...] = ("screenshot",),
    required_capture_roles: tuple[str, ...] = ("primary",),
    min_screenshot_count: int = 0,
    status: EvidenceExitRequirementStatus = EvidenceExitRequirementStatus.PENDING,
    capture_id: str = CAPTURE_ID,
):
    requirement = build_active_session_evidence_requirement(
        task_id=TASK_ID,
        capture_id=capture_id,
        rule_id="rule-1",
        rule_code="alioth_1",
        required_artifact_types=required_artifact_types,
        required_capture_roles=required_capture_roles,
        min_screenshot_count=min_screenshot_count,
        allow_empty_visible_text=True,
        status=status,
        resolution_manifest_id=(
            "evidence-manifest:resolved"
            if status is EvidenceExitRequirementStatus.SATISFIED
            else None
        ),
    )
    return build_evidence_exit_requirement_set(
        revision=1,
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=capture_id,
        matcher_snapshot_hash="matcher-hash-1",
        requirements=(requirement,),
        created_at=NOW,
    )


class FakeManifestRepository:
    """镜像 Postgres 仓储的幂等语义：同版本同内容返回 created=False。"""

    def __init__(self) -> None:
        self.manifests: list[EvidenceManifestReference] = []
        self.add_calls = 0

    async def add(self, *, manifest: EvidenceManifestReference) -> PersistedEvidenceManifest:
        self.add_calls += 1
        for existing in self.manifests:
            if (
                existing.capture_id == manifest.capture_id
                and existing.manifest_version == manifest.manifest_version
            ):
                if existing.manifest_id == manifest.manifest_id:
                    return PersistedEvidenceManifest(manifest=existing, created=False)
                raise RuntimeError("Manifest 版本已被其他内容占用")
        self.manifests.append(manifest)
        return PersistedEvidenceManifest(manifest=manifest, created=True)

    async def get(self, *, manifest_id: str) -> EvidenceManifestReference | None:
        return next(
            (item for item in self.manifests if item.manifest_id == manifest_id),
            None,
        )

    async def get_latest_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        require_quality_pass: bool = False,
    ) -> EvidenceManifestReference | None:
        rows = [
            item
            for item in self.manifests
            if item.task_id == task_id
            and item.capture_id == capture_id
            and (not require_quality_pass or item.quality_status is EvidenceQualityStatus.PASS)
        ]
        return max(rows, key=lambda item: item.manifest_version, default=None)

    async def list_latest_for_session(self, **kwargs: Any) -> tuple[Any, ...]:
        del kwargs
        return ()


class FakeRequirementReader:
    def __init__(self, requirement_set=None) -> None:
        self.requirement_set = requirement_set
        self.calls: list[dict[str, str]] = []

    async def get_latest(self, *, task_id: str, session_id: str, capture_id: str):
        self.calls.append(
            {"task_id": task_id, "session_id": session_id, "capture_id": capture_id}
        )
        return self.requirement_set


def test_pending_manifest_carries_recomputable_hash_and_identity() -> None:
    artifacts = (_artifact(), _artifact(artifact_id="artifact-2", artifact_type="dom"))
    manifest = _pending_manifest(artifacts)

    assert manifest.manifest_version == 1
    assert manifest.quality_status is EvidenceQualityStatus.PENDING
    assert manifest.quality_reason == PENDING_QUALITY_REASON
    assert {item.role for item in manifest.artifacts} == {"primary"}
    assert {item.artifact_id for item in manifest.artifacts} == {"artifact-1", "artifact-2"}

    # 可复算：同一身份重建得到相同 manifest_hash 与 manifest_id
    rebuilt = build_evidence_manifest_reference(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        manifest_version=1,
        artifacts=manifest.artifacts,
        quality_status=EvidenceQualityStatus.PENDING,
        quality_reason=PENDING_QUALITY_REASON,
        created_at=NOW,
    )
    assert rebuilt.manifest_hash == manifest.manifest_hash
    assert rebuilt.manifest_id == manifest.manifest_id

    # 序列化回读时域合同重新校验哈希，篡改无法通过
    restored = EvidenceManifestReference.from_dict(manifest.to_dict())
    assert restored == manifest
    tampered = manifest.to_dict()
    tampered["manifest_hash"] = "sha256:forged"
    with pytest.raises(EvidenceContractValidationError):
        EvidenceManifestReference.from_dict(tampered)


def test_pending_manifest_requires_at_least_one_artifact() -> None:
    with pytest.raises(EvidenceContractValidationError):
        build_pending_page_capture_manifest(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            capture_id=CAPTURE_ID,
            capture_role="primary",
            artifacts=(),
            created_at=NOW,
        )


def test_manifest_rejects_capture_role_outside_frozen_vocabulary() -> None:
    """E01 条款 3.1：E07 起 ArtifactReference.role 强制校验属于取值表。"""
    from services.api.app.domain.evidence.vocabulary import EvidenceVocabularyError

    with pytest.raises(EvidenceVocabularyError, match="不在 E01 冻结词汇内"):
        build_pending_page_capture_manifest(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            capture_id=CAPTURE_ID,
            capture_role="not_a_frozen_role",
            artifacts=(_artifact(),),
            created_at=NOW,
        )


@pytest.mark.parametrize(
    "capture_role",
    ["primary", "before_action", "after_action", "bootstrap", "observation"],
)
def test_manifest_accepts_frozen_supply_and_placeholder_roles(capture_role: str) -> None:
    """E01 取值表 2.1 供给角色与 2.3 持久层占位值都在清单允许集合内。"""
    manifest = build_pending_page_capture_manifest(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        capture_role=capture_role,
        artifacts=(_artifact(),),
        created_at=NOW,
    )

    assert {item.role for item in manifest.artifacts} == {capture_role}


def test_manifest_session_identity_prefers_surface_session() -> None:
    assert manifest_session_id("surface-1", agent_run_id="run-1") == "surface-1"
    assert manifest_session_id("  ", agent_run_id="run-1") == "agent-run:run-1"
    assert manifest_session_id(None, agent_run_id="run-1") == "agent-run:run-1"


async def test_adjudicate_returns_no_manifest_when_capture_has_none() -> None:
    repository = FakeManifestRepository()
    writer = EvidenceManifestWriter(manifest_repository=repository)

    outcome = await writer.adjudicate_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert outcome.status is ManifestAdjudicationStatus.NO_MANIFEST
    assert outcome.manifest is None
    assert repository.add_calls == 0


async def test_adjudicate_keeps_pending_without_requirement_set() -> None:
    repository = FakeManifestRepository()
    await repository.add(manifest=_pending_manifest())
    reader = FakeRequirementReader(requirement_set=None)
    writer = EvidenceManifestWriter(
        manifest_repository=repository,
        requirement_repository=reader,
    )

    outcome = await writer.adjudicate_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert outcome.status is ManifestAdjudicationStatus.AWAITING_REQUIREMENT_SET
    assert outcome.manifest is not None
    assert outcome.manifest.quality_status is EvidenceQualityStatus.PENDING
    assert len(repository.manifests) == 1
    # 需求集查询按清单 session 身份读取同一仓储（E14 同源）
    assert reader.calls == [
        {"task_id": TASK_ID, "session_id": SESSION_ID, "capture_id": CAPTURE_ID}
    ]


async def test_adjudicate_appends_pass_version_when_requirements_met() -> None:
    repository = FakeManifestRepository()
    await repository.add(manifest=_pending_manifest())
    writer = EvidenceManifestWriter(
        manifest_repository=repository,
        requirement_repository=FakeRequirementReader(_requirement_set()),
        now=lambda: NOW,
    )

    outcome = await writer.adjudicate_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert outcome.status is ManifestAdjudicationStatus.ADJUDICATED
    assert outcome.created is True
    assert outcome.shortfalls == ()
    assert outcome.manifest is not None
    assert outcome.manifest.manifest_version == 2
    assert outcome.manifest.quality_status is EvidenceQualityStatus.PASS
    assert outcome.manifest.quality_reason == PASS_QUALITY_REASON
    assert outcome.manifest.artifacts == repository.manifests[0].artifacts


async def test_adjudicate_appends_fail_version_with_shortfalls() -> None:
    repository = FakeManifestRepository()
    await repository.add(manifest=_pending_manifest())
    requirement_set = _requirement_set(
        required_artifact_types=("screenshot", "visible_text"),
        min_screenshot_count=2,
    )
    writer = EvidenceManifestWriter(
        manifest_repository=repository,
        requirement_repository=FakeRequirementReader(requirement_set),
        now=lambda: NOW,
    )

    outcome = await writer.adjudicate_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert outcome.status is ManifestAdjudicationStatus.ADJUDICATED
    assert outcome.manifest is not None
    assert outcome.manifest.manifest_version == 2
    assert outcome.manifest.quality_status is EvidenceQualityStatus.FAIL
    assert outcome.manifest.quality_reason is not None
    assert "alioth_1" in outcome.manifest.quality_reason
    assert "缺少证据类型" in outcome.manifest.quality_reason
    assert len(outcome.shortfalls) == 1
    shortfall = outcome.shortfalls[0]
    assert shortfall.missing_artifact_types == ("visible_text",)
    assert shortfall.screenshot_shortfall is True


async def test_adjudicate_is_idempotent_after_terminal_version() -> None:
    repository = FakeManifestRepository()
    await repository.add(manifest=_pending_manifest())
    writer = EvidenceManifestWriter(
        manifest_repository=repository,
        requirement_repository=FakeRequirementReader(_requirement_set()),
        now=lambda: NOW,
    )

    first = await writer.adjudicate_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)
    second = await writer.adjudicate_for_capture(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert first.status is ManifestAdjudicationStatus.ADJUDICATED
    assert second.status is ManifestAdjudicationStatus.ALREADY_TERMINAL
    assert second.manifest == first.manifest
    # 终态后不再追加版本：仓储只有 v1 pending 与 v2 终态两行
    assert [item.manifest_version for item in repository.manifests] == [1, 2]


async def test_adjudicate_uses_directly_supplied_requirement_set() -> None:
    repository = FakeManifestRepository()
    await repository.add(manifest=_pending_manifest())
    writer = EvidenceManifestWriter(manifest_repository=repository, now=lambda: NOW)

    outcome = await writer.adjudicate_for_capture(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        requirement_set=_requirement_set(),
    )

    assert outcome.status is ManifestAdjudicationStatus.ADJUDICATED
    assert outcome.manifest is not None
    assert outcome.manifest.quality_status is EvidenceQualityStatus.PASS


async def test_adjudicate_rejects_requirement_set_of_other_capture() -> None:
    repository = FakeManifestRepository()
    await repository.add(manifest=_pending_manifest())
    foreign = _requirement_set(capture_id="55555555-5555-5555-5555-555555555555")
    writer = EvidenceManifestWriter(manifest_repository=repository)

    with pytest.raises(ValueError, match="Capture 归属不一致"):
        await writer.adjudicate_for_capture(
            task_id=TASK_ID,
            capture_id=CAPTURE_ID,
            requirement_set=foreign,
        )
    assert [item.manifest_version for item in repository.manifests] == [1]


async def test_terminal_requirement_members_do_not_block_pass() -> None:
    """终态需求成员不翻案：已 satisfied 的需求缺证据也不再拉低裁决。"""
    repository = FakeManifestRepository()
    await repository.add(manifest=_pending_manifest())
    requirement_set = _requirement_set(
        required_artifact_types=("har",),
        status=EvidenceExitRequirementStatus.SATISFIED,
    )
    writer = EvidenceManifestWriter(manifest_repository=repository, now=lambda: NOW)

    outcome = await writer.adjudicate_for_capture(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        requirement_set=requirement_set,
    )

    assert outcome.status is ManifestAdjudicationStatus.ADJUDICATED
    assert outcome.manifest is not None
    assert outcome.manifest.quality_status is EvidenceQualityStatus.PASS


async def test_repository_add_is_idempotent_for_same_content() -> None:
    """E10：重复生成不产生重复行（同版本同内容 created=False）。"""
    repository = FakeManifestRepository()
    manifest = _pending_manifest()

    first = await repository.add(manifest=manifest)
    second = await repository.add(manifest=manifest)

    assert first.created is True
    assert second.created is False
    assert len(repository.manifests) == 1


class FakeRequirementRepository(FakeRequirementReader):
    """带追加能力的需求仓储替身：镜像 revision 追加语义。"""

    def __init__(self, requirement_set=None) -> None:
        super().__init__(requirement_set)
        self.added: list[Any] = []

    async def get_latest(self, *, task_id: str, session_id: str, capture_id: str):
        if self.added:
            return self.added[-1]
        return await super().get_latest(
            task_id=task_id, session_id=session_id, capture_id=capture_id
        )

    async def add(self, *, requirement_set):
        self.added.append(requirement_set)
        return requirement_set


async def test_pass_adjudication_marks_pending_requirements_satisfied() -> None:
    """裁决 pass 后需求若停在 pending，离场门禁按非终态永远阻断。"""
    manifests = FakeManifestRepository()
    manifests.manifests.append(_pending_manifest())
    requirements = FakeRequirementRepository(_requirement_set())
    writer = EvidenceManifestWriter(
        manifest_repository=manifests,
        requirement_repository=requirements,
        now=lambda: NOW,
    )

    outcome = await writer.adjudicate_for_capture(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        session_id=SESSION_ID,
    )

    assert outcome.manifest.quality_status is EvidenceQualityStatus.PASS
    (satisfied_set,) = requirements.added
    assert satisfied_set.revision == 2
    assert satisfied_set.nonterminal_count == 0
    (member,) = satisfied_set.requirements
    assert member.status is EvidenceExitRequirementStatus.SATISFIED
    assert member.resolution_manifest_id == outcome.manifest.manifest_id


async def test_already_terminal_pass_repairs_pending_requirements() -> None:
    """清单 pass 与需求 satisfied 两步写入之间中断后，重入必须幂等修复。"""
    manifests = FakeManifestRepository()
    pending = _pending_manifest()
    manifests.manifests.append(pending)
    requirements = FakeRequirementRepository(_requirement_set())
    writer = EvidenceManifestWriter(
        manifest_repository=manifests,
        requirement_repository=requirements,
        now=lambda: NOW,
    )
    first = await writer.adjudicate_for_capture(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        session_id=SESSION_ID,
    )
    assert first.manifest.quality_status is EvidenceQualityStatus.PASS
    # 模拟 satisfied 版本丢失（写入中断）：需求回到 pending 快照
    requirements.added.clear()

    second = await writer.adjudicate_for_capture(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        session_id=SESSION_ID,
    )

    assert second.status is ManifestAdjudicationStatus.ALREADY_TERMINAL
    (repaired,) = requirements.added
    assert repaired.nonterminal_count == 0


async def test_fail_adjudication_keeps_requirements_pending() -> None:
    """fail 结论不得伪造 satisfied：需求终态只能由 pass 清单解决。"""
    manifests = FakeManifestRepository()
    manifests.manifests.append(_pending_manifest())
    requirements = FakeRequirementRepository(
        _requirement_set(required_artifact_types=("screenshot", "har"))
    )
    writer = EvidenceManifestWriter(
        manifest_repository=manifests,
        requirement_repository=requirements,
        now=lambda: NOW,
    )

    outcome = await writer.adjudicate_for_capture(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        session_id=SESSION_ID,
    )

    assert outcome.manifest.quality_status is EvidenceQualityStatus.FAIL
    assert requirements.added == []
