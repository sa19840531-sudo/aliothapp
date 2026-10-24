"""检查点尝试与证据绑定的 PostgreSQL 权威仓储。"""

import json
from datetime import UTC, datetime

from services.api.app.application.capture_coordination import CaptureCoordinationResult
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.application.h5_session_journey import assert_h5_journeys_active
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.exploration import ExplorationWork
from services.api.app.domain.interaction import ActionIntent, CaptureRef, InteractionResult
from services.api.app.domain.journey.checkpoint_evidence import bind_checkpoint_evidence
from services.api.app.domain.journey.dsl import parse_journey_execution_plan
from services.api.app.domain.journey.validation import stable_hash
from services.api.app.infrastructure.db.checkpoint_recovery_binding import load_checkpoint_lineage
from services.api.app.infrastructure.db.journey_definition_repository import (
    PostgresJourneyDefinitionRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


def _object(value):
    return json.loads(value) if isinstance(value, str) else dict(value)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _prior_claim(prior, *, epoch, context, work, decision):
    if prior["finalization_epoch"] != epoch:
        raise ValueError("检查点原尝试属于旧 Epoch")
    if (
        any(str(prior[key]) != value for key, value in context.items())
        or _object(prior["intent_payload"]) != decision.to_dict()
        or ExplorationWork.from_dict(_object(prior["work_payload"])) != work
    ):
        raise ValueError("检查点重领改变了原动作合同")
    return "bound" if prior["status"] == "bound" else "uncertain"


class PostgresCheckpointExecutionRepository:
    def __init__(self, *, pool=None, observer=None):
        self._pool = pool
        self._observer = observer

    async def _get_pool(self):
        return self._pool or await get_postgres_pool()

    async def list_plans(self, *, task_id, agent_run_id):
        return await PostgresJourneyDefinitionRepository(
            pool=await self._get_pool()
        ).list_execution_plans_for_run(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )

    async def get_checkpoint(self, *, context):
        plans = await self.list_plans(
            task_id=context["task_id"], agent_run_id=context["agent_run_id"]
        )
        matches = [
            item
            for plan in plans
            if plan.execution_plan_id == context["execution_plan_id"]
            for item in plan.checkpoints
            if item.checkpoint_id == context["checkpoint_id"]
        ]
        if len(matches) != 1:
            raise ValueError("检查点冻结合同不存在")
        return matches[0]

    async def restore_work(self, *, work, agent_run_id):
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT attempt.work_payload,attempt.agent_run_id::text "
                "FROM qc_journey_checkpoint_attempt AS attempt JOIN qc_task AS task "
                "ON task.id=attempt.task_id AND task.finalization_epoch=attempt.finalization_epoch "
                "WHERE attempt.work_id=$1",
                work.work_id,
            )
        if row is None:
            return None
        restored = ExplorationWork.from_dict(_object(row["work_payload"]))
        if (
            row["agent_run_id"] != agent_run_id
            or restored.task_id != work.task_id
            or restored.session_id != work.session_id
            or restored.source_capture_id != work.source_capture_id
            or restored.deadline != work.deadline
            or restored.budget != work.budget
        ):
            raise ValueError("检查点重领不能换来源、会话、预算或截止")
        return restored

    async def next_checkpoint(self, *, work, plans):
        if not plans:
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT attempt.*
                   FROM qc_journey_checkpoint_attempt AS attempt JOIN qc_task AS task
                     ON task.id=attempt.task_id
                    AND task.finalization_epoch=attempt.finalization_epoch
                    WHERE attempt.task_id=$1::uuid AND attempt.agent_run_id=$2::uuid
                      AND attempt.execution_plan_id=ANY($3::text[])""",
                work.task_id,
                plans[0].agent_run_id,
                [plan.execution_plan_id for plan in plans],
            )
        refreshed = []
        for row in rows:
            current = dict(row)
            if row["status"] == "bound":
                context = {
                    key: str(row[key])
                    for key in (
                        "attempt_id",
                        "execution_plan_id",
                        "checkpoint_id",
                        "task_id",
                        "agent_run_id",
                        "session_id",
                        "source_capture_id",
                        "work_id",
                    )
                }
                original_work = ExplorationWork.from_dict(_object(row["work_payload"]))
                turn = await self.read_turn(work=original_work, context=context)
                try:
                    observed = await self.bind(
                        work=original_work, context=context, turn=turn, audit_only=True
                    )
                except ValueError:
                    observed = False
                current["observation_status"] = "observed" if observed else "needs_review"
            refreshed.append(current)
        rows = refreshed
        by_checkpoint = {(row["execution_plan_id"], row["checkpoint_id"]): row for row in rows}
        for plan in plans:
            if any(
                sum(
                    row["execution_plan_id"] == plan.execution_plan_id
                    and row["checkpoint_id"] == item.checkpoint_id
                    for row in rows
                )
                > 1
                for item in plan.checkpoints
            ):
                continue
            observed = {
                checkpoint.checkpoint_id
                for checkpoint in plan.checkpoints
                if (row := by_checkpoint.get((plan.execution_plan_id, checkpoint.checkpoint_id)))
                and row["observation_status"] == "observed"
            }
            for checkpoint in plan.checkpoints:
                if (
                    (plan.execution_plan_id, checkpoint.checkpoint_id) in by_checkpoint
                    or not set(checkpoint.depends_on) <= observed
                    or checkpoint.side_effect_policy != "read_only"
                ):
                    continue
                async with pool.acquire() as connection:
                    if not await self._dependencies(
                        connection, work=work, plan=plan, checkpoint=checkpoint, rows=rows
                    ):
                        continue
                if self._observer is not None and await self._observer.entry_matches(
                    work=work,
                    checkpoint=checkpoint,
                ):
                    return plan, checkpoint
        return None

    async def _dependencies(self, connection, *, work, plan, checkpoint, rows):
        for dependency in checkpoint.depends_on:
            previous = [
                row
                for row in rows
                if row["execution_plan_id"] == plan.execution_plan_id
                and row["checkpoint_id"] == dependency
                and row["status"] == "bound"
            ]
            if len(previous) != 1:
                return False
            row = previous[0]
            if row["observation_status"] != "observed":
                return False
            receipt = _object(row["turn_payload"])
            lineage = await load_checkpoint_lineage(
                connection,
                task_id=work.task_id,
                agent_run_id=plan.agent_run_id,
                epoch=row["finalization_epoch"],
                source_capture_id=work.source_capture_id,
                origin_capture_id=receipt["coordination"]["after_capture"]["capture_id"],
                recovery_policy=checkpoint.recovery_policy,
                before=min(work.deadline, datetime.now(UTC)),
                deadline=work.deadline,
            )
            if lineage is None:
                return False
        return True

    async def reserve(self, *, work, context, decision):
        assert_h5_journeys_active()
        for key, value in (
            ("task_id", work.task_id),
            ("session_id", work.session_id),
            ("work_id", work.work_id),
            ("source_capture_id", work.source_capture_id),
        ):
            if context.get(key) != value:
                raise ValueError("检查点准入上下文串属")
        if (
            decision.task_id != work.task_id
            or decision.session_id != work.session_id
            or decision.source_capture_id != work.source_capture_id
            or decision.deadline != work.deadline
        ):
            raise ValueError("检查点准入动作串属或刷新截止")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            # 锁顺序沿用 Task→Session→Work；准备与动作之间不刷新任何预算。
            task = await connection.fetchrow(
                "SELECT finalization_epoch,finalization_state FROM qc_task "
                "WHERE id=$1::uuid FOR KEY SHARE",
                work.task_id,
            )
            if task is None or task["finalization_state"] in ("finalizing", "sealed"):
                raise ValueError("检查点任务已禁止新动作")
            epoch = task["finalization_epoch"]
            prior = await connection.fetchrow(
                "SELECT * FROM qc_journey_checkpoint_attempt WHERE work_id=$1 FOR UPDATE",
                work.work_id,
            )
            if prior is not None:
                return _prior_claim(
                    prior, epoch=epoch, context=context, work=work, decision=decision
                )
            plan_payload = await connection.fetchval(
                """SELECT plan_payload FROM qc_journey_execution_plan WHERE execution_plan_id=$1
                   AND task_id=$2::uuid AND agent_run_id=$3::uuid""",
                context["execution_plan_id"],
                work.task_id,
                context["agent_run_id"],
            )
            if plan_payload is None:
                raise ValueError("检查点计划与 Run 不一致")
            plan = parse_journey_execution_plan(_object(plan_payload))
            checkpoint = next(
                (
                    item
                    for item in plan.checkpoints
                    if item.checkpoint_id == context["checkpoint_id"]
                ),
                None,
            )
            if checkpoint is None or checkpoint.side_effect_policy != "read_only":
                raise ValueError("检查点没有只读执行许可")
            # 不同物理会话仍共享同一逻辑检查点；锁后再核对，禁止恢复造成重复动作。
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1),hashtext($2))",
                plan.execution_plan_id,
                f"{checkpoint.checkpoint_id}:{epoch}",
            )
            existing = await connection.fetch(
                "SELECT * FROM qc_journey_checkpoint_attempt WHERE execution_plan_id=$1 "
                "AND finalization_epoch=$2",
                plan.execution_plan_id,
                epoch,
            )
            if any(row["checkpoint_id"] == checkpoint.checkpoint_id for row in existing):
                return "uncertain"
            if not await self._dependencies(
                connection, work=work, plan=plan, checkpoint=checkpoint, rows=existing
            ):
                raise ValueError("检查点依赖来源或权威恢复链未闭合")
            created = await connection.fetchval(
                """INSERT INTO qc_journey_checkpoint_attempt(attempt_id,execution_plan_id,
                    checkpoint_id,task_id,agent_run_id,session_id,finalization_epoch,work_id,
                    source_capture_id,intent_id,intent_payload,work_payload,status)
                   SELECT $1::text,$2::text,$3::text,$4::uuid,$5::uuid,$6::text,$7::int,
                          $8::text,$9::uuid,$10::text,
                          $11::jsonb,$12::jsonb,'reserved'
                   FROM qc_v2_work_item AS work JOIN qc_surface_session AS session
                     ON session.task_id=work.task_id AND session.session_id=work.session_id
                   JOIN qc_capture AS source ON source.id=$9::uuid
                   JOIN qc_agent_run AS run ON run.id=$5::uuid AND run.task_id=work.task_id
                   WHERE work.work_id=$8 AND work.task_id=$4::uuid AND work.session_id=$6
                     AND work.source_capture_id=source.id AND work.status='running'
                     AND work.finalization_epoch=$7 AND work.lease_expires_at>now()
                     AND work.deadline>now() AND run.status='running'
                     AND source.task_id=work.task_id AND source.agent_run_id=run.id
                     AND source.surface_session_id=session.session_id
                     AND source.capture_status='captured'
                     AND session.status='active' AND session.finalization_epoch=$7
                   ON CONFLICT DO NOTHING RETURNING true""",
                context["attempt_id"],
                plan.execution_plan_id,
                checkpoint.checkpoint_id,
                work.task_id,
                context["agent_run_id"],
                work.session_id,
                epoch,
                work.work_id,
                work.source_capture_id,
                decision.intent_id,
                _json(decision.to_dict()),
                _json(work.to_dict()),
            )
            if not created:
                # 并发插入赢家提交后复核原合同，不把唯一键冲突当成新动作许可。
                prior = await connection.fetchrow(
                    "SELECT * FROM qc_journey_checkpoint_attempt WHERE work_id=$1 FOR UPDATE",
                    work.work_id,
                )
                if prior is not None:
                    return _prior_claim(
                        prior, epoch=epoch, context=context, work=work, decision=decision
                    )
                raise ValueError("检查点准入失去 Work 租约、来源或会话")
            return "new"

    async def bind(self, *, work, context, turn, audit_only=False):
        assert_h5_journeys_active()
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            attempt = await connection.fetchrow(
                "SELECT * FROM qc_journey_checkpoint_attempt WHERE attempt_id=$1 FOR UPDATE",
                context["attempt_id"],
            )
            expected_status = "bound" if audit_only else "reserved"
            if attempt is None or attempt["status"] != expected_status:
                raise ValueError("检查点原尝试不可绑定")
            for key in (
                "execution_plan_id",
                "checkpoint_id",
                "task_id",
                "agent_run_id",
                "session_id",
                "source_capture_id",
                "work_id",
            ):
                if str(attempt[key]) != context.get(key):
                    raise ValueError("检查点证据尝试身份串属")
            if _object(attempt["intent_payload"]) != turn.decision.to_dict():
                raise ValueError("检查点结果不是原动作合同")
            coordination = turn.coordination
            action = await connection.fetchrow(
                "SELECT * FROM qc_action_execution WHERE action_execution_id=$1 FOR KEY SHARE",
                coordination.interaction.action_execution_id,
            )
            if action is None or _object(action["intent_payload"]) != turn.decision.to_dict():
                raise ValueError("检查点动作账本缺失或合同漂移")
            if action["created_at"] < attempt["created_at"]:
                raise ValueError("实际动作早于检查点尝试，禁止追认执行归属")
            captures, manifests = [], []
            for reference in (coordination.before_capture, coordination.after_capture):
                capture = await connection.fetchrow(
                    """SELECT id::text,task_id::text,agent_run_id::text,
                       surface_session_id AS session_id,
                       parent_capture_id::text,action_execution_id,capture_role AS role,
                       session_command_sequence AS command_sequence,
                       capture_status AS status,captured_at,
                       (metadata->>'finalization_epoch')::int AS epoch
                       FROM qc_capture WHERE id=$1::uuid FOR KEY SHARE""",
                    reference.capture_id,
                )
                row = await connection.fetchrow(
                    """SELECT * FROM qc_evidence_manifest WHERE capture_id=$1::uuid
                       ORDER BY manifest_version DESC LIMIT 1 FOR KEY SHARE""",
                    reference.capture_id,
                )
                artifacts = await connection.fetch(
                    "SELECT id::text,artifact_type,checksum,storage_path FROM qc_evidence_artifact "
                    "WHERE page_capture_id=$1::uuid AND task_id=$2::uuid",
                    reference.capture_id,
                    work.task_id,
                )
                if capture is None or row is None:
                    raise ValueError("检查点 Capture 或 Manifest 缺失")
                if capture["captured_at"] is None or capture["captured_at"] < attempt["created_at"]:
                    raise ValueError("Capture 早于检查点尝试，禁止用旧帧补造本次证据")
                if (
                    reference.task_id != capture["task_id"]
                    or reference.session_id != capture["session_id"]
                    or reference.role != capture["role"]
                    or reference.parent_capture_id != capture["parent_capture_id"]
                    or reference.action_execution_id != capture["action_execution_id"]
                ):
                    raise ValueError("检查点返回 Capture 引用与实际角色或父链不同")
                manifest = _object(row["manifest_payload"])
                EvidenceManifestReference.from_dict(manifest)
                if any(
                    manifest.get(key) != row[key]
                    for key in (
                        "manifest_id",
                        "manifest_version",
                        "manifest_hash",
                        "quality_status",
                    )
                ):
                    raise ValueError("Manifest 载荷与实际版本不一致")
                persisted = {item["id"]: item for item in artifacts}
                for item in manifest["artifacts"]:
                    actual = persisted.get(item["artifact_id"])
                    if actual is None or any(
                        actual[key] != item[key]
                        for key in ("artifact_type", "checksum", "storage_path")
                    ):
                        raise ValueError("检查点清单附件未实际绑定")
                capture = dict(capture)
                capture["artifact_ids"] = [item["id"] for item in artifacts]
                if set(reference.artifact_ids) != set(capture["artifact_ids"]):
                    raise ValueError("检查点返回 Capture 与实际附件不一致")
                captures.append(capture)
                manifests.append(manifest)
            scope = {
                **dict(context),
                "epoch": attempt["finalization_epoch"],
                "intent_id": attempt["intent_id"],
            }
            action = dict(action)
            for key in (
                "task_id",
                "requested_source_capture_id",
                "source_capture_id",
                "result_capture_id",
            ):
                action[key] = str(action[key]) if action[key] is not None else None
            bindings = bind_checkpoint_evidence(
                scope=scope, action=action, captures=captures, manifests=manifests
            )
            plan_payload = await connection.fetchval(
                "SELECT plan_payload FROM qc_journey_execution_plan WHERE execution_plan_id=$1",
                attempt["execution_plan_id"],
            )
            plan = parse_journey_execution_plan(_object(plan_payload))
            checkpoint = next(
                item for item in plan.checkpoints if item.checkpoint_id == attempt["checkpoint_id"]
            )
            # 业务角色来自冻结合同，附件和物理角色来自实际账本；绑定不代表义务已达标。
            bindings = tuple(
                {
                    **binding,
                    "execution_plan_id": plan.execution_plan_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "checkpoint_hash": stable_hash(checkpoint.to_dict()),
                    "manifest_hash": manifest["manifest_hash"],
                    "evidence_roles": [
                        requirement.role
                        for requirement in checkpoint.evidence_requirements
                        if requirement.capture_phase == binding["role"]
                    ],
                    "evidence_requirements": [
                        requirement.to_dict()
                        for requirement in checkpoint.evidence_requirements
                        if requirement.capture_phase == binding["role"]
                    ],
                }
                for binding, manifest in zip(bindings, manifests, strict=True)
            )
            interaction = coordination.interaction
            if (
                interaction.task_id != work.task_id
                or interaction.session_id != work.session_id
                or interaction.intent_id != attempt["intent_id"]
                or interaction.source_capture_id != captures[0]["id"]
                or interaction.result_capture_id != captures[1]["id"]
            ):
                raise ValueError("检查点返回动作结果与实际父链不同")
            assert_h5_journeys_active()
            for binding in bindings:
                if audit_only:
                    continue
                await connection.execute(
                    """INSERT INTO qc_journey_checkpoint_evidence(attempt_id,role,capture_id,
                       action_execution_id,manifest_id,manifest_version,binding_payload)
                       VALUES($1,$2,$3::uuid,$4,$5,$6,$7::jsonb)""",
                    context["attempt_id"],
                    binding["role"],
                    binding["capture_id"],
                    binding["action_execution_id"],
                    binding["manifest_id"],
                    binding["manifest_version"],
                    _json(binding),
                )
            observed = False
            if self._observer is not None:
                from services.api.app.application.h5_checkpoint_observation import (
                    checkpoint_needs_payment_receipt,
                )

                receipt = ({"action_receipt": _object(action["result_payload"])}
                           if checkpoint_needs_payment_receipt(checkpoint) else {})
                observed = await self._observer.result_matches(
                    plan_id=context["execution_plan_id"],
                    checkpoint_id=context["checkpoint_id"],
                    captures=captures,
                    manifests=manifests,
                    **receipt,
                )
            assert_h5_journeys_active()
            observation_id = "checkpoint-observation:" + stable_hash(
                {
                    "attempt_id": context["attempt_id"],
                    "bindings": bindings,
                    "observed": observed,
                }
            )
            await connection.execute(
                """INSERT INTO qc_journey_checkpoint_observation(observation_id,attempt_id,
                   task_id,finalization_epoch,observed,bindings)
                   VALUES($1,$2,$3::uuid,$4,$5,$6::jsonb) ON CONFLICT DO NOTHING""",
                observation_id,
                context["attempt_id"],
                work.task_id,
                attempt["finalization_epoch"],
                observed,
                _json(bindings),
            )
            if audit_only:
                return observed
            await connection.execute(
                """UPDATE qc_journey_checkpoint_attempt SET status='bound',turn_payload=$2::jsonb,
                   observation_status=$3 WHERE attempt_id=$1 AND status='reserved'""",
                context["attempt_id"],
                _json(
                    {"decision": turn.decision.to_dict(), "coordination": coordination.to_dict()}
                ),
                "observed" if observed else "needs_review",
            )
            return observed

    async def fail(self, *, context, reason):
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE qc_journey_checkpoint_attempt SET status='gap',reason=$2 "
                "WHERE attempt_id=$1 AND status='reserved'",
                context["attempt_id"],
                reason,
            )

    async def _read_attempt(self, *, work, context):
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT attempt.* FROM qc_journey_checkpoint_attempt AS attempt "
                "JOIN qc_task AS task ON task.id=attempt.task_id "
                "AND task.finalization_epoch=attempt.finalization_epoch "
                "WHERE attempt.attempt_id=$1",
                context["attempt_id"],
            )
        if row is None:
            return None
        for key in (
            "execution_plan_id",
            "checkpoint_id",
            "task_id",
            "agent_run_id",
            "session_id",
            "source_capture_id",
            "work_id",
        ):
            if str(row[key]) != context.get(key):
                raise ValueError("检查点原结果身份串属")
        if ExplorationWork.from_dict(_object(row["work_payload"])) != work:
            raise ValueError("检查点原结果 Work 合同漂移")
        return row

    async def recover_attempt(self, *, work, context):
        row = await self._read_attempt(work=work, context=context)
        if row is not None:
            return row["status"]
        # 旧 Epoch 不能伪装成未尝试，再次准入或重放动作。
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            exists = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM qc_journey_checkpoint_attempt WHERE work_id=$1)",
                work.work_id,
            )
        if exists:
            raise ValueError("检查点原尝试不属于当前 Epoch 或身份")
        return None

    async def read_turn(self, *, work, context):
        row = await self._read_attempt(work=work, context=context)
        if row is None or row["status"] != "bound" or row["turn_payload"] is None:
            raise ValueError("检查点原结果不存在")
        payload = _object(row["turn_payload"])
        coordination = payload["coordination"]
        if coordination.get("exit_gate_decision") is not None:
            from services.api.app.domain.exploration import EvidenceExitGateDecision

            gate = EvidenceExitGateDecision.from_dict(coordination["exit_gate_decision"])
        else:
            gate = None
        return ExplorerTurnResult(
            work_id=work.work_id,
            task_id=work.task_id,
            decision=ActionIntent.from_dict(payload["decision"]),
            coordination=CaptureCoordinationResult(
                before_capture=CaptureRef.from_dict(coordination["before_capture"]),
                after_capture=CaptureRef.from_dict(coordination["after_capture"]),
                interaction=InteractionResult.from_dict(coordination["interaction"]),
                exit_gate_decision=gate,
            ),
        )
