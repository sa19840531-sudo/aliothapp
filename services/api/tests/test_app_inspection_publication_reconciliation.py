"""封存证据交接恢复的幂等、围栏传递及逐任务失败隔离。"""

import asyncio
from types import SimpleNamespace

from services.api.app.application.app_inspection_publication import AppUnpublishedSealedIndex
from services.api.app.application.app_inspection_publication_reconciliation import (
    AppInspectionPublicationReconciler,
)
from services.api.app.worker import _app_publication_reconciliation_loop
from services.api.tests.test_app_sealed_inspection_publisher import _publisher, _sealed_index


async def test_recovery_reuses_formal_sealed_validation_and_passes_epoch() -> None:
    publisher, _, _, writer = _publisher(_sealed_index())

    class Repository:
        async def list_unpublished_sealed_indexes(self, *, limit):
            assert limit == 10
            return (
                AppUnpublishedSealedIndex(
                    task_id="task-1", source_agent_run_id="run-1", finalization_epoch=7
                ),
            )

    recovered = await AppInspectionPublicationReconciler(
        repository=Repository(), publisher=publisher
    ).reconcile(limit=10)

    assert recovered == 1
    assert writer.calls[0]["expected_finalization_epoch"] == 7
    assert len(writer.calls[0]["entries"]) == 2


async def test_one_failed_handoff_does_not_block_later_tasks_and_replay_is_empty() -> None:
    candidates = {
        name: AppUnpublishedSealedIndex(
            task_id=name, source_agent_run_id=f"run-{name}", finalization_epoch=3
        )
        for name in ("bad", "good")
    }
    calls = []

    class Repository:
        async def list_unpublished_sealed_indexes(self, *, limit):
            return tuple(candidates.values())

    class Publisher:
        async def publish(self, **kwargs):
            calls.append(kwargs)
            if kwargs["task_id"] == "bad":
                raise RuntimeError("归属围栏已变化")
            candidates.pop(kwargs["task_id"])
            return SimpleNamespace(publication_id="publication-good")

    reconciler = AppInspectionPublicationReconciler(
        repository=Repository(), publisher=Publisher()
    )
    assert await reconciler.reconcile() == 1
    assert [call["task_id"] for call in calls] == ["bad", "good"]
    candidates.clear()
    assert await reconciler.reconcile() == 0
    assert len(calls) == 2


async def test_failed_missing_handoff_scan_still_reconciles_existing_publications() -> None:
    stop = asyncio.Event()
    calls = []

    class Repository:
        async def list_unpublished_sealed_indexes(self, *, limit):
            raise RuntimeError("模拟一次扫描失败")

        async def reconcile(self):
            calls.append("existing_publications")
            stop.set()

    await _app_publication_reconciliation_loop(
        Repository(), stop, sealed_inspection_publisher=SimpleNamespace(publish=None)
    )

    assert calls == ["existing_publications"]
