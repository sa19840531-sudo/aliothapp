"""补采证据接上后按规则重新封存 EvidenceInputSet（09-11 审查 P1-10）。

补采成功只多了一份新 Capture / Manifest / Supplement；重判如果只换 Attempt 输入哈希，审计链在
「Manifest → Attempt」之间就断了。这里给每条已接上但还没封存的补采封一个不可变输入集合：成员是本次
补采的 Manifest，封存快照指回原输入、补采请求与状态等价证明，集合记在补采记录上。领取之后的新
Attempt 把集合身份并进输入哈希，写回时规则执行也指回它。

封存有两个入口：补采挂接后立即封，以及工作项被领取、准备重判的同一事务里补封。两处都先锁住补采
记录再看有没有封过，谁先封就沿用谁的集合，一次补采只对应一个输入集合。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.domain.evidence import (
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceInputSet,
    EvidenceManifestReference,
    seal_evidence_input_set,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.evidence_input_set_repository import (
    PostgresEvidenceInputSetRepository,
)

EVIDENCE_SUPPLEMENT_RESEAL_PROTOCOL_VERSION = "evidence-supplement-reseal.v1"

# 补采只会发生在会话还活着的阶段；review 与 session_draining 并存时也要能重判
_PERMITTED_TASK_STATUSES = ("running", "review")
_PERMITTED_FINALIZATION_STATES = ("open", "exploration_draining", "session_draining")


class EvidenceSupplementResealError(RuntimeError):
    """已接上的补采无法按规则上下文封存输入集合。"""


async def reseal_supplement_input_sets_on_connection(
    connection: Any,
    *,
    work_item_id: str,
) -> int:
    """复用调用方事务，为工作项里已接上、尚未封存的补采逐条封存并挂回补采记录。"""

    await connection.fetch(
        _LOCK_SATISFIED_SUPPLEMENTS_SQL.format(scope="request.work_item_id = $1::uuid"),
        work_item_id,
    )
    # 领取时只补封接下来要重判的规则
    rows = await connection.fetch(
        _UNSEALED_SUPPLEMENT_CONTEXT_SQL.format(
            scope="request.work_item_id = $1::uuid AND member.status IN ('pending', 'judged')"
        ),
        work_item_id,
    )
    for row in rows:
        await _seal_and_link(connection, row)
    return len(rows)


async def seal_supplement_input_set_on_connection(
    connection: Any,
    *,
    request_id: str,
) -> tuple[str, str, bool] | None:
    """补采挂接后立即封存，返回补采记录上的集合（编号、哈希、是否本次新封）。

    领取时已经封过就直接沿用那个集合。挂接时规则可能已经结算、不会再重判，照样封，好让补采请求上的
    重封标记与补采记录指向同一个集合。Web 序列规则不在这里封，返回 None。
    """

    await connection.fetch(
        _LOCK_SATISFIED_SUPPLEMENTS_SQL.format(scope="request.request_id = $1"),
        request_id,
    )
    rows = await connection.fetch(
        _UNSEALED_SUPPLEMENT_CONTEXT_SQL.format(scope="request.request_id = $1"),
        request_id,
    )
    for row in rows:
        await _seal_and_link(connection, row)
    linked = await connection.fetchrow(
        """
        SELECT input_set.input_set_id, input_set.input_set_hash
        FROM qc_evidence_request AS request
        INNER JOIN qc_evidence_supplement AS supplement
            ON supplement.request_id = request.request_id
           AND supplement.manifest_id = request.resolution_manifest_id
        INNER JOIN qc_evidence_input_set AS input_set
            ON input_set.task_id = request.task_id
           AND input_set.input_set_id = supplement.evidence_input_set_id
        WHERE request.request_id = $1
          AND request.status = 'satisfied'
        """,
        request_id,
    )
    if linked is None:
        return None
    return str(linked["input_set_id"]), str(linked["input_set_hash"]), bool(rows)


# 两个入口并发时靠这把行锁排队：后到的一方等前一方提交后，再读就能看到已挂上的集合
_LOCK_SATISFIED_SUPPLEMENTS_SQL = """
    SELECT supplement.supplement_id
    FROM qc_evidence_request AS request
    INNER JOIN qc_evidence_supplement AS supplement
        ON supplement.request_id = request.request_id
       AND supplement.manifest_id = request.resolution_manifest_id
    WHERE {scope}
      AND request.status = 'satisfied'
    ORDER BY supplement.supplement_id
    FOR UPDATE OF supplement
