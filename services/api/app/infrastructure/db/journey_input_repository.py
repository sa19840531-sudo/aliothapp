"""冻结规则到检查点输入的精确选择与复核，不按截图数量选最长路径。"""

import json

from services.api.app.application.evidence_presentation import presentation_from_capture_row
from services.api.app.application.h5_capture_readiness import h5_capture_is_inspection_ready
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.evidence.presentation import (
    EvidenceLayerTarget,
    evaluate_presentation_for_rule,
)
from services.api.app.domain.exploration import ExplorationWork
from services.api.app.domain.journey.checkpoint_evidence import bind_checkpoint_evidence
from services.api.app.domain.journey.dsl import parse_journey_execution_plan
from services.api.app.domain.journey.rule_selection import (
    rule_selection_payload,
    select_rule_checkpoints,
)
from services.api.app.domain.journey.validation import stable_hash
from services.api.app.infrastructure.db.checkpoint_recovery_binding import load_checkpoint_lineage
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    read_frozen_task_rule_inventory_on_connection,
)


def _object(value):
    return json.loads(value) if isinstance(value, str) else dict(value)


async def load_journey_selection(
    connection,
    *,
    task_id,
    agent_run_id,
    epoch,
    rule_hash,
    allow_closed_epoch=False,
    legacy_full_plan=False,
):
    if allow_closed_epoch:
        from services.api.app.infrastructure.db.journey_epoch import closed_journey_epoch_is_bound

        if not await closed_journey_epoch_is_bound(
            connection,
            task_id=task_id,
            agent_run_id=agent_run_id,
            epoch=epoch,
        ):
            return None
    rows = await connection.fetch(
        "SELECT plan_payload FROM qc_journey_execution_plan WHERE task_id=$1::uuid "
        "AND agent_run_id=$2::uuid AND schema_version='journey-execution-plan.v2'",
        task_id,
        agent_run_id,
    )
    plans = [parse_journey_execution_plan(_object(row["plan_payload"])) for row in rows]
    plans = [plan for plan in plans if rule_hash in plan.rule_contract_hashes]
    if len(plans) != 1:
        return None
    plan = plans[0]
    rule_contract = None
    if not legacy_full_plan:
        inventory = await read_frozen_task_rule_inventory_on_connection(connection, task_id=task_id)
        if inventory is not None:
            rules = [item for item in inventory["rules"] if item["contract_hash"] == rule_hash]
            if len(rules) != 1 or rules[0]["applicability"] != "applicable":
                return None
            capability = rules[0].get("judge_config", {}).get("capability_config", {})
            rule_contract = capability.get("journey_rule_contract")
    if rule_contract is not None:
        existing = await connection.fetch(
            "SELECT selection_hash,contract_payload FROM qc_journey_input_contract "
            "WHERE task_id=$1::uuid AND agent_run_id=$2::uuid AND finalization_epoch=$3 "
            "AND rule_contract_hash=$4 AND execution_plan_id=$5",
            task_id, agent_run_id, epoch, rule_hash, plan.execution_plan_id,
        )
        if existing:
            if len(existing) != 1:
                return None
            frozen = _object(existing[0]["contract_payload"])
            if (
                stable_hash(frozen) != existing[0]["selection_hash"]
                or frozen["plan"] != plan.to_dict()
            ):
                return None
            # 发布重试必须复用已经封存的旧协议，不能升级选择后补造第二份输入。
            if frozen["schema_version"] == "journey-input-contract.v1":
                rule_contract = None
    checkpoints = plan.checkpoints
    if rule_contract is not None:
        from services.api.app.application.h5_journey_rule_contract import parse_rule_contract

        try:
            rule_contract = parse_rule_contract(rule_contract)
            if any(
                rule_contract[key] != getattr(plan.journey_version, key)
                for key in ("definition_id", "version", "content_hash")
            ) or rule_contract["scenario_key"] != plan.scenario_key:
                return None
            checkpoints = select_rule_checkpoints(plan, rule_contract["evidence_conditions"])
        except (ValueError, KeyError, TypeError):
            # 单条冻结规则不能解释时只形成自身缺口，不中断其余规则或回退普通页面。
            return None
    attempts = await connection.fetch(
        "SELECT attempt.* FROM qc_journey_checkpoint_attempt AS attempt JOIN qc_task AS task "
        "ON task.id=attempt.task_id "
        "AND (task.finalization_epoch=attempt.finalization_epoch OR $5::boolean) "
        "WHERE attempt.execution_plan_id=$1 AND attempt.task_id=$2::uuid "
        "AND attempt.agent_run_id=$3::uuid AND attempt.finalization_epoch=$4",
        plan.execution_plan_id,
        task_id,
        agent_run_id,
        epoch,
        allow_closed_epoch,
    )
    checkpoint_ids = {item.checkpoint_id for item in checkpoints}
    if any(row["checkpoint_id"] not in {item.checkpoint_id for item in plan.checkpoints}
           for row in attempts):
        return None
    attempts = [row for row in attempts if row["checkpoint_id"] in checkpoint_ids]
    if len(attempts) != len(checkpoints) or any(row["status"] != "bound" for row in attempts):
        return None
    selected = []
    for checkpoint in checkpoints:
        matched = [row for row in attempts if row["checkpoint_id"] == checkpoint.checkpoint_id]
        if len(matched) != 1:
            return None
        attempt = matched[0]
        receipt = _object(attempt["turn_payload"])
        action_id = receipt["coordination"]["interaction"]["action_execution_id"]
        action = await connection.fetchrow(
            "SELECT * FROM qc_action_execution WHERE action_execution_id=$1",
            action_id,
        )
        if (
            action is None
            or _object(action["intent_payload"]) != receipt["decision"]
            or action["created_at"] < attempt["created_at"]
        ):
            return None
        captures, manifests = [], []
        for role in ("before_action", "after_action"):
            reference = receipt["coordination"][
                "before_capture" if role == "before_action" else "after_capture"
            ]
            capture = await connection.fetchrow(
                "SELECT id::text,task_id::text,agent_run_id::text,surface_session_id AS session_id,"
                "parent_capture_id::text,action_execution_id,capture_role AS role,"
                "session_command_sequence AS command_sequence,capture_status AS status,captured_at,"
                "(metadata->>'finalization_epoch')::int AS epoch,metadata "
                "FROM qc_capture WHERE id=$1::uuid",
                reference["capture_id"],
            )
            row = await connection.fetchrow(
                "SELECT manifest_payload FROM qc_evidence_manifest WHERE capture_id=$1::uuid "
                "ORDER BY manifest_version DESC LIMIT 1",
                reference["capture_id"],
            )
            artifacts = await connection.fetch(
                "SELECT id::text,artifact_type,checksum,storage_path,metadata "
                "FROM qc_evidence_artifact "
                "WHERE task_id=$1::uuid AND page_capture_id=$2::uuid",
                task_id,
                reference["capture_id"],
            )
            if capture is None or row is None or capture["captured_at"] < attempt["created_at"]:
                return None
            metadata = _object(capture["metadata"])
            presentation = presentation_from_capture_row(
                surface_kind="web",
                metadata=metadata,
                page_state=metadata.get("page_state"),
            )
            if (
                not h5_capture_is_inspection_ready(
                    metadata,
                    page_state=metadata.get("page_state"),
                    screenshot_checksums=[
                        item["checksum"]
                        for item in artifacts
                        if item["artifact_type"] == "screenshot"
                    ],
                    primary_screenshot_checksums=[
                        item["checksum"]
                        for item in artifacts
                        if item["artifact_type"] == "screenshot"
                        and _object(item["metadata"]).get("purpose") == "h5_video_primary"
                    ],
                )
                or presentation is None
                or not evaluate_presentation_for_rule(
                    presentation,
                    layer_target=EvidenceLayerTarget.BASE_PAGE,
                    accepts_diagnostic=False,
                ).accepted
            ):
                return None
            manifest = EvidenceManifestReference.from_dict(_object(row["manifest_payload"]))
            if manifest.quality_status.value != "pass":
                return None
            actual = {item["id"]: item for item in artifacts}
            if set(actual) != set(reference["artifact_ids"]):
                return None
            for item in manifest.artifacts:
                stored = actual.get(item.artifact_id)
                if stored is None or any(
                    stored[key] != getattr(item, key)
                    for key in ("artifact_type", "checksum", "storage_path")
                ):
                    return None
            capture = dict(capture)
            capture["artifact_ids"] = list(actual)
            captures.append(capture)
            manifests.append(manifest.to_dict())
        scope = dict(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=attempt["session_id"],
            epoch=epoch,
            intent_id=attempt["intent_id"],
            source_capture_id=str(attempt["source_capture_id"]),
        )
        ledger = dict(action)
        for key in (
            "task_id",
            "requested_source_capture_id",
            "source_capture_id",
            "result_capture_id",
        ):
            ledger[key] = str(ledger[key]) if ledger[key] is not None else None
        try:
            bindings = bind_checkpoint_evidence(
                scope=scope, action=ledger, captures=captures, manifests=manifests
            )
        except ValueError:
            return None
        selected.append(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "attempt_id": attempt["attempt_id"],
                "bindings": list(bindings),
                "manifests": manifests,
            }
        )
        from services.api.app.application.h5_checkpoint_observation import (
            checkpoint_needs_payment_receipt,
        )

        if checkpoint_needs_payment_receipt(checkpoint):
            selected[-1]["action_receipt"] = _object(action["result_payload"])
        recovery = {}
        for dependency in checkpoint.depends_on:
            previous = next(
                (item for item in selected if item["checkpoint_id"] == dependency), None
            )
            if previous is None:
                return None
            lineage = await load_checkpoint_lineage(
                connection,
                task_id=task_id,
                agent_run_id=agent_run_id,
                epoch=epoch,
                source_capture_id=str(attempt["source_capture_id"]),
                origin_capture_id=previous["bindings"][-1]["capture_id"],
                recovery_policy=checkpoint.recovery_policy,
                before=attempt["created_at"],
                deadline=ExplorationWork.from_dict(_object(attempt["work_payload"])).deadline,
                allow_closed_epoch=allow_closed_epoch,
            )
            if lineage is None:
                return None
            recovery[dependency] = lineage
        if recovery:
            selected[-1]["recovery"] = recovery
    contract = {
        "schema_version": "journey-input-contract.v1",
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "epoch": epoch,
        "rule_contract_hash": rule_hash,
        "plan": plan.to_dict(),
        "checkpoints": selected,
    }
    if rule_contract is not None:
        contract.update(
            schema_version="journey-input-contract.v2",
            journey_rule_contract=rule_contract,
            rule_selection=rule_selection_payload(plan, rule_contract["evidence_conditions"]),
        )
    return {"selection_hash": stable_hash(contract), "contract": contract}


