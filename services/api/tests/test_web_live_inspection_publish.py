from __future__ import annotations

import pytest

from services.api.app.application import inspection_composition
from services.api.app.application.web_live_inspection_publish import (
    WebLiveInspectionPublishService,
)
from services.api.app.infrastructure.db.web_live_inspection_repository import (
    PostgresWebLiveInspectionSourceRunReader,
)


class _SourceRunReader:
    def __init__(self, source_run_id: str | None) -> None:
        self.source_run_id = source_run_id
        self.calls: list[str] = []

    async def find_source_agent_run_id(self, *, task_id: str) -> str | None:
        self.calls.append(task_id)
        return self.source_run_id


@pytest.mark.asyncio
async def test_web_sequence_pending_phase_reuses_bounded_live_entry_before_stable_scan():
    from services.api.app.application.live_inspection_publication import (
        LiveInspectionPublicationBatchResult,
    )

    class SequencePending:
        async def publish_pending_batch(self,**kwargs):
            assert kwargs=={'task_id':'task-1','limit':2,'cursor':None}
            return LiveInspectionPublicationBatchResult(1,1,True,'web-sequence:publication-1')

    publisher = _Publisher()
    service = WebLiveInspectionPublishService(source_run_reader=_SourceRunReader('main-run'),
        publisher=publisher,sequence_publisher=SequencePending())
    result = await service.publish_pending_batch(task_id='task-1',agent_run_id='judge-run',limit=2,cursor=None)
    assert result.created_count==1 and result.next_cursor=='web-sequence:publication-1'
    assert not publisher.calls


class _Publisher:
    def __init__(self, published_count: int = 3) -> None:
        self.published_count = published_count
        self.calls: list[dict[str, object]] = []

    async def publish_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int | None = None,
    ) -> int:
        self.calls.append(
            {"task_id": task_id, "agent_run_id": agent_run_id, "limit": limit}
        )
        return self.published_count

    async def publish_pending_batch(self, **kwargs: object):
        self.calls.append(dict(kwargs))
        return type(
            "BatchResult",
            (),
            {
                "attempted_count": 1,
                "created_count": self.published_count,
                "has_more": False,
                "next_cursor": None,
            },
        )()


@pytest.mark.asyncio
async def test_web_live_publish_maps_consumer_run_to_source_main_run() -> None:
    reader = _SourceRunReader("main-run")
    publisher = _Publisher()
    service = WebLiveInspectionPublishService(
        source_run_reader=reader,
        publisher=publisher,
    )

    published = await service.publish_pending(
        task_id="task-1",
        agent_run_id="inspection-consumer-run",
        limit=None,
    )

    assert published == 3
    assert reader.calls == ["task-1"]
    assert publisher.calls == [
        {"task_id": "task-1", "agent_run_id": "main-run", "limit": None}
    ]


@pytest.mark.asyncio
async def test_web_live_publish_forwards_batch_limit_and_cursor() -> None:
    publisher = _Publisher()
    service = WebLiveInspectionPublishService(
        source_run_reader=_SourceRunReader("main-run"),
        publisher=publisher,
    )

    result = await service.publish_pending_batch(
        task_id="task-1",
        agent_run_id="inspection-consumer-run",
        limit=2,
        cursor="capture-7",
    )

    assert publisher.calls == [
        {
            "task_id": "task-1",
            "agent_run_id": "main-run",
            "limit": 2,
            "cursor": "capture-7",
        }
    ]
    assert result.attempted_count == 1


@pytest.mark.asyncio
async def test_web_live_publish_skips_task_without_current_web_main_run() -> None:
    publisher = _Publisher()
    service = WebLiveInspectionPublishService(
        source_run_reader=_SourceRunReader(None),
        publisher=publisher,
    )

    published = await service.publish_pending(
        task_id="task-1",
        agent_run_id="inspection-consumer-run",
    )

    assert published == 0
    assert publisher.calls == []


class _Connection:
    def __init__(self) -> None:
        self.query = ""
        self.args: tuple[object, ...] = ()

    async def fetchval(self, query: str, *args: object) -> str:
        self.query = " ".join(query.split())
        self.args = args
        return "main-run"


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


@pytest.mark.asyncio
async def test_postgres_web_source_run_reader_is_scoped_to_current_v2_session() -> None:
    connection = _Connection()
    reader = PostgresWebLiveInspectionSourceRunReader(pool=_Pool(connection))

    run_id = await reader.find_source_agent_run_id(task_id="task-1")

    assert run_id == "main-run"
    assert connection.args == ("task-1",)
    assert "task.target_platform = 'web'" in connection.query
    assert "task.task_config->>'workflow_version' = 'v2'" in connection.query
    assert "task.task_config->>'execution_mode' = 'full_inspection'" in connection.query
    assert "task.task_config->>'web_inspection_notification_mode' = 'enforced'" in connection.query
    assert "run.agent_type = 'main'" in connection.query
    assert "session.session_id = ('web:' || run.id::text)" in connection.query
    assert "session.session_owner = 'web-v2-workflow'" in connection.query
    assert "session.finalization_epoch = task.finalization_epoch" in connection.query
    assert "competing_run.agent_type = 'main'" in connection.query


def test_default_web_live_publish_wraps_active_session_work_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_reader = _SourceRunReader("main-run")
    publisher = _Publisher()
    monkeypatch.setattr(
        inspection_composition,
        "PostgresWebLiveInspectionSourceRunReader",
        lambda: source_reader,
    )
    monkeypatch.setattr(
        inspection_composition,
        "build_default_h5_live_inspection_publish_service",
        lambda: publisher,
        raising=False,
    )
    monkeypatch.setattr(
        inspection_composition,
        "build_default_web_page_inspection_notification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("Web live 适配器不得只委托 Publication 通知器")
        ),
    )

    service = inspection_composition.build_default_web_live_inspection_publish_service(
        settings=object()
    )

    assert service._source_run_reader is source_reader
    assert service._publisher is publisher