"""

_UNSEALED_SUPPLEMENT_CONTEXT_SQL = """
        SELECT
            supplement.supplement_id,
            supplement.manifest_id AS supplement_manifest_id,
            supplement.probe_work_id,
            supplement.restoration_attempt_id,
            supplement.equivalence_assessment_id,
            supplement.source_grade,
            supplement.origin,
            request.request_id,
            request.task_id::text AS task_id,
            request.rule_id::text AS rule_id,
            request.rule_code,
            request.requirement_hash,
            request.required_equivalence,
            request.replay_policy,
            request.minimum_source_grade,
            request.target_capture_id::text AS target_capture_id,
            work_item.id::text AS work_item_id,
            work_item.input_hash AS work_item_input_hash,
            member.contract_hash,
            task.finalization_epoch,
            ruleset.snapshot_hash AS ruleset_snapshot_hash,
            manifest.manifest_payload,
            capture.id::text AS capture_id,
            capture.agent_run_id::text AS source_agent_run_id,
            capture.capture_role,
            COALESCE(result_page.page_seq, target_page.page_seq, 0) AS page_sequence,
            original.manifest_id AS original_manifest_id,
            original.manifest_version AS original_manifest_version,
            original.manifest_hash AS original_manifest_hash
        FROM qc_evidence_request AS request
        INNER JOIN qc_evidence_supplement AS supplement
            ON supplement.request_id = request.request_id
           AND supplement.manifest_id = request.resolution_manifest_id
        INNER JOIN qc_inspection_work_item AS work_item
            ON work_item.id = request.work_item_id
        INNER JOIN qc_inspection_work_item_rule AS member
            ON member.work_item_id = work_item.id
           AND member.rule_id = request.rule_id
           -- Web 序列不在这里另封；H5 原页面成员保留基线，再另封补采。
           AND (member.evidence_input_set_id IS NULL OR EXISTS (
               SELECT 1 FROM qc_task bound_task JOIN qc_evidence_input_set original_input
                   ON original_input.task_id=bound_task.id
               WHERE bound_task.id=request.task_id AND bound_task.target_platform='h5'
                   AND original_input.input_set_id=member.evidence_input_set_id
                   AND original_input.scope='page'
           ))
        INNER JOIN qc_task AS task ON task.id = request.task_id
        INNER JOIN qc_ruleset AS ruleset ON ruleset.id = task.ruleset_id
        INNER JOIN qc_evidence_manifest AS manifest
            ON manifest.manifest_id = supplement.manifest_id
           AND manifest.task_id = request.task_id
           AND manifest.quality_status = 'pass'
        INNER JOIN qc_capture AS capture
            ON capture.id = manifest.capture_id
           AND capture.task_id = request.task_id
           AND capture.capture_status = 'captured'
        LEFT JOIN qc_page_capture AS result_page ON result_page.id = capture.id
        LEFT JOIN qc_page_capture AS target_page ON target_page.id = request.target_capture_id
        LEFT JOIN LATERAL (
            SELECT prior.manifest_id, prior.manifest_version, prior.manifest_hash
            FROM qc_evidence_manifest AS prior
            WHERE prior.task_id = request.task_id
              AND prior.capture_id = request.target_capture_id
              AND prior.quality_status = 'pass'
            ORDER BY prior.manifest_version DESC
            LIMIT 1
        ) AS original ON TRUE
        WHERE {scope}
          AND request.status = 'satisfied'
          AND supplement.evidence_input_set_id IS NULL
        ORDER BY request.rule_id, supplement.supplement_id
