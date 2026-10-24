"""T9 实际 Work Consumer/Maf/RuleJudge/Writer 隔离PG组合，不调用外部模型。"""

from services.api.app.agents.rule_judge.maf_agent import MafRuleJudgeAgent
from services.api.app.agents.runtime.inspection_work_item_consumer import InspectionWorkItemConsumer
from services.api.app.application.inspection_work_items import InspectionWorkItemService
from services.api.app.application.manifest_backed_packet import ManifestBackedPacketLoader
from services.api.app.application.observation_packet import ObservationPacketService
from services.api.app.application.rule_execution import RuleExecutionService
from services.api.app.application.rule_judge import RuleJudgeService
from services.api.app.application.semantic_rule_judge import SemanticJudgeModelOutput
from services.api.app.application.web_sequence_reader import WebSequenceObservationReader
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.observation_packet_repository import (
    PostgresObservationPacketRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.web_sequence_binding_reader import (
    PostgresWebSequenceBindingReader,
)


async def consume_sequence_with_actual_work(
    connection,
    pool,
    store,
    ids,
    consumer_run_id,
    monkeypatch,
    *,
    conclusion="pass",
    crash_after_write=False,
    factory_mode=None,
    writer_tamper=None,
    read_tamper=None,
):
    from services.api.app.agents.tools import rule_tools
    from services.api.app.application import inspection_handoff
    from services.api.app.application.app_action_evidence import PostgresAppActionEvidenceStore

    class ArtifactStore:
        async def read(self, storage_path):
            return store[storage_path]

    class BusinessJudge:
        def __init__(self):
            self.packets = []

        async def judge(self, *, packet, tool_results):
            self.packets.append(packet)
            return SemanticJudgeModelOutput(
                conclusion=conclusion,
                confidence=0.95,
                reasoning_summary="局部模型替身按每条规则独立返回业务结论",
                evidence_refs=[packet.evidence_artifact_ids[-1]],
            )

        async def judge_batch(self, *, items):
            return {
                packet.rule_code: await self.judge(packet=packet, tool_results=results)
                for packet, results in items
            }

    artifacts = ArtifactStore()
    contexts = PostgresObservationPacketRepository(pool=pool, artifact_store=artifacts)
    tasks = PostgresTaskRepository(pool=pool)
    if writer_tamper is not None:
        from dataclasses import replace
        from uuid import uuid4

        write = tasks.record_rule_execution

        async def tampered_write(command):
            if writer_tamper == "no_contract_difference":
                import pytest

                from services.api.app.application.rule_execution import (
                    RuleExecutionLeaseConflictError,
                )

                with pytest.raises(RuleExecutionLeaseConflictError):
                    await PostgresInspectionWorkItemRepository(
                        pool=pool
                    ).terminate_web_contract_incompatibility(command=command)
            if writer_tamper == "evidence":
                command = replace(command, evidence_artifact_ids=[str(uuid4())])
            elif writer_tamper == "packet":
                command = replace(command, observation_packet_id=str(uuid4()))
            elif writer_tamper == "lease":
                command = replace(command, lease_token=str(uuid4()))
            return await write(command)

        monkeypatch.setattr(tasks, "record_rule_execution", tampered_write)
    reader = WebSequenceObservationReader(
        inner=contexts,
        contexts=contexts,
        bindings=PostgresWebSequenceBindingReader(pool=pool),
        packet_loader=ManifestBackedPacketLoader(
            manifests=PostgresEvidenceManifestRepository(pool=pool), artifact_reader=artifacts
        ),
    )
    business = BusinessJudge()
    judge = MafRuleJudgeAgent(
        service=RuleJudgeService(
            repository=tasks,
            observation_packet_service=ObservationPacketService(repository=reader),
            semantic_judge=business,
        )
    )
    if factory_mode is not None:
        from services.api.tests.web_sequence_factory_pg_support import build_actual_factory

        judge = await build_actual_factory(
            pool, ids, artifacts, tasks, business, factory_mode, monkeypatch
        )
    monkeypatch.setattr(
        rule_tools, "_get_rule_execution_service", lambda: RuleExecutionService(repository=tasks)
    )
    monkeypatch.setattr(
        inspection_handoff,
        "PostgresAppActionEvidenceStore",
        lambda: PostgresAppActionEvidenceStore(pool=pool),
    )
    await connection.execute(
        "UPDATE qc_agent_run SET status='running' WHERE id=$1::uuid", consumer_run_id
    )
    from services.api.app.agents.runtime.inspection_result_writer import CurrentRuleDecisionWriter

    class CrashAfterWriter:
        async def write(self, **kwargs):
            await CurrentRuleDecisionWriter().write(**kwargs)
            raise RuntimeError("模拟正式提交后进程中断")

    tamper_applied = []
    if read_tamper is not None:
        from dataclasses import replace
        from uuid import uuid4

        original_judge = judge

        class TamperedReaderCaller:
            async def judge(self, handoff):
                if read_tamper in {"consumer", "attempt", "work", "token"}:
                    field = {
                        "consumer": "agent_run_id",
                        "attempt": "attempt_id",
                        "work": "work_item_id",
                        "token": "lease_token",
                    }[read_tamper]
                    if field == "lease_token":
                        from copy import copy

                        handoff = copy(handoff)
                        object.__setattr__(handoff, field, str(uuid4()))
                    else:
                        handoff = replace(handoff, **{field: str(uuid4())})
                elif read_tamper in {"expired", "replaced"}:
                    await connection.execute(
                        "UPDATE qc_inspection_work_item SET lease_expires_at=now()-interval '1 second' WHERE id=$1::uuid",
                        handoff.work_item_id,
                    )
                    if read_tamper == "replaced":
                        from services.api.app.agents.runtime.inspection_work_item_execution import (
                            InspectionWorkItemAttempt,
                        )
                        from services.api.app.application.inspection_work_items import (
                            ClaimInspectionWorkItemCommand,
                        )

                        service = InspectionWorkItemService(
                            repository=PostgresInspectionWorkItemRepository(pool=pool)
                        )
                        renewed = await service.claim(
                            ClaimInspectionWorkItemCommand(
                                task_id=ids["task"],
                                consumer_agent_run_id=consumer_run_id,
                                lease_owner="t9-fix1-replacement",
                                lease_seconds=60,
                                session_dependency="snapshot",
                            )
                        )
                        from services.api.app.agents.runtime.run_context import (
                            get_agent_run_context,
                        )

                        assert (
                            renewed is not None
                            and renewed.lease_token != get_agent_run_context().lease_token
                        )
                        replacement = InspectionWorkItemAttempt(
                            service=service,
                            lease=renewed,
                            consumer_agent_run_id=consumer_run_id,
                            lease_seconds=60,
                            retry_backoff_seconds=0.1,
                        )
                        await replacement.start()
                tamper_applied.append(True)
                return await original_judge.judge(handoff)

        judge = TamperedReaderCaller()

    consumer = InspectionWorkItemConsumer(
        service=InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository(pool=pool)
        ),
        lease_owner="t9-real-work",
        lease_seconds=60,
        retry_backoff_seconds=0.1,
        judge=judge,
        max_drain_items=1,
        result_writer=CrashAfterWriter() if crash_after_write else None,
    )
    result = await consumer.drain_snapshot(
        task_id=ids["task"], consumer_agent_run_id=consumer_run_id
    )
    if read_tamper is not None:
        assert tamper_applied, "负向夹具必须实际到达读取入口，不能用夹具异常冒充拒绝"
    return result, business.packets
