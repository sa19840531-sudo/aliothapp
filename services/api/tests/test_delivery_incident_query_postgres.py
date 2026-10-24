"""在隔离 PG 临时表验证事故列表的分页、版本围栏、消息及消费者计数。"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.delivery_operations import DeliveryPlatform
from services.api.app.infrastructure.db.delivery_operation_repository import (
    _incident_query,
    _publication_message_type,
    _publication_tables,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)


@pytest.fixture
async def delivery_connection():
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式设置完整迁移后的隔离 ALIOTH_TEST_DATABASE_URL")
    connection = await asyncpg.connect(
        require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL") or None)
    )
    try:
        # 复用迁移后的真实列和默认值，临时表随连接关闭清理，不触碰业务事实。
        for name in (
            "qc_h5_inspection_publication",
            "qc_app_inspection_publication",
            "qc_h5_inspection_publication_work_item",
            "qc_app_inspection_publication_work_item",
            "qc_outbox_event",
            "qc_event_inbox",
            "qc_delivery_operation_audit",
        ):
            await connection.execute(
                f"CREATE TEMP TABLE {name} (LIKE public.{name} INCLUDING DEFAULTS)"
            )
        await connection.execute(
            "CREATE TEMP TABLE qc_inspection_work_item"
            "(id uuid,status varchar(32),row_version bigint)"
        )
        yield connection
    finally:
        await connection.close()


async def _publication(connection, platform, task, publication, updated):
    table, _ = _publication_tables(platform)
    if platform is DeliveryPlatform.H5:
        await connection.execute(
            f"""INSERT INTO {table}(id,task_id,source_agent_run_id,page_capture_id,
                page_type_assessment_id,assessment_hash,ruleset_id,ruleset_version,
                finalization_epoch,input_hash,mode,row_version,updated_at)
                VALUES($1::uuid,$2::uuid,$1::uuid,$1::uuid,$1::text,'测试评估',
                       $1::uuid,1,0,$1::text,'enforced',3,$3)""",
            publication,
            task,
            updated,
        )
    else:
        await connection.execute(
            f"""INSERT INTO {table}(id,task_id,source_agent_run_id,evidence_index_id,
                evidence_index_hash,ruleset_id,ruleset_version,finalization_epoch,
                input_hash,expected_entry_count,row_version,updated_at)
                VALUES($1::uuid,$2::uuid,$1::uuid,$1::text,'测试证据索引',
                       $1::uuid,1,0,$1::text,1,3,$3)""",
            publication,
            task,
            updated,
        )


async def _outbox(connection, *, task, kind, aggregate, version, message_type, status):
    message = str(uuid4())
    await connection.execute(
        """INSERT INTO qc_outbox_event(message_id,task_id,aggregate_type,aggregate_id,
              aggregate_version,message_type,protocol_version,payload_protocol_version,
              idempotency_key,envelope_payload,occurred_at,status)
           VALUES($1,$2::uuid,$3,$4,$5,$6,'回归','回归',$1,'{}',now(),$7)""",
        message,
        task,
        kind,
        aggregate,
        version,
        message_type,
        status,
    )
    return message


async def _inbox(connection, message, task, consumer, status):
    await connection.execute(
        """INSERT INTO qc_event_inbox(consumer_name,message_id,task_id,message_type,
              aggregate_type,aggregate_id,aggregate_version,idempotency_key,status)
           VALUES($1,$2,$3::uuid,'回归','回归','回归',0,$2,$4)""",
        consumer,
        message,
        task,
        status,
    )


@pytest.mark.parametrize("platform", [DeliveryPlatform.H5, DeliveryPlatform.APP])
@pytest.mark.parametrize("limit", [1, 2])
async def test_paged_incidents_match_original_single_publication_projection(
    delivery_connection, platform, limit
):
    connection = delivery_connection
    task, other_task = str(uuid4()), str(uuid4())
    publications = [str(uuid4()) for _ in range(3)]
    now = datetime.now(UTC)
    for index, publication in enumerate(publications):
        await _publication(connection, platform, task, publication, now - timedelta(minutes=index))
    await _publication(connection, platform, other_task, str(uuid4()), now + timedelta(minutes=1))
    _, link = _publication_tables(platform)
    publication = publications[0]
    work = str(uuid4())
    await connection.execute(
        "INSERT INTO qc_inspection_work_item VALUES($1::uuid,'pending',5)", work
    )
    extra_columns = ",entry_id" if platform is DeliveryPlatform.APP else ""
    extra_values = ",1" if platform is DeliveryPlatform.APP else ""
    await connection.execute(
        f"INSERT INTO {link}(publication_id,work_item_id,batch_key{extra_columns}) "
        f"VALUES($1::uuid,$2::uuid,'测试批次'{extra_values})",
        publication,
        work,
    )
    kind = f"{platform.value}_inspection_publication"
    message_type = _publication_message_type(platform)
    direct = await _outbox(
        connection,
        task=task,
        kind=kind,
        aggregate=publication,
        version=3,
        message_type=message_type,
        status="pending",
    )
    linked = await _outbox(
        connection,
        task=task,
        kind="inspection_work_item",
        aggregate=work,
        version=5,
        message_type=message_type,
        status="dead_letter",
    )
    await _inbox(connection, direct, task, "消费者甲", "pending")
    await _inbox(connection, direct, task, "消费者乙", "dead_letter")
    await _inbox(connection, linked, task, "消费者甲", "retry_wait")
    # 同一聚合但版本、任务或消息类型不同，均不能进入当前投递统计。
    for wrong_task, wrong_version, wrong_type in (
        (task, 2, message_type),
        (other_task, 3, message_type),
        (task, 3, "其他消息"),
    ):
        await _outbox(
            connection,
            task=wrong_task,
            kind=kind,
            aggregate=publication,
            version=wrong_version,
            message_type=wrong_type,
            status="pending",
        )
    await _outbox(
        connection,
        task=task,
        kind="inspection_work_item",
        aggregate=work,
        version=4,
        message_type=message_type,
        status="pending",
    )
    rows = [
        dict(row)
        for row in await connection.fetch(
            _incident_query(platform, by_publication=False), task, limit
        )
    ]
    baseline = [
        dict(
            await connection.fetchrow(
                _incident_query(platform, by_publication=True), publication_id
            )
        )
        for publication_id in publications[:limit]
    ]
    assert rows == baseline
    assert [row["publication_id"] for row in rows] == publications[:limit]
    assert rows[0]["outbox_pending_count"] == rows[0]["outbox_dead_letter_count"] == 1
    assert rows[0]["inbox_pending_count"] == 2
    assert rows[0]["inbox_dead_letter_count"] == 1
    assert rows[0]["linked_work_item_count"] == rows[0]["linked_work_active_count"] == 1
    if limit > 1:
        assert rows[1]["outbox_pending_count"] == rows[1]["inbox_pending_count"] == 0


@pytest.mark.parametrize("platform", [DeliveryPlatform.H5, DeliveryPlatform.APP])
async def test_empty_incident_list_has_no_synthetic_projection(delivery_connection, platform):
    assert (
        await delivery_connection.fetch(
            _incident_query(platform, by_publication=False), str(uuid4()), 20
        )
        == []
    )