async def persist_journey_selection(connection, *, input_set, selection):
    contract = selection["contract"]
    if (
        input_set.task_id != contract["task_id"]
        or input_set.source_agent_run_id != contract["agent_run_id"]
        or input_set.finalization_epoch != contract["epoch"]
        or input_set.rule_contract_hash != contract["rule_contract_hash"]
    ):
        raise ValueError("流程 InputSet 与合同串属")
    current = await load_journey_selection(
        connection,
        task_id=input_set.task_id,
        agent_run_id=input_set.source_agent_run_id,
        epoch=input_set.finalization_epoch,
        rule_hash=input_set.rule_contract_hash,
        legacy_full_plan=contract["schema_version"] == "journey-input-contract.v1",
    )
    if current != selection:
        raise ValueError("流程封存前证据版本发生变化")
    row = await connection.fetchval(
        "INSERT INTO qc_journey_input_contract(input_set_id,task_id,agent_run_id,"
        "finalization_epoch,"
        "execution_plan_id,rule_contract_hash,selection_hash,contract_payload) "
        "VALUES($1,$2::uuid,$3::uuid,$4,$5,$6,$7,$8::jsonb) ON CONFLICT DO NOTHING "
        "RETURNING selection_hash",
        input_set.input_set_id,
        input_set.task_id,
        input_set.source_agent_run_id,
        input_set.finalization_epoch,
        contract["plan"]["execution_plan_id"],
        input_set.rule_contract_hash,
        selection["selection_hash"],
        json.dumps(contract, ensure_ascii=False),
    )
    if row is None:
        row = await connection.fetchval(
            "SELECT selection_hash FROM qc_journey_input_contract WHERE input_set_id=$1",
            input_set.input_set_id,
        )
    if row != selection["selection_hash"]:
        raise ValueError("流程输入合同不得覆盖")