"""


async def _seal_and_link(connection: Any, row: Any) -> None:
    persisted = await PostgresEvidenceInputSetRepository(
        permitted_task_statuses=_PERMITTED_TASK_STATUSES,
        permitted_finalization_states=_PERMITTED_FINALIZATION_STATES,
    ).add_on_connection(
        connection,
        input_set=build_supplement_input_set(_mapping(row)),
    )
    linked = await connection.fetchval(
        """
        UPDATE qc_evidence_supplement
        SET evidence_input_set_id = $2
        WHERE supplement_id = $1
          AND evidence_input_set_id IS NULL
        RETURNING supplement_id
        """,
        str(row["supplement_id"]),
        persisted.input_set.input_set_id,
    )
    if linked is None:
        raise EvidenceSupplementResealError("补采记录已挂到其他输入集合，拒绝重复封存")


def build_supplement_input_set(row: Mapping[str, Any]) -> EvidenceInputSet:
    """把一条补采的上下文封成只含补采 Manifest 的 page 输入集合，血缘进封存快照哈希。"""

    supplement_id = str(row["supplement_id"])
    manifest = EvidenceManifestReference.from_dict(_json_mapping(row["manifest_payload"]))
    lineage = {
        "protocol_version": EVIDENCE_SUPPLEMENT_RESEAL_PROTOCOL_VERSION,
        "supplement": {
            "supplement_id": supplement_id,
            "manifest_id": str(row["supplement_manifest_id"]),
            "probe_work_id": str(row["probe_work_id"]),
            "origin": str(row["origin"]),
        },
        "request": {
            "request_id": str(row["request_id"]),
            "requirement_hash": str(row["requirement_hash"]),
            "required_equivalence": str(row["required_equivalence"]),
            "replay_policy": str(row["replay_policy"]),
            "minimum_source_grade": str(row["minimum_source_grade"]),
        },
        "restoration": {
            "attempt_id": str(row["restoration_attempt_id"]),
            "equivalence_assessment_id": str(row["equivalence_assessment_id"]),
            "source_grade": str(row["source_grade"]),
        },
        "original_input": {
            "work_item_id": str(row["work_item_id"]),
            "work_item_input_hash": str(row["work_item_input_hash"]),
            "capture_id": str(row["target_capture_id"]),
            "manifest_id": _optional_text(row.get("original_manifest_id")),
            "manifest_version": row.get("original_manifest_version"),
            "manifest_hash": _optional_text(row.get("original_manifest_hash")),
        },
    }
    capture_id = str(row["capture_id"])
    return seal_evidence_input_set(
        task_id=str(row["task_id"]),
        source_agent_run_id=str(row["source_agent_run_id"]),
        rule_id=str(row["rule_id"]),
        rule_code=str(row["rule_code"]),
        rule_contract_hash=str(row["contract_hash"]),
        ruleset_snapshot_hash=str(row["ruleset_snapshot_hash"]),
        scope=EvidenceInputScope.PAGE,
        scope_key="supplement." + hashlib.sha256(supplement_id.encode("utf-8")).hexdigest()[:32],
        finalization_epoch=int(row["finalization_epoch"]),
        eligibility_snapshot_id=f"evidence-supplement:{supplement_id}",
        eligibility_snapshot_hash=stable_protocol_hash(lineage),
        selection_policy_hash=stable_protocol_hash(
            {
                "protocol_version": EVIDENCE_SUPPLEMENT_RESEAL_PROTOCOL_VERSION,
                # 与重判时的证据合并口径一致：补采替换 primary，基线其余角色保留
                "replaces_role": "primary",
                "retains_baseline_roles": True,
            }
        ),
        eligible_capture_ids=(capture_id,),
        candidates=(
            EvidenceInputCandidate(
                manifest=manifest,
                capture_role=str(row["capture_role"]),
                page_sequence=int(row["page_sequence"]),
            ),
        ),
        sealed_at=datetime.now(UTC),
    )


def _mapping(row: Any) -> Mapping[str, Any]:
    return row if isinstance(row, Mapping) else dict(row)


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise EvidenceSupplementResealError("补采 Manifest 载荷必须是对象")
    return value


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
