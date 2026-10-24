"""短流程公开摘要与整组材料导出；读取证据不执行探索或重算质检。"""

from services.api.app.application.evidence_archive import (
    EvidenceArchiveError,
    build_evidence_archive,
)
from services.api.app.application.h5_checkpoint_observation import supports_checkpoint
from services.api.app.domain.journey.definitions import JOURNEY_CHECKPOINT_SCHEMA_VERSION


def definition_summary(definition):
    if (
        definition.schema_version != JOURNEY_CHECKPOINT_SCHEMA_VERSION
        or "web" not in definition.surface_kinds
        or any(
            item.side_effect_policy != "read_only" or not supports_checkpoint(item)
            for item in definition.checkpoints
        )
    ):
        return None
    payment = any(
        {"payment_display_visible", "payment_plan_selected"}.intersection(
            set(item.entry_condition) | set(item.expected) | set(item.exit_condition)
        )
        for item in definition.checkpoints
    )
    return {
        "definition_id": definition.definition_id,
        "version": definition.version,
        "content_hash": definition.content_hash,
        "name": definition.name,
        "workflow_skill": "h5-payment-workflow" if payment else "h5-player-workflow",
        "evidence_conditions": [
            {
                "checkpoint_id": item.checkpoint_id,
                "requirements": [
                    requirement.to_dict() for requirement in item.evidence_requirements
                ],
            }
            for item in definition.checkpoints
        ],
        "checkpoints": [
            {"checkpoint_id": item.checkpoint_id, "depends_on": list(item.depends_on)}
            for item in definition.checkpoints
        ],
    }


def shortflow_export_plan(snapshot, plan_id):
    plans = [plan for plan in snapshot["plans"] if plan["execution_plan_id"] == plan_id]
    if len(plans) != 1:
        raise EvidenceArchiveError("EXPORT_PLAN_NOT_FOUND", "流程不存在或不属于当前任务")
    return plans[0]


async def prepare_shortflow_archive(
    snapshot, plan_id, reader, *, mode="original", request_id=None, redactions=None,
):
    plan = shortflow_export_plan(snapshot, plan_id)
    entries = []
    for checkpoint_index, checkpoint in enumerate(plan["checkpoints"]):
        for attempt_index, attempt in enumerate(checkpoint["attempts"]):
            for binding in attempt["bindings"]:
                if binding["role"] not in {"before_action", "after_action"}:
                    raise EvidenceArchiveError(
                        "EXPORT_ROLE_INVALID", "未注册的材料角色不能进入导出"
                    )
                if binding["integrity"] != "current":
                    raise EvidenceArchiveError(
                        "EXPORT_BINDING_CHANGED", "证据身份已变化，请核对台账"
                    )
                for artifact_index, artifact in enumerate(binding["artifacts"]):
                    # 文件名不使用站点标题、远端路径或用户输入，角色相同的重复图片仍完整保留。
                    name = (
                        f"{checkpoint_index + 1:03}/{attempt_index + 1:03}/"
                        f"{binding['role']}/{artifact_index + 1:03}"
                    )
                    context = {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "attempt_id": attempt["attempt_id"],
                        "session_id": attempt.get("session_id"),
                        **{
                            key: binding.get(key)
                            for key in (
                                "role",
                                "capture_id",
                                "manifest_id",
                                "manifest_version",
                                "action_execution_id",
                            )
                        },
                    }
                    entries.append((name, artifact, context))
    return await build_evidence_archive(
        entries=entries,
        reader=reader,
        mode=mode,
        redactions=redactions,
        receipt={
            "request_id": request_id,
            "task_id": snapshot["task_id"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "execution_plan_id": plan_id,
            "agent_run_id": plan.get("agent_run_id"),
            # 遮盖副本只带身份白名单；规则理由、站点原文等不能经收据绕过遮盖。
            "plan": plan
            if mode == "original"
            else {
                key: plan.get(key)
                for key in (
                    "execution_plan_id",
                    "agent_run_id",
                    "definition_id",
                    "version",
                    "content_hash",
                )
            },
        },
    )


async def build_shortflow_archive(snapshot, plan_id, reader, *, mode="original"):
    """保留旧调用合同；API 使用带追溯摘要的 prepare_shortflow_archive。"""
    return (await prepare_shortflow_archive(snapshot, plan_id, reader, mode=mode)).content
