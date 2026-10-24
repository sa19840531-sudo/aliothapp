"""在原 Publication 租约内为真实 Web 序列封存逐规则输入并发布原 Work。"""

import json
from datetime import UTC, datetime

from services.api.app.application.inspection_work_items import (
    InspectionWorkItemRule,
    PublishInspectionWorkItemCommand,
    build_inspection_rule_contract_hash,
    build_inspection_work_item_input_hash,
    validate_inspection_work_item_publish_command,
)
from services.api.app.application.rule_judge import rule_execution_route_payload
from services.api.app.application.web_rule_evidence_contract import validate_web_sequence_execution
from services.api.app.application.web_sequence_rule_selection import select_web_sequence_rule
from services.api.app.domain.evidence import (
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceManifestReference,
    seal_evidence_input_set,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    publish_inspection_work_item_on_connection,
)
from services.api.app.infrastructure.db.rule_repository import _normalize_rule_row
from services.api.app.infrastructure.db.web_sequence_binding_reader import _mapping
from services.api.app.infrastructure.db.web_sequence_input_set_repository import (
    seal_web_input_on_connection,
)
from services.api.app.infrastructure.db.web_sequence_publication_claim import (
    load_web_sequence_publication,
)


async def process_web_sequence_publication(repository, *, lease):
    """显式绑定才进入；真实租约、来源与任务围栏复核失败时整笔回滚。"""
    if lease.web_evidence_sequence_id is None:
        raise RuntimeError("WEB_SEQUENCE_BINDING_REQUIRED")
    pool = await repository._get_pool()
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{lease.task_id}"
        )
        row = await load_web_sequence_publication(
            connection, publication_id=lease.publication_id, task_id=lease.task_id
        )
        if (
            row is None
            or not row["authorized"]
            or row["status"] != "leased"
            or str(row["lease_token"]) != lease.lease_token
            or row["row_version"] != lease.row_version
            or row["lease_expires_at"] <= datetime.now(UTC)
            or str(row["web_evidence_sequence_id"]) != lease.web_evidence_sequence_id
            or str(row["source_agent_run_id"]) != lease.source_agent_run_id
            or str(row["page_capture_id"]) != lease.page_capture_id
            or row["finalization_epoch"] != lease.finalization_epoch
        ):
            raise RuntimeError("WEB_SEQUENCE_PUBLICATION_LEASE_REJECTED")
        sequence = _mapping(row["sequence_payload"])
        if stable_protocol_hash(sequence) != row["sequence_hash"]:
            raise RuntimeError("WEB_SEQUENCE_PAYLOAD_DRIFT")
        candidates = await _load_candidates(connection, lease, sequence)
        anchor = await connection.fetchrow(
            "SELECT task.channel,page.url FROM qc_task task JOIN qc_capture capture ON capture.task_id=task.id "
            "JOIN qc_page_capture page ON page.id=capture.id AND page.task_id=task.id "
            "WHERE task.id=$1::uuid AND capture.id=$2::uuid AND capture.agent_run_id=$3::uuid",
            lease.task_id,
            lease.page_capture_id,
            lease.source_agent_run_id,
        )
        if anchor is None:
            raise RuntimeError("WEB_SEQUENCE_ANCHOR_MISSING")
        rule_rows = await connection.fetch(
            "SELECT * FROM qc_rule WHERE ruleset_id=ANY(qc_ruleset_scope($1::uuid)) AND status='enabled' ORDER BY sort_order,rule_code FOR UPDATE",
            str(row["ruleset_id"]),
        )
        assessment = await connection.fetchrow(
            "SELECT assessment.page_type,assessment.detection_status,(assessment.page_type=page.page_type) AS type_matches FROM qc_h5_page_type_assessment assessment "
            "JOIN qc_page_capture page ON page.id=assessment.page_capture_id AND page.task_id=assessment.task_id "
            "WHERE assessment.task_id=$1::uuid AND assessment.agent_run_id=$2::uuid "
            "AND assessment.page_capture_id=$3::uuid ORDER BY assessment.created_at DESC,assessment.assessment_record_id DESC LIMIT 1",
            lease.task_id,
            lease.source_agent_run_id,
            lease.page_capture_id,
        )
        groups = {}
        frozen_rows = await connection.fetch(
            "SELECT rule_id::text,contract_hash,execution_plan FROM qc_h5_inspection_publication_rule WHERE publication_id=$1::uuid",
            lease.publication_id,
        )
        frozen = {item["rule_id"]: dict(item) for item in frozen_rows}
        prepared = []
        for raw in rule_rows:
            rule = _normalize_rule_row(dict(raw))
            contract = validate_web_sequence_execution(
                rule["input_contract"],
                executor_type=rule["executor_type"],
                judge_config=rule["judge_config"],
            )
            if contract is None:
                continue
            rule["rule_id"] = str(rule["id"])
            rule["execution_route"] = rule_execution_route_payload(
                executor_type=rule["executor_type"],
                judge_config=rule["judge_config"],
                input_contract=rule["input_contract"],
            )
            prepared.append((rule, contract, build_inspection_rule_contract_hash(rule)))
        current_hashes = {rule["rule_id"]: hash_value for rule, _, hash_value in prepared}
        if frozen and current_hashes != {
            key: item["contract_hash"] for key, item in frozen.items()
        }:
            await connection.execute(
                "UPDATE qc_h5_inspection_publication_rule SET execution_plan=execution_plan||jsonb_build_object('selection',jsonb_build_object('status','evidence_insufficient','reason_code','WEB_SEQUENCE_RULE_SNAPSHOT_DRIFT')) WHERE publication_id=$1::uuid",
                lease.publication_id,
            )
            await connection.execute(
                "UPDATE qc_h5_inspection_publication SET status='needs_review',failure_code='WEB_SEQUENCE_RULE_SNAPSHOT_DRIFT',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,row_version=row_version+1,finished_at=now(),updated_at=now() WHERE id=$1::uuid AND lease_token=$2::uuid AND row_version=$3",
                lease.publication_id,
                lease.lease_token,
                lease.row_version,
            )
            return ()
        from services.api.app.infrastructure.db.web_sequence_classification_wait import (
            defer_classification,
            may_wait_for_classification,
        )

        may_wait = (
            await may_wait_for_classification(connection, lease, sequence)
            if assessment is None and sequence["purpose"] == "interaction_sequence"
            else False
        )
        waiting = False
        selection_count = 0
        insufficient = False
        for rule, contract, contract_hash in prepared:
            selection = select_web_sequence_rule(
                rule,
                contract,
                sequence,
                task_channel=anchor["channel"],
                page_url=anchor["url"],
                page_type=assessment["page_type"]
                if assessment
                and assessment["detection_status"] == "recognized"
                and assessment["type_matches"]
                else None,
            )
            if (
                sequence["purpose"] == "interaction_sequence"
                and selection["reason_code"] == "WEB_RULE_UNCLASSIFIED_REJECTED"
            ):
                selection = {
                    "status": "classification_pending"
                    if assessment is None and may_wait
                    else "evidence_insufficient",
                    "reason_code": "WEB_SEQUENCE_CLASSIFICATION_PENDING"
                    if assessment is None and may_wait
                    else "WEB_SEQUENCE_CLASSIFICATION_UNKNOWN"
                    if assessment is not None
                    else "WEB_SEQUENCE_CLASSIFICATION_UNRESOLVED",
                }
            waiting |= selection["status"] == "classification_pending"
            rule["rule_id"] = str(rule["id"])
            rule["execution_route"] = rule_execution_route_payload(
                executor_type=rule["executor_type"],
                judge_config=rule["judge_config"],
                input_contract=rule["input_contract"],
            )
            contract_hash = build_inspection_rule_contract_hash(rule)
            if contract.local_structure is not None and selection["status"] == "admissible":
                from services.api.app.application.web_local_structure import (
                    plan_web_local_structure_rule,
                )
                from services.api.app.infrastructure.db.web_capability_revision_repository import (
                    read_capture_revision_on_connection,
                )

                revision = await read_capture_revision_on_connection(
                    connection,
                    task_id=lease.task_id,
                    agent_run_id=lease.source_agent_run_id,
                    finalization_epoch=lease.finalization_epoch,
                    capture_id=lease.page_capture_id,
                )
                if revision != sequence.get("capability_revision"):
                    raise RuntimeError("WEB_CAPABILITY_REVISION_DRIFT")
                selection = plan_web_local_structure_rule(
                    contract.local_structure,
                    revision or {},
                    rule_version=contract.rule_version,
                    rule_contract_hash=contract_hash,
                )
            await connection.execute(
                "INSERT INTO qc_h5_inspection_publication_rule(publication_id,rule_id,rule_code,ordinal,"
                "contract_hash,strategy,category,session_dependency,execution_plan) "
                "VALUES($1::uuid,$2::uuid,$3,$4,$5,$6,$7,'snapshot',$8::jsonb) "
                "ON CONFLICT(publication_id,rule_id) DO UPDATE SET execution_plan=qc_h5_inspection_publication_rule.execution_plan||EXCLUDED.execution_plan",
                lease.publication_id,
                rule["rule_id"],
                rule["rule_code"],
                selection_count,
                contract_hash,
                rule["executor_type"],
                rule["category"],
                json.dumps(
                    {
                        "web_evidence_sequence_id": lease.web_evidence_sequence_id,
                        "selection": selection,
                    },
                    ensure_ascii=False,
                ),
            )
            selection_count += 1
            insufficient |= selection["status"] == "evidence_insufficient"
            if selection["status"] != "admissible":
                continue
            if rule["rule_id"] in frozen and _mapping(
                frozen[rule["rule_id"]]["execution_plan"]
            ).get("evidence_input_set_id"):
                continue
            inputs = seal_evidence_input_set(
                task_id=lease.task_id,
                source_agent_run_id=lease.source_agent_run_id,
                rule_id=rule["rule_id"],
                rule_code=rule["rule_code"],
                rule_contract_hash=contract_hash,
                ruleset_snapshot_hash=row["ruleset_snapshot_hash"],
                scope=EvidenceInputScope.WEB_SEQUENCE,
                scope_key="web_sequence:" + lease.web_evidence_sequence_id,
                finalization_epoch=lease.finalization_epoch,
                eligibility_snapshot_id=lease.web_evidence_sequence_id,
                eligibility_snapshot_hash=row["sequence_hash"],
                selection_policy_hash=stable_protocol_hash(contract.to_dict()),
                eligible_capture_ids=tuple(frame["capture_id"] for frame in sequence["frames"]),
                candidates=candidates,
                sealed_at=datetime.now(UTC),
            )
            await seal_web_input_on_connection(connection, input_set=inputs, lease=lease)
            members = groups.setdefault(rule["category"], [])
            members.append(
                InspectionWorkItemRule(
                    rule_id=rule["rule_id"],
                    rule_code=rule["rule_code"],
                    ordinal=len(members),
                    contract_hash=contract_hash,
                    evidence_input_set_id=inputs.input_set_id,
                )
            )
        work_ids = [
            str(item["work_item_id"])
            for item in await connection.fetch(
                "SELECT work_item_id FROM qc_h5_inspection_publication_work_item WHERE publication_id=$1::uuid",
                lease.publication_id,
            )
        ]
        for category, members in groups.items():
            binding_hash = stable_protocol_hash(
                {
                    "purpose": sequence["purpose"],
                    "sequence_id": lease.web_evidence_sequence_id,
                    "inputs": [member.evidence_input_set_id for member in members],
                }
            )
            command = PublishInspectionWorkItemCommand(
                task_id=lease.task_id,
                page_capture_id=lease.page_capture_id,
                producer_agent_run_id=lease.source_agent_run_id,
                ruleset_id=str(row["ruleset_id"]),
                ruleset_version=row["ruleset_version"],
                ruleset_snapshot_hash=row["ruleset_snapshot_hash"],
                batch_key="web_sequence:" + binding_hash,
                strategy="llm",
                category=category,
                session_dependency="snapshot",
                rules=tuple(members),
                control_mode="enforced",
            )
            validate_inspection_work_item_publish_command(command)
            work = await publish_inspection_work_item_on_connection(
                connection,
                command=command,
                input_hash=build_inspection_work_item_input_hash(command),
            )
            work_ids.append(work.work_item_id)
            await connection.execute(
                "INSERT INTO qc_h5_inspection_publication_work_item(publication_id,work_item_id,batch_key) "
                "VALUES($1::uuid,$2::uuid,$3) ON CONFLICT(publication_id,work_item_id) DO NOTHING",
                lease.publication_id,
                work.work_item_id,
                command.batch_key,
            )
        await connection.execute(
            """
            INSERT INTO qc_h5_inspection_publication_rule(publication_id,rule_id,rule_code,ordinal,contract_hash,
                strategy,category,session_dependency,execution_plan)
            SELECT link.publication_id,member.rule_id,member.rule_code,
                row_number() OVER(ORDER BY link.batch_key,member.ordinal)-1,member.contract_hash,
                work.strategy,work.category,work.session_dependency,
                jsonb_build_object('batch_key',link.batch_key,'evidence_input_set_id',member.evidence_input_set_id,
                    'web_evidence_sequence_id',$2::text)
            FROM qc_h5_inspection_publication_work_item link
            JOIN qc_inspection_work_item work ON work.id=link.work_item_id
            JOIN qc_inspection_work_item_rule member ON member.work_item_id=work.id
            WHERE link.publication_id=$1::uuid
            ON CONFLICT(publication_id,rule_id) DO UPDATE SET
                execution_plan=qc_h5_inspection_publication_rule.execution_plan || EXCLUDED.execution_plan
            """,
            lease.publication_id,
            lease.web_evidence_sequence_id,
        )
        if waiting:
            await defer_classification(
                repository, connection, lease, work_count=len(work_ids), rule_count=selection_count
            )
            return tuple(work_ids)
        await connection.execute(
            "UPDATE qc_h5_inspection_publication SET status=$4,published_work_item_count=$5,candidate_rule_count=$6,"
            "expected_batch_count=$5,failure_code=$7,"
            "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,row_version=row_version+1,finished_at=now(),updated_at=now() "
            "WHERE id=$1::uuid AND lease_token=$2::uuid AND row_version=$3",
            lease.publication_id,
            lease.lease_token,
            lease.row_version,
            "needs_review" if insufficient else "published" if work_ids else "not_applicable",
            len(work_ids),
            selection_count,
            "WEB_SEQUENCE_EVIDENCE_INSUFFICIENT" if insufficient else None,
        )
        return tuple(work_ids)


