"""规则确定性判定的可信证据包读取与持久化实现。"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import PurePosixPath
from typing import Any

from services.api.app.application.evidence_presentation import (
    presentation_from_capture_row,
)
from services.api.app.application.observation_packet import (
    ObservationPacket,
    build_observation_input_fingerprint,
)
from services.api.app.application.page_capture_persistence import EvidenceArtifactStore
from services.api.app.application.rule_judge import (
    RuleJudgeEvidenceInsufficientError,
    RuleJudgeValidationError,
)
from services.api.app.core.config import get_settings
from services.api.app.domain.evidence.presentation import PresentationAssessment
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.rule_judge_evidence_read_batch import EvidenceReadBatchCache
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

_CAPTURE_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TRUSTED_SOURCES = frozenset({"baseline", "rule_supplement", "app_runtime"})


@dataclass(frozen=True)
class TrustedEvidenceArtifact:
    """已校验归属、摘要和存储路径的单个证据产物。"""

    artifact_id: str
    artifact_type: str
    page_capture_id: str
    capture_role: str
    content_type: str
    payload: bytes

    def decode_text(self) -> str:
        """按 UTF-8 严格读取文本证据。"""
        try:
            value = self.payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RuleJudgeValidationError(f"{self.artifact_type} 证据不是有效 UTF-8") from exc
        if not value.strip():
            raise RuleJudgeEvidenceInsufficientError(f"{self.artifact_type} 证据内容为空")
        return value

    def decode_json(self) -> Any:
        """读取 JSON 证据并拒绝非法载荷。"""
        try:
            return json.loads(self.decode_text())
        except json.JSONDecodeError as exc:
            raise RuleJudgeValidationError(f"{self.artifact_type} 证据不是有效 JSON") from exc


@dataclass(frozen=True)
class TrustedEvidenceBundle:
    """单次规则判定的标准可信 Observation Packet。"""

    observation_packet_id: str
    page_capture_id: str
    capture_roles: dict[str, str]
    artifacts: tuple[TrustedEvidenceArtifact, ...]
    # 采集 ID -> 持久化的展示层评估（专项七 §5.2）；观察包建不出来时（如收口补判的
    # 采集不属于当前判定 Run）判定侧靠它执行同一套展示层门禁，诊断材料不得判 pass。
    capture_presentations: Mapping[str, PresentationAssessment | None] = field(default_factory=dict)

    def artifacts_of_type(self, artifact_type: str) -> tuple[TrustedEvidenceArtifact, ...]:
        """按类型返回证据，保持创建顺序。"""
        return tuple(item for item in self.artifacts if item.artifact_type == artifact_type)

    def require_unique(self, artifact_type: str) -> TrustedEvidenceArtifact:
        """整包不唯一时，要求 primary Capture 内该类型恰好一份。"""
        matches = self.artifacts_of_type(artifact_type)
        if len(matches) == 1:
            return matches[0]
        primary_capture_id = self.capture_roles.get("primary")
        primary_matches = tuple(
            item for item in matches if item.page_capture_id == primary_capture_id
        )
        if artifact_type == "ui_snapshot" and len(primary_matches) > 1:
            baseline_matches = tuple(
                item for item in primary_matches
                if not (
                    isinstance(payload := item.decode_json(), dict)
                    and payload.get("schema_version") == "ui-snapshot-sequence.v1"
                    and payload.get("sequence") is True
                    and isinstance(payload.get("frames"), list)
                )
            )
            if len(baseline_matches) == 1:
                return baseline_matches[0]
        if len(primary_matches) != 1:
            raise RuleJudgeValidationError(f"规则判定必须引用唯一的 {artifact_type} 证据")
        return primary_matches[0]


class PostgresRuleJudgeEvidenceReader:
    """校验证据后创建 Observation Packet，并从配置存储读取原文。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        artifact_store: EvidenceArtifactStore | None = None,
        manifest_loader: Any | None = None,
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._artifact_store = artifact_store or build_evidence_artifact_store(
            get_settings().evidence_storage
        )
        # E22：非 None 时规则引擎取证走清单，不再直查附件表
        self._manifest_loader = manifest_loader
        self._artifact_read_batches = EvidenceReadBatchCache()

    def read_batch(self):
        """隔离一次判定批次的原文复用，逐规则归属和摘要校验仍照常执行。"""
        return self._artifact_read_batches.scope()

    async def read_green_fact_context(self, *, task_id, capture_roles, source_agent_run_id):
        """只为显式绿色事实绑定提供正式动作，普通规则读取行为不变。"""
        from services.api.app.infrastructure.db.green_page_action_context import (
            read_green_page_action_context,
        )

        pool = self._pool or await get_postgres_pool(self._database_url)
        return await read_green_page_action_context(
            pool, task_id=task_id, capture_roles=capture_roles,
            source_agent_run_id=source_agent_run_id,
        )

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
        """校验跨采集证据并创建单次判定的标准观察包。

        E22：配置了清单加载器时，规则引擎取证同样改由清单来源，
        附件表不再参与判定读取；未配置时沿用旧读取（默认 off 行为）。
        """
        source_page_capture_id = evidence_source_page_capture_id or page_capture_id
        capture_roles = _normalize_capture_roles(
            page_capture_id=source_page_capture_id,
            evidence_capture_roles=evidence_capture_roles,
        )
        artifact_ids = _normalize_artifact_ids(
            evidence_artifact_ids,
            allow_empty=True,
        )
        manifest_refs = dict(evidence_manifest_refs or {})
        retained_ids_by_role = {
            role: set(retained_ids)
            for role, retained_ids in (retained_baseline_artifact_ids or {}).items()
        }
        has_exact_primary_manifest = evidence_manifest_id is not None or "primary" in manifest_refs
        project_unlisted_nonprimary_artifacts = (
            bool(artifact_ids) and len(capture_roles) > 1 and has_exact_primary_manifest
        )
        pool = self._pool or await get_postgres_pool(self._database_url)
        if source_agent_run_id is not None:
            await _validate_capture_run_ownership(
                pool=pool,
                task_id=task_id,
                capture_ids=tuple(capture_roles.values()),
                source_agent_run_id=source_agent_run_id,
            )
        if self._manifest_loader is not None:
            return await self._prepare_from_manifests(
                task_id=task_id,
                agent_run_id=agent_run_id,
                rule_id=rule_id,
                page_capture_id=page_capture_id,
                evidence_manifest_id=evidence_manifest_id,
                capture_roles=capture_roles,
                evidence_manifest_refs=evidence_manifest_refs,
                retained_baseline_artifact_ids=retained_baseline_artifact_ids,
                artifact_ids=artifact_ids,
                required_artifact_types=required_artifact_types,
                frozen_observation_packet=frozen_observation_packet,
            )
        async with pool.acquire() as connection:
            artifact_filter = ""
            if not project_unlisted_nonprimary_artifacts:
                artifact_filter = """
                  AND (
                      (
                          cardinality($3::uuid[]) > 0
                          AND evidence.id = ANY($3::uuid[])
                      )
                      OR (
                          cardinality($3::uuid[]) = 0
                          -- 未显式指定附件时必须读回 Capture 下全部可信制品，
                          -- 再由下方统一校验必需类型；否则缺少 visible_text 等类型时，
                          -- SQL 先把其他制品过滤掉，判定入口会误报"证据采集角色未引用有效证据"。
                      )
                  )
                """
            else:
                artifact_filter = """
                  AND (
                      cardinality($3::uuid[]) = 0
                      OR evidence.id = ANY($3::uuid[])
                      OR evidence.page_capture_id = ANY($2::uuid[])
                  )
                """
            rows = await connection.fetch(
                f"""
                SELECT
                    evidence.id::text AS artifact_id,
                    evidence.page_capture_id::text AS page_capture_id,
                    evidence.artifact_type,
                    evidence.source,
                    evidence.storage_path,
                    evidence.content_type,
                    evidence.checksum,
                    evidence.size_bytes
                FROM qc_evidence_artifact AS evidence
                INNER JOIN qc_capture AS capture
                    ON capture.id = evidence.page_capture_id
                   AND capture.task_id = evidence.task_id
                  WHERE capture.task_id = $1::uuid
                  AND capture.id = ANY($2::uuid[])
                  AND capture.capture_status = 'captured'
                  {artifact_filter}
                  AND evidence.source IN ('baseline', 'rule_supplement', 'app_runtime')
                ORDER BY evidence.created_at, evidence.id
                """,
                task_id,
                list(capture_roles.values()),
                list(artifact_ids),
            )

        rows_by_id = {str(row["artifact_id"]): row for row in rows}
        missing_ids = [item for item in artifact_ids if item not in rows_by_id]
        if missing_ids:
            raise RuleJudgeValidationError(
                "证据附件不存在、未捕获或不属于声明的页面采集：" + ", ".join(missing_ids)
            )
        referenced_capture_ids = {str(row["page_capture_id"]) for row in rows}
        missing_capture_ids = sorted(set(capture_roles.values()) - referenced_capture_ids)
        if missing_capture_ids:
            raise RuleJudgeValidationError(
                "证据采集角色未引用有效证据：" + ", ".join(missing_capture_ids)
            )
        role_by_capture = {capture_id: role for role, capture_id in capture_roles.items()}
        selected_rows: list[Any] = []
        selected_counts = dict.fromkeys(capture_roles, 0)
        for role, capture_id in capture_roles.items():
            role_rows = [row for row in rows if str(row["page_capture_id"]) == capture_id]
            role_ids = {str(row["artifact_id"]) for row in role_rows}
            explicit_role_ids = tuple(
                artifact_id for artifact_id in artifact_ids if artifact_id in role_ids
            )
            if explicit_role_ids:
                selected_role_rows = [rows_by_id[artifact_id] for artifact_id in explicit_role_ids]
            elif project_unlisted_nonprimary_artifacts and role != "primary":
                retained_ids = retained_ids_by_role.get(role, set())
                selected_role_rows = [
                    row
                    for row in role_rows
                    if not retained_ids or str(row["artifact_id"]) in retained_ids
                ]
            elif not artifact_ids:
                retained_ids = retained_ids_by_role.get(role, set())
                selected_role_rows = [
                    row
                    for row in role_rows
                    if not retained_ids or str(row["artifact_id"]) in retained_ids
                ]
            else:
                selected_role_rows = []
            selected_rows.extend(selected_role_rows)
            selected_counts[role] = len(selected_role_rows)
        retained_roles = {
            role for role, retained_ids in retained_ids_by_role.items() if retained_ids
        }
        missing_selected_roles = [
            role for role in retained_roles if selected_counts.get(role, 0) == 0
        ]
        if missing_selected_roles:
            raise RuleJudgeValidationError(
                "证据采集角色未引用有效证据：" + ", ".join(missing_selected_roles)
            )
        artifacts: list[TrustedEvidenceArtifact] = []
        for row in selected_rows:
            artifact_id = str(row["artifact_id"])
            capture_id = str(row["page_capture_id"])
            source = str(row["source"])
            if source not in _TRUSTED_SOURCES:
                raise RuleJudgeValidationError(f"证据来源不可信：{source}")
            storage_path = str(row["storage_path"])
            if PurePosixPath(storage_path).parts[:2] != (task_id, capture_id):
                raise RuleJudgeValidationError("证据存储路径归属不匹配")
            checksum = str(row["checksum"] or "")
            size_bytes = int(row["size_bytes"]) if row["size_bytes"] is not None else None
            payload = await self._artifact_read_batches.read(
                key=(storage_path, checksum, size_bytes),
                load=partial(self._artifact_store.read, storage_path),
                validate=partial(
                    _validate_payload,
                    artifact_type=str(row["artifact_type"]),
                    content_type=str(row["content_type"] or ""),
                    checksum=checksum,
                    size_bytes=size_bytes,
                ),
            )
            artifacts.append(
                TrustedEvidenceArtifact(
                    artifact_id=artifact_id,
                    artifact_type=str(row["artifact_type"]),
                    page_capture_id=capture_id,
                    capture_role=role_by_capture[capture_id],
                    content_type=str(row["content_type"] or ""),
                    payload=payload,
                )
            )

        available_types = {item.artifact_type for item in artifacts}
        missing_types = sorted(set(required_artifact_types) - available_types)
        if missing_types:
            raise RuleJudgeEvidenceInsufficientError(
                "规则判定缺少必需证据类型：" + ", ".join(missing_types)
            )
        observation_packet_id = await self._create_observation_packet(
            pool=pool,
            task_id=task_id,
            agent_run_id=agent_run_id,
            rule_id=rule_id,
            page_capture_id=page_capture_id,
            capture_roles=capture_roles,
            artifacts=artifacts,
            frozen_observation_packet=frozen_observation_packet,
        )
        capture_presentations = await _load_capture_presentations(
            pool=pool,
            task_id=task_id,
            capture_ids=tuple(capture_roles.values()),
        )
        return TrustedEvidenceBundle(
            observation_packet_id=observation_packet_id,
            page_capture_id=page_capture_id,
            capture_roles=capture_roles,
            artifacts=tuple(artifacts),
            capture_presentations=capture_presentations,
        )

    async def read_visible_text(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        evidence_artifact_ids: tuple[str, ...],
    ) -> str:
        """兼容旧调用：读取同一页面采集下唯一可见文本。"""
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    evidence.id::text AS artifact_id,
                    evidence.page_capture_id::text AS page_capture_id,
                    evidence.artifact_type,
                    evidence.source,
                    evidence.storage_path,
                    evidence.content_type,
                    evidence.checksum,
                    evidence.size_bytes
                FROM qc_evidence_artifact AS evidence
                INNER JOIN qc_capture AS capture
                    ON capture.id = evidence.page_capture_id
                   AND capture.task_id = evidence.task_id
                WHERE capture.id = $1::uuid
                  AND capture.task_id = $2::uuid
                  AND capture.capture_status = 'captured'
                  AND evidence.id = ANY($3::uuid[])
                  AND evidence.source IN ('baseline', 'rule_supplement', 'app_runtime')
                  AND evidence.artifact_type = 'visible_text'
                ORDER BY evidence.created_at, evidence.id
                """,
                page_capture_id,
                task_id,
                list(evidence_artifact_ids),
            )
        if len(rows) != 1:
            raise RuleJudgeValidationError(
                "technical_error_text 必须引用当前页面唯一的 visible_text 证据"
            )
        row = rows[0]
        storage_path = str(row["storage_path"])
        if PurePosixPath(storage_path).parts[:2] != (task_id, page_capture_id):
            raise RuleJudgeValidationError("visible_text 证据存储路径归属不匹配")
        payload = await self._artifact_store.read(storage_path)
        _validate_payload(
            payload,
            artifact_type="visible_text",
            content_type=str(row["content_type"] or ""),
            checksum=str(row["checksum"] or ""),
            size_bytes=(int(row["size_bytes"]) if row["size_bytes"] is not None else None),
        )
        return TrustedEvidenceArtifact(
            artifact_id=str(row["artifact_id"]),
            artifact_type="visible_text",
            page_capture_id=page_capture_id,
            capture_role="primary",
            content_type=str(row["content_type"] or ""),
            payload=payload,
        ).decode_text()

    async def _prepare_from_manifests(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        rule_id: str,
        page_capture_id: str,
        evidence_manifest_id: str | None,
        capture_roles: dict[str, str],
        evidence_manifest_refs: dict[str, dict[str, str]] | None,
        retained_baseline_artifact_ids: dict[str, tuple[str, ...]] | None,
        artifact_ids: tuple[str, ...],
        required_artifact_types: tuple[str, ...],
        frozen_observation_packet: ObservationPacket | None = None,
    ) -> TrustedEvidenceBundle:
        """E22：按清单读取规则引擎所需证据，附件表不参与。"""
        from services.api.app.application.manifest_backed_packet import (
            ManifestBackedReadError,
        )

        role_by_capture = {capture_id: role for role, capture_id in capture_roles.items()}
        manifest_refs = dict(evidence_manifest_refs or {})
        retained_ids_by_role = {
            role: set(artifact_ids)
            for role, artifact_ids in (retained_baseline_artifact_ids or {}).items()
        }
        has_exact_primary_manifest = evidence_manifest_id is not None or "primary" in manifest_refs
        artifacts: list[TrustedEvidenceArtifact] = []
        quality_status = "pass"
        wanted_ids = set(artifact_ids)
        selected_counts = dict.fromkeys(capture_roles, 0)
        for capture_id in capture_roles.values():
            role = role_by_capture[capture_id]
            manifest_ref = manifest_refs.get(role)
            try:
                if manifest_ref is not None:
                    packet = await self._manifest_loader.load_for_manifest(
                        task_id=task_id,
                        manifest_id=manifest_ref["manifest_id"],
                    )
                    if packet.manifest_id != manifest_ref["manifest_id"]:
                        raise RuleJudgeValidationError("指定 Manifest 返回了不匹配的清单身份")
                    if packet.capture_id != capture_id:
                        raise RuleJudgeValidationError("指定 Manifest 不属于声明的证据 Capture")
                    if str(packet.manifest_version) != manifest_ref["manifest_version"]:
                        raise RuleJudgeValidationError("指定 Manifest 返回了不匹配的清单版本")
                elif evidence_manifest_id is not None and role == "primary":
                    # 多角色补采：精确 Manifest 只钉 primary；before 等按 Capture 取 PASS。
                    packet = await self._manifest_loader.load_for_manifest(
                        task_id=task_id,
                        manifest_id=evidence_manifest_id,
                    )
                    if (
                        packet.capture_id != capture_id
                        or packet.manifest_id != evidence_manifest_id
                    ):
                        raise RuleJudgeValidationError("指定 Manifest 不属于声明的证据 Capture")
                else:
                    # 单个补采 Manifest 只锁定 primary；其它冻结角色仍按各自
                    # Capture 的清单读取，不能把 primary 清单套到 before 等角色。
                    packet = await self._manifest_loader.load_for_capture(
                        task_id=task_id,
                        capture_id=capture_id,
                    )
            except ManifestBackedReadError as exc:
                raise RuleJudgeEvidenceInsufficientError(
                    f"Capture {capture_id} 的证据清单不可用：{exc}"
                ) from exc
            if packet.quality_status.value != "pass":
                quality_status = packet.quality_status.value
            packet_artifact_ids = {item.artifact_id for item in packet.artifacts}
            role_has_explicit_artifact_ids = bool(wanted_ids & packet_artifact_ids)
            for item in packet.artifacts:
                if (
                    wanted_ids
                    and item.artifact_id not in wanted_ids
                    and not (
                        has_exact_primary_manifest
                        and role != "primary"
                        and not role_has_explicit_artifact_ids
                    )
                ):
                    continue
                retained_ids = retained_ids_by_role.get(role, set())
                if (
                    not role_has_explicit_artifact_ids
                    and role != "primary"
                    and retained_ids
                    and item.artifact_id not in retained_ids
                ):
                    continue
                # 补采只显式锁定新的 primary；before 等基线角色没有独立
                # Artifact ID 集合时，保留其冻结清单中的全部材料。
                # 规则未声明必需证据类型时不限定类型，与附件表读取路径保持同一口径。
                if (
                    not wanted_ids
                    and required_artifact_types
                    and item.artifact_type not in required_artifact_types
                ):
                    continue
                artifacts.append(
                    TrustedEvidenceArtifact(
                        artifact_id=item.artifact_id,
                        artifact_type=item.artifact_type,
                        page_capture_id=capture_id,
                        capture_role=role_by_capture[capture_id],
                        content_type=item.content_type or "",
                        payload=item.payload,
                    )
                )
                selected_counts[role] += 1
        missing_ids = sorted(wanted_ids - {item.artifact_id for item in artifacts})
        if missing_ids:
            raise RuleJudgeValidationError(
                "证据附件未出现在对应 Capture 的清单中：" + ", ".join(missing_ids)
            )
        missing_types = sorted(
            set(required_artifact_types) - {item.artifact_type for item in artifacts}
        )
        if missing_types:
            raise RuleJudgeEvidenceInsufficientError(
                "规则判定缺少必需证据类型：" + ", ".join(missing_types)
            )
        retained_roles = {
            role for role, retained_ids in retained_ids_by_role.items() if retained_ids
        }
        missing_roles = [role for role in retained_roles if selected_counts.get(role, 0) == 0]
        if missing_roles:
            raise RuleJudgeValidationError(
                "证据采集角色未引用有效证据：" + ", ".join(missing_roles)
            )
        pool = self._pool or await get_postgres_pool(self._database_url)
        # 清单路径此前没有其它读取：展示层评估放在观察包写入之前，保持写入是最后一次调用。
        capture_presentations = await _load_capture_presentations(
            pool=pool,
            task_id=task_id,
            capture_ids=tuple(capture_roles.values()),
        )
        observation_packet_id = await self._create_observation_packet(
            pool=pool,
            task_id=task_id,
            agent_run_id=agent_run_id,
            rule_id=rule_id,
            page_capture_id=page_capture_id,
            capture_roles=capture_roles,
            artifacts=artifacts,
            quality_gate_status=quality_status,
            frozen_observation_packet=frozen_observation_packet,
        )
        return TrustedEvidenceBundle(
            observation_packet_id=observation_packet_id,
            page_capture_id=page_capture_id,
            capture_roles=capture_roles,
            artifacts=tuple(artifacts),
            capture_presentations=capture_presentations,
        )

    async def _reuse_frozen_observation_packet(
        self,
        *,
        pool: Any,
        task_id: str,
        agent_run_id: str | None,
        rule_id: str,
        page_capture_id: str,
        capture_roles: dict[str, str],
        artifacts: list[TrustedEvidenceArtifact],
        quality_gate_status: str,
        packet: ObservationPacket,
    ) -> str:
        """附件读取仍校验归属和摘要，正式冻结输入只读核对后沿用。"""
        if (packet.task_id, packet.page_capture_id, packet.rule_id) != (
            task_id,
            page_capture_id,
            rule_id,
        ):
            raise RuleJudgeValidationError("正式观察包不属于当前任务、采集或规则")
        identity = packet.packet_payload.get("identity")
        frozen_roles = identity.get("capture_roles") if isinstance(identity, dict) else None
        if not isinstance(frozen_roles, dict) or any(
            frozen_roles.get(role) != capture for role, capture in capture_roles.items()
        ):
            raise RuleJudgeValidationError("本次读取角色与正式冻结观察包不一致")
        if {item.artifact_id for item in artifacts} - set(packet.evidence_artifact_ids):
            raise RuleJudgeValidationError("本次读取附件未包含在正式冻结观察包中")
        if packet.quality_gate_status != "pass" or quality_gate_status != "pass":
            raise RuleJudgeEvidenceInsufficientError("正式观察包或本次附件读取尚未通过证据门禁")
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id::text AS observation_packet_id,
                       evidence_artifact_ids::text AS evidence_artifact_ids,
                       packet_payload::text AS packet_payload,
                       quality_gate_status, quality_gate_reason
                FROM qc_observation_packet
                WHERE id=$1::uuid AND task_id=$2::uuid AND page_capture_id=$3::uuid
                  AND rule_id=$4::uuid AND created_by_agent_run_id IS NOT DISTINCT FROM $5::uuid
                """,
                packet.observation_packet_id,
                task_id,
                page_capture_id,
                rule_id,
                agent_run_id,
            )
        if row is None:
            raise RuleJudgeValidationError("正式冻结观察包不存在或不属于当前消费 Run")
        _ensure_frozen_packet_matches(
            row=row,
            expected_artifact_ids=list(packet.evidence_artifact_ids),
            expected_payload=packet.packet_payload,
            expected_quality_gate_status=packet.quality_gate_status,
            expected_quality_gate_reason=packet.quality_gate_reason,
        )
        return str(row["observation_packet_id"])

    async def _create_observation_packet(
        self,
        *,
        pool: Any,
        task_id: str,
        agent_run_id: str | None,
        rule_id: str,
        page_capture_id: str,
        capture_roles: dict[str, str],
        artifacts: list[TrustedEvidenceArtifact],
        quality_gate_status: str = "pass",
        frozen_observation_packet: ObservationPacket | None = None,
    ) -> str:
        if frozen_observation_packet is not None:
            return await self._reuse_frozen_observation_packet(
                pool=pool,
                task_id=task_id,
                agent_run_id=agent_run_id,
                rule_id=rule_id,
                page_capture_id=page_capture_id,
                capture_roles=capture_roles,
                artifacts=artifacts,
                quality_gate_status=quality_gate_status,
                packet=frozen_observation_packet,
            )
        payload = {
            "schema_version": "1.0",
            "capture_roles": capture_roles,
            "artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "artifact_type": item.artifact_type,
                    "page_capture_id": item.page_capture_id,
                    "capture_role": item.capture_role,
                }
                for item in artifacts
            ],
        }
        input_fingerprint = build_observation_input_fingerprint(
            evidence_artifact_ids=[item.artifact_id for item in artifacts],
            capture_roles=capture_roles,
        )
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_observation_packet AS packet (
                    task_id,
                    page_capture_id,
                    rule_id,
                    evidence_artifact_ids,
                    packet_payload,
                    quality_gate_status,
                    quality_gate_reason,
                    created_by_agent_run_id,
                    input_fingerprint
                )
                SELECT
                    task.id,
                    $3::uuid,
                    rule.id,
                    $4::jsonb,
                    $5::jsonb,
                    $7,
                    $8,
                    $6::uuid,
                    $9
                FROM qc_task AS task
                INNER JOIN qc_rule AS rule
                    ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                   AND rule.id = $2::uuid
                WHERE task.id = $1::uuid
                  AND (
                      $6::uuid IS NULL
                      OR EXISTS (
                          SELECT 1 FROM qc_agent_run AS run
                          WHERE run.id = $6::uuid AND run.task_id = task.id
                      )
                  )
                ORDER BY rule.sort_order
                LIMIT 1
                ON CONFLICT (
                    created_by_agent_run_id,
                    page_capture_id,
                    rule_id,
                    input_fingerprint
                )
                WHERE created_by_agent_run_id IS NOT NULL
                  AND page_capture_id IS NOT NULL
                  AND rule_id IS NOT NULL
                  AND input_fingerprint IS NOT NULL
                -- Observation Packet 一经创建即冻结；冲突时只读回既有输入。
                DO UPDATE SET updated_at = packet.updated_at
                RETURNING
                    packet.id::text AS observation_packet_id,
                    packet.evidence_artifact_ids::text AS evidence_artifact_ids,
                    packet.packet_payload::text AS packet_payload,
                    packet.quality_gate_status AS quality_gate_status,
                    packet.quality_gate_reason AS quality_gate_reason
                """,
                task_id,
                rule_id or None,
                page_capture_id,
                json.dumps([item.artifact_id for item in artifacts]),
                json.dumps(payload, ensure_ascii=False),
                agent_run_id,
                quality_gate_status,
                _packet_quality_reason(quality_gate_status),
                input_fingerprint,
            )
        if row is None:
            raise RuleJudgeValidationError("无法为当前任务、规则和 Agent Run 创建观察包")
        _ensure_frozen_packet_matches(
            row=row,
            expected_artifact_ids=[item.artifact_id for item in artifacts],
            expected_payload=payload,
            expected_quality_gate_status=quality_gate_status,
            expected_quality_gate_reason=_packet_quality_reason(quality_gate_status),
        )
        return str(row["observation_packet_id"])


def _ensure_frozen_packet_matches(
    *,
    row: Any,
    expected_artifact_ids: list[str],
    expected_payload: dict[str, Any],
    expected_quality_gate_status: str,
    expected_quality_gate_reason: str | None,
) -> None:
    """同一 Run/采集/规则重判必须命中完全相同的冻结输入。"""
    drifted = [
        field
        for field, stored, expected in (
            ("证据附件", json.loads(row["evidence_artifact_ids"]), expected_artifact_ids),
            ("观察包内容", json.loads(row["packet_payload"]), expected_payload),
            ("证据门禁结论", row["quality_gate_status"], expected_quality_gate_status),
            ("证据门禁原因", row["quality_gate_reason"], expected_quality_gate_reason),
        )
        if stored != expected
    ]
    if drifted:
        raise RuleJudgeValidationError(
            f"观察包 {row['observation_packet_id']} 已冻结，"
            f"本次读到的{'、'.join(drifted)}与冻结内容不一致，已停止判定"
        )


async def _load_capture_presentations(
    *,
    pool: Any,
    task_id: str,
    capture_ids: tuple[str, ...],
) -> dict[str, PresentationAssessment | None]:
    """读回各证据角色采集持久化的展示层评估；读不到或 App 行（读侧走视觉观察）为 None。

    只读公共采集表 qc_capture（运行时 SQL 合同要求判定侧只引用公共采集表）：
    持久化服务把展示层评估与识别状态一并写在 capture.metadata 里。
    """
    if not capture_ids:
        return {}
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT capture.id::text AS capture_id,
                   capture.surface_kind,
                   capture.metadata
            FROM qc_capture AS capture
            WHERE capture.task_id = $1::uuid
              AND capture.id = ANY($2::uuid[])
            """,
            task_id,
            list(capture_ids),
        )
    presentations: dict[str, PresentationAssessment | None] = {}
    for row in rows:
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        if not {"capture_id", "metadata"} <= keys:
            continue
        metadata = row["metadata"]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = None
        if not isinstance(metadata, Mapping):
            metadata = None
        page_state = metadata.get("page_state") if metadata is not None else None
        presentations[str(row["capture_id"])] = presentation_from_capture_row(
            surface_kind=row["surface_kind"] if "surface_kind" in keys else None,
            metadata=metadata,
            page_state=str(page_state) if isinstance(page_state, str) else None,
        )
    return presentations