async def load_journey_judge_input(connection, *, input_set, allow_closed_epoch=False):
    row = await connection.fetchrow(
        "SELECT selection_hash,contract_payload FROM qc_journey_input_contract "
        "WHERE input_set_id=$1 AND task_id=$2::uuid AND agent_run_id=$3::uuid "
        "AND finalization_epoch=$4 AND rule_contract_hash=$5",
        input_set.input_set_id,
        input_set.task_id,
        input_set.source_agent_run_id,
        input_set.finalization_epoch,
        input_set.rule_contract_hash,
    )
    if row is None:
        return None
    contract = _object(row["contract_payload"])
    if stable_hash(contract) != row["selection_hash"]:
        raise ValueError("流程输入合同摘要错误")
    latest = await load_journey_selection(
        connection,
        task_id=input_set.task_id,
        agent_run_id=input_set.source_agent_run_id,
        epoch=input_set.finalization_epoch,
        rule_hash=input_set.rule_contract_hash,
        allow_closed_epoch=allow_closed_epoch,
        legacy_full_plan=contract["schema_version"] == "journey-input-contract.v1",
    )
    expected = tuple(
        binding["capture_id"]
        for checkpoint in contract["checkpoints"]
        for binding in checkpoint["bindings"]
    )
    if (
        latest is None
        or latest["contract"] != contract
        or expected != input_set.eligible_capture_ids
    ):
        return None
    return contract