async def _load_candidates(connection, lease, sequence):
    """逐帧按冻结制品集合选择精确 PASS Manifest，不复用锚点的第一份材料。"""
    candidates = []
    for frame in sequence["frames"]:
        rows = await connection.fetch(
            "SELECT manifest.manifest_payload,capture.capture_role FROM qc_evidence_manifest manifest "
            "JOIN qc_capture capture ON capture.id=manifest.capture_id AND capture.task_id=manifest.task_id "
            "WHERE capture.task_id=$1::uuid AND capture.agent_run_id=$2::uuid AND capture.id=$3::uuid "
            "AND manifest.quality_status='pass' ORDER BY manifest.manifest_version DESC",
            lease.task_id,
            lease.source_agent_run_id,
            frame["capture_id"],
        )
        chosen = None
        for row in rows:
            manifest = EvidenceManifestReference.from_dict(_mapping(row["manifest_payload"]))
            if [artifact.artifact_id for artifact in manifest.artifacts] == frame[
                "artifact_ids"
            ] and row["capture_role"] == frame["capture_role"]:
                chosen = EvidenceInputCandidate(
                    manifest=manifest,
                    capture_role=row["capture_role"],
                    page_sequence=len(candidates) + 1,
                )
                break
        if chosen is None:
            raise RuntimeError("WEB_SEQUENCE_EXACT_MANIFEST_MISSING")
        candidates.append(chosen)
    return tuple(candidates)