async def _validate_capture_run_ownership(
    *,
    pool: Any,
    task_id: str,
    capture_ids: tuple[str, ...],
    source_agent_run_id: str,
) -> None:
    """校验所有证据角色均来自 Handoff 声明的生产 Run。"""
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT id::text AS capture_id, agent_run_id::text AS agent_run_id
            FROM qc_capture
            WHERE task_id = $1::uuid
              AND id = ANY($2::uuid[])
              AND capture_status = 'captured'
            """,
            task_id,
            list(capture_ids),
        )
    run_by_capture = {
        str(row["capture_id"]): (
            str(row["agent_run_id"]) if row["agent_run_id"] is not None else None
        )
        for row in rows
    }
    mismatched = [
        capture_id
        for capture_id in capture_ids
        if run_by_capture.get(capture_id) != source_agent_run_id
    ]
    if mismatched:
        raise RuleJudgeValidationError(
            "证据采集不属于 Handoff 声明的生产 Run：" + ", ".join(mismatched)
        )


def _packet_quality_reason(quality_gate_status: str) -> str:
    """E02 条款 1.4：判定侧不再自行下门禁结论，只说明结论来源。"""
    if quality_gate_status == "pass":
        return "证据归属、类型、路径、大小和摘要校验通过"
    return f"投影自证据清单裁决：{quality_gate_status}"


def _normalize_capture_roles(
    *,
    page_capture_id: str,
    evidence_capture_roles: dict[str, str] | None,
) -> dict[str, str]:
    roles = dict(evidence_capture_roles or {"primary": page_capture_id})
    if roles.get("primary") != page_capture_id:
        raise RuleJudgeValidationError("证据采集角色 primary 必须指向当前页面采集")
    if not roles or any(
        not isinstance(role, str)
        or _CAPTURE_ROLE_PATTERN.fullmatch(role) is None
        or not isinstance(capture_id, str)
        or not capture_id.strip()
        for role, capture_id in roles.items()
    ):
        raise RuleJudgeValidationError("证据采集角色必须使用有效名称和页面采集 ID")
    if len(set(roles.values())) != len(roles):
        raise RuleJudgeValidationError("不同证据采集角色不得指向同一页面采集")
    return roles


def _normalize_artifact_ids(
    evidence_artifact_ids: tuple[str, ...],
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if (not allow_empty and not evidence_artifact_ids) or any(
        not isinstance(item, str) or not item.strip() for item in evidence_artifact_ids
    ):
        raise RuleJudgeValidationError("规则判定必须提供有效证据附件 ID")
    if len(set(evidence_artifact_ids)) != len(evidence_artifact_ids):
        raise RuleJudgeValidationError("规则判定证据附件 ID 不得重复")
    return evidence_artifact_ids


def _validate_payload(
    payload: bytes,
    *,
    artifact_type: str,
    content_type: str,
    checksum: str,
    size_bytes: int | None,
) -> None:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if artifact_type == "visible_text" and media_type != "text/plain":
        raise RuleJudgeValidationError("visible_text 证据 content_type 必须为 text/plain")
    if (
        artifact_type in {"action_trace", "ui_snapshot", "temporal_trace"}
        and media_type != "application/json"
    ):
        raise RuleJudgeValidationError(f"{artifact_type} 证据 content_type 必须为 application/json")
    if size_bytes is not None and len(payload) != size_bytes:
        raise RuleJudgeValidationError("证据文件大小与元数据不一致")
    if checksum:
        algorithm, separator, expected = checksum.partition(":")
        if separator != ":" or algorithm.lower() != "sha256":
            raise RuleJudgeValidationError("证据 checksum 格式无效")
        actual = hashlib.sha256(payload).hexdigest()
        if actual.lower() != expected.lower():
            raise RuleJudgeValidationError("证据 checksum 校验失败")
