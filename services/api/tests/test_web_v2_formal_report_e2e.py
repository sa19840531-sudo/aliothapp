"""Web V2 从公开入口到正式报告的真实 PostgreSQL 回归。

该文件只允许连接任务简报指定的隔离库；未显式设置时跳过，
绝不读取 ``ALIOTH_DATABASE_URL`` 或项目 ``.env`` 作为回退。
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

import asyncpg
import fakeredis
import httpx
import pytest

from services.api.app import worker as worker_module
from services.api.app.agents.runtime.business_middlewares.base import TaskRuntimeSnapshot
from services.api.app.agents.runtime.h5_inspection_publication_wakeup import (
    H5InspectionPublicationWakeupHandler,
)
from services.api.app.api.v2 import tasks as v2_tasks_module
from services.api.app.application import rule_auth_gate as rule_auth_gate_module
from services.api.app.application.app_inspection_publisher import (
    AppCaptureInspectionPublisher,
)
from services.api.app.application.h5_live_inspection_publish import (
    H5LiveInspectionPublishService,
    PostgresH5LiveInspectionTaskGateReader,
)
from services.api.app.application.inspection_work_items import (
    ClaimInspectionWorkItemCommand,
    InspectionAttemptStatus,
    InspectionWorkItemLease,
    InspectionWorkItemRuleStatus,
    InspectionWorkItemService,
    InspectionWorkItemStatus,
    RenewInspectionWorkItemLeaseCommand,
    StartInspectionAttemptCommand,
    TransitionInspectionAttemptCommand,
    TransitionInspectionWorkItemCommand,
    TransitionInspectionWorkItemRuleCommand,
)
from services.api.app.application.outbox import (
    INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
    SUPPORTED_WAKEUP_MESSAGE_TYPES,
    OutboxLease,
    OutboxRelay,
    WakeupTransportReceipt,
)
from services.api.app.application.page_capture_persistence import (
    PageCapturePersistenceService,
    PersistedEvidenceArtifact,
)
from services.api.app.application.report_generation import ReportGenerationService
from services.api.app.application.rule_execution import (
    RecordRuleExecutionCommand,
    RuleExecutionService,
)
from services.api.app.application.task_finalization import (
    TaskFinalizationService,
    TerminateTaskFinalizationCommand,
)
from services.api.app.application.task_query import TaskQueryService
from services.api.app.application.task_submission import TaskSubmissionService
from services.api.app.application.v2_task_entries import (
    ExplorationTaskV2EntryService,
    TaskSubmissionV2EntryService,
)
from services.api.app.application.web_inspection_reconciliation import (
    ReconcileWebInspectionCommand,
    WebInspectionReconciliation,
)
from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageStructureFacts,
    WebReadyPageCapture,
)
from services.api.app.application.web_page_inspection_notification import (
    WebPageInspectionNotification,
)
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeAssessmentRecorder,
)
from services.api.app.application.workstation_tasks import WorkstationTaskListFilter
from services.api.app.domain.exploration.web_coverage_snapshot import (
    WebCoverageSnapshot,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.inspection import (
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
)
from services.api.app.domain.protocol import ProtocolEnvelope
from services.api.app.domain.task import CompletionBlockReason, TaskExecutionStatus
from services.api.app.domain.task.submission import (
    TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureResult,
)
from services.api.app.infrastructure.db.h5_evidence_handoff_repository import (
    PostgresH5EvidenceCaptureReader,
)
from services.api.app.infrastructure.db.h5_evidence_inventory_repository import (
    PostgresH5EvidenceInventoryRepository,
)
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.outbox_repository import PostgresOutboxRepository
from services.api.app.infrastructure.db.page_capture_repository import (
    PostgresPageCaptureRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    _GATE_FACTS_SQL,
    PostgresTaskFinalizationRepository,
    _parse_gate_facts,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_completion_gate_sql import (
    WEB_COMPLETION_GATE_INVENTORY_CTES_SQL,
)
from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
    PostgresWebCoverageSnapshotRepository,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue
from services.api.app.infrastructure.queue.redis_stream import RedisStreamWakeup
from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore
from services.api.app.main import create_app

_ISOLATED_DATABASE_NAME = "alioth_test_web_v2"


def _require_formal_test_database_url():
    from urllib.parse import urlsplit

    from services.api.app.infrastructure.db.test_database_guard import (
        require_isolated_test_database_url,
    )

    database_url = os.environ.get("ALIOTH_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("需显式设置任务指定的 ALIOTH_TEST_DATABASE_URL")
    primary = os.environ.get("ALIOTH_DATABASE_URL")
    if not primary:
        pytest.fail("必须显式提供主库比较地址")
    require_isolated_test_database_url(database_url, primary,
        allowed_database_names=frozenset({_ISOLATED_DATABASE_NAME, "alioth_test_unbounded_menu"}),
        allowed_roles=frozenset({"alioth_web_gap", "alioth"}))
    parsed = urlsplit(database_url)
    permitted_endpoints = {
        ("127.0.0.1", 55439, _ISOLATED_DATABASE_NAME, "alioth_web_gap"),
        ("alioth-postgres", 5432, "alioth_test_unbounded_menu", "alioth"),
    }
    if (parsed.hostname, parsed.port, parsed.path.lstrip("/"), parsed.username) not in permitted_endpoints:
        pytest.fail("仅允许显式批准的隔离数据库端点与角色组合")
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


class _TransactionScopedPool:
    """让公开 API 使用同一真实 PG 事务，用例结束后整体回滚。"""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self._connection


@dataclass(frozen=True)
class _PostgresCase:
    connection: asyncpg.Connection
    pool: Any
    tenant_id: str
    user_id: str
    ruleset_id: str
    observed_rule_id: str
    gap_rule_id: str
    ruleset_code: str

    @property
    def access_scope(self) -> AccessScope:
        return AccessScope(
            actor_user_id=self.user_id,
            tenant_id=self.tenant_id,
            permissions=frozenset(),
        )


class _BridgeArtifactStore:
    """仅替代对象存储，Capture 与附件元数据仍走真实 PostgreSQL 仓储。"""

    async def store(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        artifact_type: str,
        content: object,
    ) -> PersistedEvidenceArtifact:
        raw_content = content if isinstance(content, bytes) else str(content).encode("utf-8")
        content_type = {
            "screenshot": "image/png",
            "dom": "text/html; charset=utf-8",
            "visible_text": "text/plain; charset=utf-8",
        }[artifact_type]
        return PersistedEvidenceArtifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            storage_path=(
                f"tests/web-v2-bridge/{task_id}/{page_capture_id}/{artifact_id}"
            ),
            content_type=content_type,
            checksum=f"sha256:{sha256(raw_content).hexdigest()}",
            size_bytes=len(raw_content),
            metadata={},
        )

    async def delete(self, storage_path: str) -> None:
        del storage_path

    async def read(self, storage_path: str) -> bytes:
        del storage_path
        return b"x"


@pytest.fixture
async def postgres_case() -> _PostgresCase:
    database_url = _require_formal_test_database_url()
    connection = await asyncpg.connect(database_url, timeout=5, command_timeout=15)
    transaction = connection.transaction()
    await transaction.start()
    tenant_id, user_id, ruleset_id, observed_rule_id, gap_rule_id = (
        str(uuid4()) for _ in range(5)
    )
    try:
        database_name = await connection.fetchval("SELECT current_database()")
        database_role = await connection.fetchval("SELECT current_user")
        assert (database_name, database_role) in {
            (_ISOLATED_DATABASE_NAME, "alioth_web_gap"),
            ("alioth_test_unbounded_menu", "alioth"),
        }
        await connection.execute(
            """
            INSERT INTO qc_tenant (id, tenant_key, display_name)
            VALUES ($1::uuid, $2, 'Web V2 E2E')
            """,
            tenant_id,
            f"web-v2-e2e-{tenant_id}",
        )
        await connection.execute(
            """
            INSERT INTO qc_user (
                id, username, full_name, password_hash, role, tenant_id
            ) VALUES (
                $1::uuid, $2, 'Web V2 E2E', 'not-a-login-secret',
                '平台管理员', $3::uuid
            )
            """,
            user_id,
            f"web-v2-e2e-{user_id}",
            tenant_id,
        )
        await connection.execute(
            """
            INSERT INTO qc_ruleset (
                id, ruleset_code, name, version, status, snapshot_hash,
                rule_count, source_type
            ) VALUES (
                $1::uuid, $2, 'Web V2 E2E', 1, 'enabled',
                'web-v2-e2e-snapshot', 2, 'manual'
            )
            """,
            ruleset_id,
            f"web_v2_e2e_{ruleset_id}",
        )
        await connection.executemany(
            """
            INSERT INTO qc_rule (
                id, ruleset_id, rule_code, name, category, executor_type,
                severity, status, check_point, applicable_page_types,
                input_contract, manual_review_policy
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4, 'visual', 'hybrid',
                'medium', 'enabled', $5, $6::jsonb, $7::jsonb,
                '{"min_confidence":0.8}'::jsonb
            )
            """,
            (
                (
                    observed_rule_id,
                    ruleset_id,
                    "web_e2e_observed",
                    "Web 已采集页面规则",
                    "检查已采集页面",
                    '["content_list","content_detail"]',
                    '{"required_artifacts":["screenshot","action_trace"],'
                    '"target_clients":["WEB"]}',
                ),
                (
                    gap_rule_id,
                    ruleset_id,
                    "web_e2e_gap",
                    "Web 缺失页型规则",
                    "检查缺失视频详情页",
                    '["video_detail"]',
                    '{"required_artifacts":["screenshot"],'
                    '"target_clients":["WEB"]}',
                ),
            ),
        )
        yield _PostgresCase(
            connection=connection,
            pool=_TransactionScopedPool(connection),
            tenant_id=tenant_id,
            user_id=user_id,
            ruleset_id=ruleset_id,
            observed_rule_id=observed_rule_id,
            gap_rule_id=gap_rule_id,
            ruleset_code=f"web_v2_e2e_{ruleset_id}",
        )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.fixture
async def committed_postgres_case() -> _PostgresCase:
    """使用真实连接池提交每个阶段，并按随机身份精确清理。"""

    database_url = _require_formal_test_database_url()
    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=4, timeout=5, command_timeout=15)
    connection = await asyncpg.connect(database_url, timeout=5, command_timeout=15)
    tenant_id, user_id, ruleset_id, observed_rule_id, gap_rule_id = (
        str(uuid4()) for _ in range(5)
    )
    try:
        database_name = await connection.fetchval("SELECT current_database()")
        database_role = await connection.fetchval("SELECT current_user")
        assert (database_name, database_role) in {
            (_ISOLATED_DATABASE_NAME, "alioth_web_gap"),
            ("alioth_test_unbounded_menu", "alioth"),
        }
        ruleset_code = f"web_v2_e2e_{ruleset_id.replace('-', '')}"
        await connection.execute(
            """
            INSERT INTO qc_tenant (id, tenant_key, display_name)
            VALUES ($1::uuid, $2, 'Web V2 Committed E2E')
            """,
            tenant_id,
            f"web-v2-committed-e2e-{tenant_id}",
        )
        await connection.execute(
            """
            INSERT INTO qc_user (
                id, username, full_name, password_hash, role, tenant_id
            ) VALUES (
                $1::uuid, $2, 'Web V2 Committed E2E', 'not-a-login-secret',
                '平台管理员', $3::uuid
            )
            """,
            user_id,
            f"web-v2-committed-e2e-{user_id}",
            tenant_id,
        )
        await connection.execute(
            """
            INSERT INTO qc_ruleset (
                id, ruleset_code, name, version, status, snapshot_hash,
                rule_count, source_type
            ) VALUES (
                $1::uuid, $2, 'Web V2 Committed E2E', 1, 'enabled',
                'web-v2-e2e-snapshot', 8, 'manual'
            )
            """,
            ruleset_id,
            ruleset_code,
        )
        await connection.executemany(
            """
            INSERT INTO qc_rule (
                id, ruleset_id, rule_code, name, category, executor_type,
                severity, status, check_point, applicable_page_types,
                input_contract, manual_review_policy
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4, 'visual', 'hybrid',
                'medium', 'enabled', $5, $6::jsonb, $7::jsonb,
                '{"min_confidence":0.8}'::jsonb
            )
            """,
            (
                (
                    observed_rule_id,
                    ruleset_id,
                    "web_e2e_observed",
                    "Web 已采集页面规则",
                    "检查已采集页面",
                    '["content_list","content_detail"]',
                    '{"required_artifacts":["screenshot","action_trace"],'
                    '"target_clients":["WEB"]}',
                ),
                (
                    gap_rule_id,
                    ruleset_id,
                    "web_e2e_gap",
                    "Web 缺失页型规则",
                    "检查缺失视频详情页",
                    '["video_detail"]',
                    '{"required_artifacts":["screenshot"],'
                    '"target_clients":["WEB"]}',
                ),
            ),
        )
        await connection.executemany(
            """
            INSERT INTO qc_rule (
                id, ruleset_id, rule_code, name, category, executor_type,
                severity, status, check_point, applicable_page_types,
                input_contract, judge_config, manual_review_policy,
                url_pattern
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4, 'visual', 'rule_engine',
                'medium', 'enabled', $5, $6::jsonb, $7::jsonb, $8::jsonb,
                '{"min_confidence":0.8}'::jsonb, $9
            )
            """,
            (
                (
                    str(uuid4()),
                    ruleset_id,
                    "alioth_21",
                    "首页弹窗蒙层点击行为检查",
                    "检查弹窗关闭行为",
                    '["video_list"]',
                    '{"required_artifacts":["screenshot"],'
                    '"target_clients":["WEB","APP","H5"]}',
                    '{"engine_config":{"required_page_states":'
                    '["popup","modal","overlay","dialog"]}}',
                    None,
                ),
                (
                    str(uuid4()),
                    ruleset_id,
                    "alioth_8",
                    "登录态交互检查",
                    "检查登录态页面",
                    '["content_detail","video_detail","profile","post_list"]',
                    '{"required_artifacts":["screenshot"],'
                    '"target_clients":["WEB","APP","H5"],'
                    '"auth_requirement":"authenticated"}',
                    '{}',
                    None,
                ),
                (
                    str(uuid4()),
                    ruleset_id,
                    "web_e2e_other_origin",
                    "其他站点 URL 规则",
                    "仅检查其他站点",
                    '["content_list"]',
                    '{"required_artifacts":["screenshot"],'
                    '"target_clients":["WEB"]}',
                    '{}',
                    r"^https://different\.test/",
                ),
                (
                    str(uuid4()),
                    ruleset_id,
                    "web_e2e_legacy_target_missing",
                    "历史通用端规则",
                    "未声明 target_clients 时仍适用",
                    '["video_list"]',
                    '{"required_artifacts":["screenshot","action_trace"]}',
                    '{}',
                    None,
                ),
                (
                    str(uuid4()),
                    ruleset_id,
                    "web_e2e_legacy_target_empty",
                    "历史空端规则",
                    "target_clients 为空数组时仍适用",
                    '["video_list"]',
                    '{"required_artifacts":["screenshot","action_trace"],'
                    '"target_clients":[]}',
                    '{}',
                    None,
                ),
                (
                    str(uuid4()),
                    ruleset_id,
                    "web_e2e_legacy_gap",
                    "历史通用端缺页规则",
                    "缺失 target_clients 的规则仍进入 Coverage Gap",
                    '["video_list"]',
                    '{"required_artifacts":["screenshot"]}',
                    '{}',
                    None,
                ),
            ),
        )
        yield _PostgresCase(
            connection=connection,
            pool=pool,
            tenant_id=tenant_id,
            user_id=user_id,
            ruleset_id=ruleset_id,
            observed_rule_id=observed_rule_id,
            gap_rule_id=gap_rule_id,
            ruleset_code=ruleset_code,
        )
    finally:
        task_ids = await connection.fetchval(
            """
            SELECT COALESCE(array_agg(id), ARRAY[]::uuid[])
            FROM qc_task WHERE tenant_id = $1::uuid
            """,
            tenant_id,
        )
        async with connection.transaction():
            await connection.execute("SET LOCAL session_replication_role = replica")
            for table_name in (
                "qc_report_payload_snapshot",
                "qc_report_gate_binding",
                "qc_completion_gate_snapshot",
                "qc_review_policy_gate_decision",
                "qc_web_coverage_snapshot",
            ):
                if table_name == "qc_report_payload_snapshot":
                    await connection.execute(
                        """
                        DELETE FROM qc_report_payload_snapshot AS payload
                        USING qc_report_gate_binding AS binding
                        WHERE payload.report_id = binding.report_id
                          AND payload.report_version = binding.report_version
                          AND binding.task_id = ANY($1::uuid[])
                        """,
                        task_ids,
                    )
                else:
                    await connection.execute(
                        f"DELETE FROM {table_name} WHERE task_id = ANY($1::uuid[])",
                        task_ids,
                    )
            await connection.execute(
                """
                DELETE FROM qc_h5_inspection_publication_work_item AS link
                USING qc_h5_inspection_publication AS publication
                WHERE link.publication_id = publication.id
                  AND publication.task_id = ANY($1::uuid[])
                """,
                task_ids,
            )
            await connection.execute(
                """
                DELETE FROM qc_h5_inspection_publication_rule AS publication_rule
                USING qc_h5_inspection_publication AS publication
                WHERE publication_rule.publication_id = publication.id
                  AND publication.task_id = ANY($1::uuid[])
                """,
                task_ids,
            )
            await connection.execute(
                """
                DELETE FROM qc_web_menu_ledger_entry AS ledger
                USING qc_web_exploration_budget AS budget
                WHERE ledger.agent_run_id = budget.agent_run_id
                  AND ledger.site_id = budget.site_id
                  AND budget.task_id = ANY($1::uuid[])
                """,
                task_ids,
            )
            await connection.execute(
                "DELETE FROM qc_event_inbox WHERE task_id = ANY($1::uuid[])",
                task_ids,
            )
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id = ANY($1::uuid[])",
                task_ids,
            )
            await connection.execute("SET LOCAL session_replication_role = origin")
            await connection.execute(
                "DELETE FROM qc_task WHERE id = ANY($1::uuid[])", task_ids
            )
            await connection.execute(
                "DELETE FROM qc_rule WHERE ruleset_id = $1::uuid", ruleset_id
            )
            await connection.execute(
                "DELETE FROM qc_ruleset WHERE id = $1::uuid", ruleset_id
            )
            await connection.execute(
                "DELETE FROM qc_user WHERE id = $1::uuid", user_id
            )
            await connection.execute(
                "DELETE FROM qc_tenant WHERE id = $1::uuid", tenant_id
            )
        await connection.close()
        await pool.close()


def _web_request(case: _PostgresCase) -> dict[str, object]:
    return {
        "task_name": "Web V2 正式闭环",
        "target": {
            "platform": "web",
            "url": "https://example.test",
            "exploration_target": {
                "required_page_types": [
                    "content_list",
                    "content_detail",
                    "video_list",
                    "video_detail",
                ]
            },
        },
        "ruleset_code": case.ruleset_code,
        "ruleset_version": 1,
        "channel": "api",
        "priority": 80,
        "product": {
            "department_name": "质量保障部",
            "product_code": "WEB-E2E",
            "product_name": "Web V2 E2E",
        },
        "scan_mode": "full",
        "journey_steps": ["首页", "列表", "详情"],
        "remark": "真实 PostgreSQL 闭环验收",
    }


async def _post_task(
    case: _PostgresCase,
    *,
    endpoint: str = "/api/v2/tasks",
) -> httpx.Response:
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    default_queue = RedisAgentRunQueue(
        fake_redis,
        ttl_seconds=3600,
        queue_key="test:web-v2-e2e:default",
    )
    web_queue = RedisAgentRunQueue(
        fake_redis,
        ttl_seconds=3600,
        queue_key="test:web-v2-e2e:web",
    )
    submission = TaskSubmissionService(
        task_repository=PostgresTaskRepository(pool=case.pool),
        agent_run_queue=default_queue,
        web_agent_run_queue=web_queue,
        state_store=PostgresAgentRunStateStore(pool=case.pool),
        default_workflow_version="v2",
    )
    app = create_app()
    dependency = (
        v2_tasks_module._get_full_task_entry
        if endpoint == "/api/v2/tasks"
        else v2_tasks_module._get_exploration_task_entry
    )
    entry_type = (
        TaskSubmissionV2EntryService
        if endpoint == "/api/v2/tasks"
        else ExplorationTaskV2EntryService
    )
    app.dependency_overrides[dependency] = lambda: entry_type(
        submission,
        web_v2_mode="on",
    )
    app.dependency_overrides[v2_tasks_module.require_access_scope] = (
        lambda: case.access_scope
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return await client.post(
            endpoint,
            headers={"Idempotency-Key": f"web-v2-e2e-{uuid4()}"},
            json=_web_request(case),
        )


class _FailingWakeup:
    """Publication 已落库后模拟 Redis 唤醒丢失。"""

    async def wake(self, **_kwargs: object) -> None:
        raise ConnectionError("injected redis wakeup loss")


class _RecordingWakeupTransport:
    """记录生产 Outbox Relay 实际领取的持久化信封。"""

    def __init__(self) -> None:
        self.deliveries: list[tuple[OutboxLease, WakeupTransportReceipt]] = []

    async def publish(self, lease: OutboxLease) -> WakeupTransportReceipt:
        receipt = WakeupTransportReceipt(
            stream_name=f"test:web-v2-e2e:{lease.envelope.message_type}",
            stream_entry_id=f"{len(self.deliveries) + 1}-0",
        )
        self.deliveries.append((lease, receipt))
        return receipt


class _TaskScopedOutboxRepository:
    """只收窄测试领取范围，其余回执写回委托生产仓储。"""

    def __init__(self, *, pool: asyncpg.Pool, task_id: str) -> None:
        self._pool = pool
        self._task_id = task_id
        self._delegate = PostgresOutboxRepository(pool=pool)

    async def claim(
        self,
        *,
        lease_owner: str,
        lease_token: str,
        lease_seconds: int = 60,
    ) -> OutboxLease | None:
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                WITH candidate AS (
                    SELECT message_id
                    FROM qc_outbox_event
                    WHERE task_id = $1::uuid
                      AND attempt_count < max_attempts
                      AND message_type = ANY($5::varchar[])
                      AND (
                        (
                            status IN ('pending', 'retry_wait')
                            AND next_attempt_at <= now()
                        )
                        OR (
                            status = 'publishing'
                            AND lease_expires_at <= now()
                        )
                      )
                    ORDER BY next_attempt_at, occurred_at, message_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE qc_outbox_event AS event
                SET status = 'publishing',
                    attempt_count = event.attempt_count + 1,
                    lease_owner = $2,
                    lease_token = $3::uuid,
                    lease_expires_at = now() + make_interval(secs => $4),
                    updated_at = now()
                FROM candidate
                WHERE event.message_id = candidate.message_id
                RETURNING event.aggregate_type, event.aggregate_id,
                          event.aggregate_version, event.envelope_payload,
                          event.lease_token::text AS lease_token,
                          event.attempt_count
                """,
                self._task_id,
                lease_owner,
                lease_token,
                lease_seconds,
                list(SUPPORTED_WAKEUP_MESSAGE_TYPES),
            )
        if row is None:
            return None
        raw_envelope = row["envelope_payload"]
        if isinstance(raw_envelope, str):
            raw_envelope = json.loads(raw_envelope)
        return OutboxLease(
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=str(row["aggregate_id"]),
            aggregate_version=int(row["aggregate_version"]),
            envelope=ProtocolEnvelope.from_dict(raw_envelope),
            lease_token=str(row["lease_token"]),
            attempt_count=int(row["attempt_count"]),
        )

    async def mark_published(self, **kwargs: object) -> bool:
        return await self._delegate.mark_published(**kwargs)

    async def reschedule(self, **kwargs: object) -> bool:
        return await self._delegate.reschedule(**kwargs)


class _RaiseAfterCommittedSnapshot:
    """快照事务真实提交后、第一条 Gap 写入前模拟进程退出。"""

    def __init__(self, delegate: PostgresWebCoverageSnapshotRepository) -> None:
        self._delegate = delegate
        self.committed_snapshot: WebCoverageSnapshot | None = None
        self.raised = False

    async def create(self, snapshot: WebCoverageSnapshot) -> WebCoverageSnapshot:
        committed = await self._delegate.create(snapshot)
        self.committed_snapshot = committed
        if not self.raised:
            self.raised = True
            raise RuntimeError("injected process exit after committed snapshot")
        return committed


class _RaiseAfterCommittedRuleExecution:
    """规则结果提交后模拟进程退出，验证下一实例幂等重放。"""

    def __init__(self, delegate: RuleExecutionService) -> None:
        self._delegate = delegate
        self.raised = False

    async def record_rule_execution(
        self, command: RecordRuleExecutionCommand
    ) -> object:
        result = await self._delegate.record_rule_execution(command)
        if not self.raised:
            self.raised = True
            raise RuntimeError("injected process exit after committed rule execution")
        return result


class _FailOnceAtReportBindingConnection:
    """委托真实连接，仅在 Gate 事务写报告绑定时退出。"""

    def __init__(
        self,
        connection: asyncpg.Connection,
        owner: _FailOnceAtReportBindingPool,
    ) -> None:
        self._connection = connection
        self._owner = owner

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)

    async def fetchrow(self, query: str, *args: object) -> object:
        if (
            not self._owner.failed
            and "INSERT INTO qc_report_gate_binding" in query
        ):
            self._owner.failed = True
            raise RuntimeError("injected process exit at report binding")
        return await self._connection.fetchrow(query, *args)


class _FailOnceAtReportBindingPool:
    """使用真实连接池，在一个原子 Gate/Report 事务中注入一次退出。"""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self.failed = False

    @asynccontextmanager
    async def acquire(self):
        async with self._pool.acquire() as connection:
            yield _FailOnceAtReportBindingConnection(connection, self)


class _NoopAnnotator:
    async def annotate_inspection(self, **_kwargs: object) -> None:
        return None


class _StaticSnapshotProvider:
    def __init__(self, case: _PostgresCase) -> None:
        self._case = case

    async def get_task_snapshot(self, task_id: str) -> TaskRuntimeSnapshot:
        return TaskRuntimeSnapshot(
            task_id=task_id,
            status="running",
            ruleset_version=1,
            ruleset_id=self._case.ruleset_id,
            ruleset_snapshot_hash="web-v2-e2e-snapshot",
            target_platform="web",
        )


class _RecordingReportGenerator:
    """记录生产报告服务调用，不替代任何规则或数据库投影。"""

    def __init__(self, delegate: ReportGenerationService) -> None:
        self._delegate = delegate
        self.calls = 0
        self.reports: list[dict] = []

    async def generate_task_report(self, task_id: str, **kwargs: object) -> dict:
        self.calls += 1
        report = await self._delegate.generate_task_report(task_id, **kwargs)
        self.reports.append(report)
        return report


class _FailOnceReportGenerator:
    """首次报告生成边界真实抛错，不进入 Gate 事务。"""

    def __init__(self, delegate: ReportGenerationService) -> None:
        self._delegate = delegate
        self.calls = 0

    async def generate_task_report(self, task_id: str, **kwargs: object) -> dict:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("injected first report generation failure")
        return await self._delegate.generate_task_report(task_id, **kwargs)


class _DeterministicInspectionConsumer:
    """用真实 Work Item 状态机写入确定性正式结果。"""

    def __init__(
        self,
        *,
        case: _PostgresCase,
        task_id: str,
        run_id: str,
        service: InspectionWorkItemService,
    ) -> None:
        self._case = case
        self._task_id = task_id
        self._run_id = run_id
        self._service = service
        self.completed_count = 0

    async def drain_available(self, **kwargs: object) -> None:
        assert kwargs["allow_terminal_web_owner_recovery"] is True
        while True:
            lease = await self._service.claim(
                ClaimInspectionWorkItemCommand(
                    task_id=self._task_id,
                    consumer_agent_run_id=self._run_id,
                    lease_owner="web-v2-e2e-active",
                    lease_seconds=180,
                    session_dependency="active_session",
                    allow_terminal_web_owner_recovery=True,
                )
            )
            if lease is None:
                return
            row_version = await self._service.transition(
                TransitionInspectionWorkItemCommand(
                    work_item_id=lease.work_item_id,
                    expected_status=InspectionWorkItemStatus.LEASED,
                    target_status=InspectionWorkItemStatus.RUNNING,
                    row_version=lease.row_version,
                    lease_token=lease.lease_token,
                )
            )
            row_version = await self._service.renew_lease(
                RenewInspectionWorkItemLeaseCommand(
                    work_item_id=lease.work_item_id,
                    consumer_agent_run_id=self._run_id,
                    lease_token=lease.lease_token,
                    row_version=row_version,
                    lease_seconds=180,
                    allow_terminal_web_owner_recovery=True,
                )
            )
            await _complete_work_item(
                pool=self._case.pool,
                service=self._service,
                lease=lease,
                row_version=row_version,
                run_id=self._run_id,
            )
            self.completed_count += 1


async def _complete_work_item(
    *,
    pool: Any,
    service: InspectionWorkItemService,
    lease: InspectionWorkItemLease,
    row_version: int,
    run_id: str,
) -> None:
    attempt_id = await service.start_attempt(
        StartInspectionAttemptCommand(
            work_item_id=lease.work_item_id,
            consumer_agent_run_id=run_id,
            attempt_no=lease.attempt_no,
            lease_token=lease.lease_token,
            input_hash=lease.input_hash,
        )
    )
    links: list[tuple[object, str, str, list[str]]] = []
    rule_execution_service = RuleExecutionService(
        repository=PostgresTaskRepository(pool=pool)
    )
    for rule in lease.rules:
        async with pool.acquire() as connection:
            evidence_artifact_ids = list(
                await connection.fetchval(
                    """
                    SELECT array_agg(id::text ORDER BY artifact_type, id)
                    FROM qc_evidence_artifact
                    WHERE task_id = $1::uuid
                      AND page_capture_id = $2::uuid
                      AND artifact_type IN ('screenshot', 'action_trace')
                    """,
                    lease.task_id,
                    lease.page_capture_id,
                )
                or []
            )
            assert evidence_artifact_ids
            packet_id = str(
                await connection.fetchval(
                """
                INSERT INTO qc_observation_packet (
                    task_id, page_capture_id, rule_id, quality_gate_status,
                    created_by_agent_run_id, evidence_artifact_ids
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, 'pass', $4::uuid, $5::jsonb
                )
                RETURNING id
                """,
                lease.task_id,
                lease.page_capture_id,
                rule.rule_id,
                run_id,
                json.dumps(evidence_artifact_ids),
                )
            )
        recorded = await rule_execution_service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id=lease.task_id,
                agent_run_id=run_id,
                rule_id=rule.rule_id,
                page_capture_id=lease.page_capture_id,
                observation_packet_id=packet_id,
                execution_status="pass",
                result_summary="页面检查通过",
                evidence_artifact_ids=evidence_artifact_ids,
                confidence=1.0,
                judge_input={
                    "schema_version": "web-v2-e2e-observation.v1",
                    "observation_packet_id": packet_id,
                    "page_capture_id": lease.page_capture_id,
                    "evidence_artifact_ids": evidence_artifact_ids,
                    "observation_summary": "固定证据附件已通过结构化检查",
                },
                judge_output={
                    "schema_version": "1.0",
                    "conclusion": "pass",
                    "reasoning_summary": "固定证据与规则要求一致",
                    "tool_results": [
                        {
                            "tool_name": "deterministic_evidence_contract_check",
                            "status": "ok",
                            "observation_packet_id": packet_id,
                            "evidence_artifact_ids": evidence_artifact_ids,
                        }
                    ],
                },
                work_item_id=lease.work_item_id,
                attempt_id=attempt_id,
                lease_token=lease.lease_token,
            )
        )
        execution_id = recorded.execution_id
        await service.transition_rule(
            TransitionInspectionWorkItemRuleCommand(
                work_item_id=lease.work_item_id,
                rule_id=rule.rule_id,
                expected_status=InspectionWorkItemRuleStatus.PENDING,
                target_status=InspectionWorkItemRuleStatus.JUDGED,
                lease_token=lease.lease_token,
                observation_packet_id=packet_id,
            )
        )
        links.append((rule, packet_id, execution_id, evidence_artifact_ids))
    await service.transition_attempt(
        TransitionInspectionAttemptCommand(
            attempt_id=attempt_id,
            expected_status=InspectionAttemptStatus.RUNNING,
            target_status=InspectionAttemptStatus.JUDGED,
            lease_token=lease.lease_token,
            result_payload={
                "items": [
                    {
                        "rule_code": rule.rule_code,
                        "execution_status": "pass",
                        "evidence_artifact_ids": evidence_ids,
                        "observation_packet_id": packet_id,
                        "rule_execution_id": execution_id,
                    }
                    for rule, packet_id, execution_id, evidence_ids in links
                ]
            },
        )
    )
    for rule, packet_id, execution_id, _ in links:
        await service.transition_rule(
            TransitionInspectionWorkItemRuleCommand(
                work_item_id=lease.work_item_id,
                rule_id=rule.rule_id,
                expected_status=InspectionWorkItemRuleStatus.JUDGED,
                target_status=InspectionWorkItemRuleStatus.WRITTEN,
                lease_token=lease.lease_token,
                observation_packet_id=packet_id,
                rule_execution_id=execution_id,
            )
        )
    await service.transition_attempt(
        TransitionInspectionAttemptCommand(
            attempt_id=attempt_id,
            expected_status=InspectionAttemptStatus.JUDGED,
            target_status=InspectionAttemptStatus.WRITTEN,
            lease_token=lease.lease_token,
        )
    )
    await service.transition(
        TransitionInspectionWorkItemCommand(
            work_item_id=lease.work_item_id,
            expected_status=InspectionWorkItemStatus.RUNNING,
            target_status=InspectionWorkItemStatus.SUCCEEDED,
            row_version=row_version,
            lease_token=lease.lease_token,
        )
    )


async def _seed_ready_capture(
    case: _PostgresCase,
    *,
    task_id: str,
    run_id: str,
    session_id: str,
    page_seq: int,
    raw_page_type: str,
    canonical_page_type: str,
    canonical_url: str,
    content_version: int,
) -> str:
    from dataclasses import replace

    from services.api.app.application.web_readiness_contracts import (
        WebEvidenceRole,
        assess_web_readiness,
    )
    from services.api.tests.test_web_page_type_assessment import _capture as assessment_capture

    # 固定采样信号属于测试替身；保留缺采样的旧观察帧为 unknown 的独立负例。
    readiness = assess_web_readiness(replace(assessment_capture().inspection_readiness.signals,
        identity_before=canonical_url, identity_after=canonical_url), role=WebEvidenceRole.INSPECTION)
    before_capture_id = str(uuid4())
    capture_id = str(uuid4())
    action_id = f"web-e2e:{task_id}:{page_seq}"
    async with case.connection.transaction():
        await case.connection.execute("SET CONSTRAINTS ALL DEFERRED")
        await case.connection.execute(
            """
            INSERT INTO qc_action_execution (
                action_execution_id, task_id, session_id, intent_id,
                idempotency_key, intent_hash, intent_protocol_version,
                intent_payload, command_sequence, finalization_epoch,
                requested_source_capture_id, source_capture_id, result_capture_id,
                status, result_payload, started_at, action_finished_at,
                evidence_finished_at
            ) VALUES (
                $1, $2::uuid, $3, $4, $4, $5, 'web-v2-workflow/v1',
                '{}'::jsonb, $6, 0,
                $7::uuid, $7::uuid, $8::uuid,
                'completed', '{}'::jsonb, now(), now(), now()
            )
            """,
            action_id,
            task_id,
            session_id,
            f"web-e2e-intent-{page_seq}",
            f"{page_seq:x}".rjust(64, "0"),
            page_seq,
            before_capture_id,
            capture_id,
        )
        await case.connection.execute(
            """
            INSERT INTO qc_page_capture (
                id, task_id, agent_run_id, parent_page_capture_id,
                page_seq, page_slug, url, page_type, page_state,
                capture_status, captured_at, detection_status,
                inspection_status, surface_session_id, capture_role,
                action_execution_id, session_command_sequence, metadata
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, NULL,
                (($4::bigint * 2) - 1)::integer, $5, $6, $7, 'stable',
                'captured', now(), 'recognized',
                'completed', $8, 'before_action', $9, $4::bigint,
                '{"finalization_epoch":0}'::jsonb
            )
            """,
            before_capture_id,
            task_id,
            run_id,
            page_seq,
            f"{canonical_page_type}-before",
            f"{canonical_url}&capture=before-{page_seq}",
            canonical_page_type,
            session_id,
            action_id,
        )
        await case.connection.execute(
            """
            INSERT INTO qc_page_capture (
                id, task_id, agent_run_id, parent_page_capture_id,
                page_seq, page_slug, url, page_type, page_state,
                capture_status, captured_at, detection_status,
                inspection_status, surface_session_id, capture_role,
                action_execution_id, session_command_sequence, metadata
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                ($5::bigint * 2)::integer, $6, $7, $8, 'loaded',
                'captured', now(), NULL,
                'evaluating', $9, 'after_action', $10, $5::bigint,
                '{"finalization_epoch":0}'::jsonb
            )
            """,
            capture_id,
            task_id,
            run_id,
            before_capture_id,
            page_seq,
            canonical_page_type,
            canonical_url,
            raw_page_type,
            session_id,
            action_id,
        )
    for related_capture_id, artifact_types in (
        (before_capture_id, ("screenshot", "dom")),
        (capture_id, ("screenshot", "dom", "visible_text", "action_trace")),
    ):
        for artifact_type in artifact_types:
            await case.connection.execute(
                """
                INSERT INTO qc_evidence_artifact (
                    task_id, page_capture_id, artifact_type, source,
                    storage_path, content_type, checksum, size_bytes
                ) VALUES (
                    $1::uuid, $2::uuid, $3, 'baseline', $4, $5, $6, 1
                )
                """,
                task_id,
                related_capture_id,
                artifact_type,
                f"tests/web-v2-e2e/{related_capture_id}/{artifact_type}",
                (
                    "image/png"
                    if artifact_type == "screenshot"
                    else "application/json"
                    if artifact_type == "action_trace"
                    else "text/plain"
                ),
                f"sha256:{(
                    content_version
                    if related_capture_id == capture_id
                    and artifact_type in {'dom', 'visible_text'}
                    else page_seq
                ):x}".ljust(71, "f"),
            )
        await case.connection.execute(
            """
            INSERT INTO qc_evidence_manifest (
                manifest_id, manifest_version, task_id, session_id,
                capture_id, protocol_version, manifest_hash, payload_hash,
                quality_status, manifest_payload, created_at
            ) VALUES (
                $1, 1, $2::uuid, $3, $4::uuid,
                'evidence-manifest.v1', $5, $6, 'pass', '{}'::jsonb, now()
            )
            """,
            f"web-e2e-manifest:{related_capture_id}",
            task_id,
            session_id,
            related_capture_id,
            f"sha256:{'1' * 64}",
            f"sha256:{'2' * 64}",
        )
    artifact_rows = await case.connection.fetch(
        """
        SELECT id::text AS artifact_id, artifact_type
        FROM qc_evidence_artifact
        WHERE task_id = $1::uuid AND page_capture_id = $2::uuid
        """,
        task_id,
        capture_id,
    )
    artifact_ids = {
        str(row["artifact_type"]): str(row["artifact_id"])
        for row in artifact_rows
    }
    captured_at = await case.connection.fetchval(
        "SELECT captured_at FROM qc_page_capture WHERE id = $1::uuid",
        capture_id,
    )
    assert isinstance(captured_at, datetime)
    initial_detection = await case.connection.fetchrow(
        """
        SELECT page_state, detection_status, detection_confidence
        FROM qc_page_capture WHERE id = $1::uuid
        """,
        capture_id,
    )
    assert initial_detection is not None
    assert dict(initial_detection) == {
        "page_state": "loaded",
        "detection_status": None,
        "detection_confidence": None,
    }
    page_role = (
        "home"
        if page_seq == 1
        else "menu"
        if canonical_page_type.endswith("list")
        else "detail"
    )
    recorder = WebPageTypeAssessmentRecorder(
        repository=PostgresWebCaptureEvidenceRepository(pool=case.pool)
    )
    assessment_result = await recorder.persist(
        task_id=task_id,
        agent_run_id=run_id,
        session_id=session_id,
        capture=WebReadyPageCapture(
            capture_id=capture_id,
            web_capture_stage="web_page_ready_baseline",
            screenshot_artifact_id=artifact_ids["screenshot"],
            dom_artifact_id=artifact_ids["dom"],
            visible_text_artifact_id=artifact_ids["visible_text"],
            url=canonical_url,
            page_role=page_role,
            structure_facts=WebPageStructureFacts(
                navigation_element_count=1,
                repeated_link_count=1 if page_role == "menu" else 0,
                video_element_count=(
                    1 if canonical_page_type == "video_detail" else 0
                ),
                single_subject_element_count=1 if page_role == "detail" else 0,
                dom_tokens=("deterministic", canonical_page_type),
            ),
            captured_at=captured_at.astimezone(UTC),
            inspection_readiness=readiness,
        ),
        assessment=WebPageAssessment(
            page_type=raw_page_type,
            raw_page_type=raw_page_type,
            confidence=1.0,
            evidence=("固定 DOM 结构事实",),
        ),
    )
    assert assessment_result.classification_status == "classified"
    assert assessment_result.final_page_type == canonical_page_type
    assert assessment_result.assessment_record_id is not None
    formal_detection = await case.connection.fetchrow(
        """
        SELECT page_state, detection_status,
               detection_confidence::float AS detection_confidence
        FROM qc_page_capture WHERE id = $1::uuid
        """,
        capture_id,
    )
    assert formal_detection is not None
    assert dict(formal_detection) == {
        "page_state": "stable",
        "detection_status": "recognized",
        "detection_confidence": 1.0,
    }
    return capture_id


async def test_minimal_formal_web_request_persists_authoritative_task_and_run_identity(
    postgres_case: _PostgresCase,
) -> None:
    """若 Task 未冻结 Web target，通知 SQL 会屏蔽 API 创建的真实 Capture。"""

    response = await _post_task(postgres_case)

    assert response.status_code == 202, response.text
    body = response.json()
    persisted = await postgres_case.connection.fetchrow(
        """
        SELECT
            task.id::text AS task_id,
            run.id::text AS run_id,
            task.target_platform,
            task.task_config->>'execution_mode' AS execution_mode,
            task.task_config->>'web_inspection_notification_mode' AS notification_mode,
            task.task_config #>> '{_task_target,platform}' AS locked_platform,
            task.finalization_epoch
        FROM qc_task AS task
        INNER JOIN qc_agent_run AS run
          ON run.task_id = task.id
         AND run.agent_type = 'main'
        WHERE task.id = $1::uuid
        """,
        body["task_id"],
    )

    assert persisted is not None
    assert dict(persisted) == {
        "task_id": body["task_id"],
        "run_id": body["run_id"],
        "target_platform": "web",
        "execution_mode": "full_inspection",
        "notification_mode": "enforced",
        "locked_platform": "web",
        "finalization_epoch": 0,
    }


async def test_reconciliation_review_terminal_is_authoritative_and_not_recoverable(
    postgres_case: _PostgresCase,
) -> None:
    """Publication/分类人工复核必须原子封存 Task，而不是只改 Redis。"""

    response = await _post_task(postgres_case)
    assert response.status_code == 202, response.text
    identity = response.json()
    task_id = identity["task_id"]
    run_id = identity["run_id"]
    await postgres_case.connection.execute(
        """
        UPDATE qc_agent_run
        SET status = 'done', stop_reason = 'completed', finished_at = now()
        WHERE id = $1::uuid
        """,
        run_id,
    )
    await postgres_case.connection.execute(
        """
        UPDATE qc_task
        SET status = 'running',
            finalization_state = 'session_draining',
            exploration_draining_at = now(),
            session_draining_at = now(),
            finalization_agent_run_id = $2::uuid,
            finalization_row_version = 2
        WHERE id = $1::uuid
        """,
        task_id,
        run_id,
    )
    repository = PostgresTaskFinalizationRepository(pool=postgres_case.pool)

    terminal = await repository.terminate(
        command=TerminateTaskFinalizationCommand(
            task_id=task_id,
            agent_run_id=run_id,
            expected_finalization_epoch=0,
            expected_row_version=2,
            execution_status=TaskExecutionStatus.NEEDS_REVIEW,
            reason_code="WEB_INSPECTION_PUBLICATION_NEEDS_REVIEW",
        )
    )
    recoverable = await repository.list_recoverable_finalizations(limit=100)
    task = await postgres_case.connection.fetchrow(
        """
        SELECT status, finalization_state, execution_status, quality_outcome,
               completion_gate_snapshot_id, finished_at
        FROM qc_task WHERE id = $1::uuid
        """,
        task_id,
    )

    assert terminal.state.value == "sealed"
    assert dict(task) == {
        "status": "review",
        "finalization_state": "sealed",
        "execution_status": "needs_review",
        "quality_outcome": "unknown",
        "completion_gate_snapshot_id": terminal.gate_snapshot_id,
        "finished_at": task["finished_at"],
    }
    assert task["finished_at"] is not None
    assert all(item.task_id != task_id for item in recoverable)


async def test_exploration_only_web_request_has_no_formal_side_effects(
    postgres_case: _PostgresCase,
) -> None:
    """仅探索入口即使已持久化 Task/Run，也不得创建正式链路事实。"""

    response = await _post_task(
        postgres_case,
        endpoint="/api/v2/exploration-tasks",
    )

    assert response.status_code == 202, response.text
    body = response.json()
    facts = await postgres_case.connection.fetchrow(
        """
        SELECT
            task.task_config->>'execution_mode' AS execution_mode,
            task.task_config->>'web_inspection_notification_mode'
                AS notification_mode,
            (
                SELECT count(*) FROM qc_h5_inspection_publication AS publication
                WHERE publication.task_id = task.id
            )::integer AS publication_count,
            (
                SELECT count(*)
                FROM qc_h5_inspection_publication_rule AS publication_rule
                INNER JOIN qc_h5_inspection_publication AS publication
                  ON publication.id = publication_rule.publication_id
                WHERE publication.task_id = task.id
            )::integer AS publication_rule_count,
            (
                SELECT count(*)
                FROM qc_h5_inspection_publication_work_item AS publication_work
                INNER JOIN qc_h5_inspection_publication AS publication
                  ON publication.id = publication_work.publication_id
                WHERE publication.task_id = task.id
            )::integer AS publication_work_link_count,
            (
                SELECT count(*) FROM qc_inspection_work_item AS work_item
                WHERE work_item.task_id = task.id
            )::integer AS work_item_count,
            (
                SELECT count(*) FROM qc_rule_execution AS execution
                WHERE execution.task_id = task.id
            )::integer AS rule_execution_count,
            (
                SELECT count(*) FROM qc_web_coverage_snapshot AS snapshot
                WHERE snapshot.task_id = task.id
            )::integer AS snapshot_count,
            (
                SELECT count(*) FROM qc_report_gate_binding AS binding
                WHERE binding.task_id = task.id
            )::integer AS report_binding_count,
            (
                SELECT count(*) FROM qc_completion_gate_snapshot AS gate
                WHERE gate.task_id = task.id
            )::integer AS gate_count,
            (
                SELECT count(*) FROM qc_review_policy_gate_decision AS decision
                WHERE decision.task_id = task.id
            )::integer AS review_gate_count,
            (
                SELECT count(*) FROM qc_outbox_event AS event
                WHERE event.task_id = task.id
                  AND event.message_type <> $2
            )::integer AS formal_outbox_count,
            (
                SELECT count(*) FROM qc_outbox_event AS event
                WHERE event.task_id = task.id
                  AND event.message_type = $2
            )::integer AS submission_outbox_count,
            (
                SELECT count(*)
                FROM qc_report_payload_snapshot AS payload
                INNER JOIN qc_report_gate_binding AS binding
                  ON binding.report_id = payload.report_id
                 AND binding.report_version = payload.report_version
                WHERE binding.task_id = task.id
            )::integer AS report_payload_count
        FROM qc_task AS task
        WHERE task.id = $1::uuid
        """,
        body["task_id"],
        TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE,
    )

    assert facts is not None
    assert dict(facts) == {
        "execution_mode": "exploration_only",
        "notification_mode": "off",
        "publication_count": 0,
        "publication_rule_count": 0,
        "publication_work_link_count": 0,
        "work_item_count": 0,
        "rule_execution_count": 0,
        "snapshot_count": 0,
        "report_binding_count": 0,
        "gate_count": 0,
        "review_gate_count": 0,
        "formal_outbox_count": 0,
        "submission_outbox_count": 1,
        "report_payload_count": 0,
    }
    submission_outbox = await postgres_case.connection.fetchrow(
        """
        SELECT aggregate_type, aggregate_id, message_type, envelope_payload
        FROM qc_outbox_event
        WHERE task_id = $1::uuid AND message_type = $2
        """,
        body["task_id"],
        TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE,
    )
    assert submission_outbox is not None
    assert submission_outbox["aggregate_type"] == "task_submission"
    submission_envelope = json.loads(submission_outbox["envelope_payload"])
    assert submission_envelope["message_type"] == TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE
    assert submission_envelope["aggregate_id"] == submission_outbox["aggregate_id"]
    assert submission_envelope["payload"]["task_id"] == body["task_id"]
    assert submission_envelope["payload"]["run_id"] == body["run_id"]


async def test_exploration_only_real_capture_records_type_without_formal_side_effects(
    postgres_case: _PostgresCase,
) -> None:
    """仅探索任务不冻结正式 target，也必须把真实 Capture 桥接为权威页型。"""

    response = await _post_task(
        postgres_case,
        endpoint="/api/v2/exploration-tasks",
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    run_id = response.json()["run_id"]
    session_id = f"web:{run_id}"
    lease_token = str(uuid4())
    await postgres_case.connection.execute(
        """
        UPDATE qc_task
        SET status = 'running', started_at = now(),
            finalization_state = 'open', finalization_agent_run_id = $2::uuid
        WHERE id = $1::uuid
        """,
        task_id,
        run_id,
    )
    await postgres_case.connection.execute(
        """
        UPDATE qc_agent_run
        SET status = 'running', started_at = now()
        WHERE id = $1::uuid
        """,
        run_id,
    )
    await postgres_case.connection.execute(
        """
        INSERT INTO qc_surface_session (
            session_id, task_id, surface_kind, session_owner, status,
            command_sequence, finalization_epoch, lease_owner, lease_token,
            lease_expires_at, activated_at
        ) VALUES (
            $1, $2::uuid, 'web', 'web-v2-workflow', 'active',
            1, 0, 'web-v2-bridge-test', $3::uuid,
            now() + interval '5 minutes', now()
        )
        """,
        session_id,
        task_id,
        lease_token,
    )
    persisted = await PageCapturePersistenceService(
        repository=PostgresPageCaptureRepository(pool=postgres_case.pool),
        artifact_store=_BridgeArtifactStore(),
    ).persist(
        task_id=task_id,
        agent_run_id=run_id,
        result=PageCaptureResult(
            execution_status="success",
            mode="observe",
            reachable=True,
            final_url="https://example.test/content",
            title="内容列表",
            status_code=200,
            artifacts=[
                PageCaptureArtifact("screenshot", None, b"png"),
                PageCaptureArtifact("dom", None, "<main>内容列表</main>"),
                PageCaptureArtifact("visible_text", None, "内容列表"),
            ],
        ),
        capture_role="observation",
        surface_session_id=session_id,
        session_command_sequence=1,
        finalization_epoch=0,
    )
    initial = await postgres_case.connection.fetchrow(
        """
        SELECT page_type, page_state, detection_status, detection_confidence
        FROM qc_page_capture
        WHERE task_id = $1::uuid AND id = $2::uuid
        """,
        task_id,
        persisted.page_capture_id,
    )
    assert initial is not None
    assert dict(initial) == {
        "page_type": None,
        "page_state": "loaded",
        "detection_status": None,
        "detection_confidence": None,
    }
    artifacts = {item.artifact_type: item.artifact_id for item in persisted.artifacts}
    captured_at = await postgres_case.connection.fetchval(
        "SELECT captured_at FROM qc_page_capture WHERE id = $1::uuid",
        persisted.page_capture_id,
    )
    assert isinstance(captured_at, datetime)

    assessment_result = await WebPageTypeAssessmentRecorder(
        repository=PostgresWebCaptureEvidenceRepository(pool=postgres_case.pool)
    ).persist(
        task_id=task_id,
        agent_run_id=run_id,
        session_id=session_id,
        capture=WebReadyPageCapture(
            capture_id=persisted.page_capture_id,
            web_capture_stage="web_page_ready_baseline",
            screenshot_artifact_id=artifacts["screenshot"],
            dom_artifact_id=artifacts["dom"],
            visible_text_artifact_id=artifacts["visible_text"],
            page_role="menu",
            structure_facts=WebPageStructureFacts(
                navigation_element_count=1,
                repeated_link_count=3,
                video_element_count=0,
                single_subject_element_count=0,
                dom_tokens=("content-list",),
            ),
            captured_at=captured_at.astimezone(UTC),
        ),
        assessment=WebPageAssessment(
            page_type="content_list",
            raw_page_type="content_list",
            confidence=0.93,
            evidence=("固定内容列表结构",),
        ),
    )

    assert assessment_result.classification_status == "classified"
    assert assessment_result.final_page_type == "content_list"
    facts = await postgres_case.connection.fetchrow(
        """
        SELECT
            task.task_config ? '_task_target' AS has_locked_target,
            page.page_type,
            page.page_state,
            page.detection_status,
            page.detection_confidence::float AS detection_confidence,
            page.metadata->'page_detection'->>'detection_status'
                AS metadata_detection_status,
            page.metadata->'page_detection'->>'page_state'
                AS metadata_page_state,
            (page.metadata->'page_detection'->>'confidence')::float
                AS metadata_confidence,
            assessment.detection_status AS assessment_detection_status,
            assessment.page_type AS assessment_page_type,
            assessment.page_state AS assessment_page_state,
            assessment.status AS assessment_status,
            (SELECT count(*) FROM qc_h5_inspection_publication AS publication
             WHERE publication.task_id = task.id)::integer AS publication_count,
            (SELECT count(*) FROM qc_inspection_work_item AS work_item
             WHERE work_item.task_id = task.id)::integer AS work_item_count,
            (SELECT count(*) FROM qc_rule_execution AS execution
             WHERE execution.task_id = task.id)::integer AS rule_execution_count,
            (SELECT count(*) FROM qc_report_gate_binding AS binding
             WHERE binding.task_id = task.id)::integer AS report_count
        FROM qc_task AS task
        INNER JOIN qc_page_capture AS page
          ON page.task_id = task.id AND page.id = $2::uuid
        INNER JOIN qc_h5_page_type_assessment AS assessment
          ON assessment.task_id = task.id
         AND assessment.page_capture_id = page.id
         AND assessment.schema_version = 'web-page-type-assessment.v3'
        WHERE task.id = $1::uuid
        """,
        task_id,
        persisted.page_capture_id,
    )
    assert facts is not None
    assert facts["has_locked_target"] is False
    assert facts["page_type"] == "content_list"
    assert facts["page_state"] == "unknown"
    assert facts["detection_status"] == "recognized"
    assert facts["detection_confidence"] == pytest.approx(0.93)
    assert facts["metadata_detection_status"] == "recognized"
    assert facts["metadata_page_state"] == "unknown"
    assert facts["metadata_confidence"] == pytest.approx(0.93)
    assert facts["assessment_detection_status"] == "recognized"
    assert facts["assessment_page_type"] == "content_list"
    assert facts["assessment_page_state"] == "unknown"
    assert facts["assessment_status"] == "covered"
    assert facts["publication_count"] == 0
    assert facts["work_item_count"] == 0
    assert facts["rule_execution_count"] == 0
    assert facts["report_count"] == 0


async def test_committed_formal_web_closure_recovers_each_boundary_idempotently(
    committed_postgres_case: _PostgresCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实提交跨连接恢复五个边界，最终只封存一份报告。"""

    postgres_case = committed_postgres_case

    async def isolated_worker_pool(*_args: object):
        return postgres_case.pool

    monkeypatch.setattr(worker_module, "get_postgres_pool", isolated_worker_pool)
    monkeypatch.setattr(worker_module, "PostgresTaskInsightRepository", lambda: PostgresTaskInsightRepository(pool=postgres_case.pool))

    response = await _post_task(postgres_case)
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    run_id = response.json()["run_id"]
    session_id = f"web:{run_id}"
    await postgres_case.connection.execute(
        """
        UPDATE qc_task
        SET status = 'running', started_at = now(),
            finalization_state = 'open', finalization_agent_run_id = $2::uuid
        WHERE id = $1::uuid
        """,
        task_id,
        run_id,
    )
    await postgres_case.connection.execute(
        """
        UPDATE qc_agent_run
        SET status = 'running', started_at = now()
        WHERE id = $1::uuid
        """,
        run_id,
    )
    await postgres_case.connection.execute(
        """
        INSERT INTO qc_surface_session (
            session_id, task_id, surface_kind, session_owner, status,
            finalization_epoch, activated_at
        ) VALUES ($1, $2::uuid, 'web', 'web-v2-workflow', 'active', 0, now())
        """,
        session_id,
        task_id,
    )
    capture_ids = []
    from services.api.app.application.web_run_policy import load_published_web_policy
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.app.infrastructure.db.web_capture_identity import (
        PostgresWebCaptureIdentityReader,
    )
    from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
        PostgresWebRunPolicySnapshotRepository,
    )

    await PostgresWebRunPolicySnapshotRepository(pool=postgres_case.pool).freeze_or_read(
        WebRunPolicySnapshot.create(task_id=task_id, agent_run_id=run_id,
            payload=load_published_web_policy(now=datetime.now(UTC), max_duration_seconds=300)))
    for page_seq, raw_type, canonical_type, content_version in (
        (1, "content_list", "content_list", 1),
        (2, "content_list", "content_list", 1),
        (3, "content_detail", "content_detail", 2),
    ):
        capture_ids.append(
            await _seed_ready_capture(
                postgres_case,
                task_id=task_id,
                run_id=run_id,
                session_id=session_id,
                page_seq=page_seq,
                raw_page_type=raw_type,
                canonical_page_type=canonical_type,
                canonical_url="https://example.test/library?id=42",
                content_version=content_version,
            )
        )
    capture_versions = await postgres_case.connection.fetch(
        """
        SELECT
            capture.id::text AS capture_id,
            capture.metadata->>'logical_page_id' AS logical_page_id,
            (
                SELECT md5(string_agg(
                    artifact.artifact_type || ':' || artifact.checksum,
                    '|' ORDER BY artifact.artifact_type, artifact.checksum
                ))
                FROM qc_evidence_artifact AS artifact
                WHERE artifact.task_id = capture.task_id
                  AND artifact.page_capture_id = capture.id
                  AND artifact.artifact_type IN ('dom', 'visible_text')
                  AND artifact.lifecycle_status IN ('active', 'archived')
            ) AS content_fingerprint
        FROM qc_capture AS capture
        WHERE capture.id = ANY($1::uuid[])
        ORDER BY array_position($1::uuid[], capture.id)
        """,
        capture_ids,
    )
    assert [row["capture_id"] for row in capture_versions] == capture_ids
    assert len({row["logical_page_id"] for row in capture_versions}) == 1
    assert capture_versions[0]["logical_page_id"] is not None
    assert capture_versions[0]["content_fingerprint"] == (
        capture_versions[1]["content_fingerprint"]
    )
    assert capture_versions[2]["content_fingerprint"] != (
        capture_versions[0]["content_fingerprint"]
    )
    await postgres_case.connection.execute(
        """
        INSERT INTO qc_web_exploration_budget (
            agent_run_id, site_id, task_id, session_id,
            target_origin_sha256, catalog_version, catalog_sha256,
            state, sealed_at
        ) VALUES (
            $1::uuid, 'web-v2-e2e-site', $2::uuid, $3,
            $4, 'web-v2-e2e-v1', $5, 'sealed', now()
        )
        """,
        run_id,
        task_id,
        session_id,
        "a" * 64,
        "b" * 64,
    )
    await postgres_case.connection.execute(
        """
        INSERT INTO qc_v2_work_item (
            work_id, work_type, task_id, session_id, branch_id,
            source_capture_id, protocol_version, payload_hash,
            work_payload, status, deadline, finished_at
        ) VALUES (
            $1, 'exploration', $2::uuid, $3, 'web-v2-e2e-branch',
            $4::uuid, 'v1', $5, '{}'::jsonb,
            'succeeded', now() + interval '1 minute', now()
        )
        """,
        f"web-v2-e2e-exploration:{run_id}",
        task_id,
        session_id,
        capture_ids[-1],
        "c" * 64,
    )
    for catalog_position, (capture_id, page_type) in enumerate(
        zip(
            capture_ids,
            ("web_content_list", "web_content_list", "web_content_detail"),
            strict=True,
        )
    ):
        await postgres_case.connection.execute(
            """
            INSERT INTO qc_web_menu_ledger_entry (
                agent_run_id, site_id, menu_entry_id, catalog_position,
                decision, menu_path, canonical_url_sha256, execution_slot,
                status, menu_action_count, menu_intent_id,
                menu_source_capture_id, menu_action_execution_id,
                menu_probe_capture_id, menu_ready_capture_id, menu_page_type
            )
            SELECT
                $1::uuid, 'web-v2-e2e-site', $2, $3,
                'allowlisted_observe_only', '[]'::jsonb, $4, ($3 + 1)::smallint,
                $5, 1, action.intent_id,
                action.source_capture_id, action.action_execution_id,
                action.result_capture_id, action.result_capture_id, $6
            FROM qc_action_execution AS action
            WHERE action.task_id = $7::uuid
              AND action.result_capture_id = $8::uuid
            """,
            run_id,
            f"web-v2-e2e-menu-{catalog_position}",
            catalog_position,
            f"{catalog_position + 1:x}".rjust(64, "0"),
            (
                "menu_page_captured"
                if page_type == "web_content_list"
                else "detail_captured"
            ),
            page_type,
            task_id,
            capture_id,
        )

    publication_repository = PostgresH5InspectionPublicationRepository(
        pool=postgres_case.pool
    )
    notification = WebPageInspectionNotification(
        reader=PostgresWebCaptureEvidenceRepository(pool=postgres_case.pool),
        inventory=PostgresH5EvidenceInventoryRepository(pool=postgres_case.pool),
        publication_writer=publication_repository,
        wakeup=_FailingWakeup(),
    )
    first_result = await notification.notify_ready_capture(
        task_id=task_id,
        agent_run_id=run_id,
        session_id=session_id,
        capture_id=capture_ids[0],
    )
    assert first_result.status == "published_wakeup_pending"
    assert await postgres_case.connection.fetchval(
        "SELECT count(*) FROM qc_h5_inspection_publication WHERE task_id = $1::uuid",
        task_id,
    ) == 1
    replay_result = await notification.notify_ready_capture(
        task_id=task_id,
        agent_run_id=run_id,
        session_id=session_id,
        capture_id=capture_ids[1],
    )
    assert replay_result.status == "published_wakeup_pending"
    assert await postgres_case.connection.fetchval(
        "SELECT count(*) FROM qc_h5_inspection_publication WHERE task_id = $1::uuid",
        task_id,
    ) == 1
    changed_result = await notification.notify_ready_capture(
        task_id=task_id,
        agent_run_id=run_id,
        session_id=session_id,
        capture_id=capture_ids[2],
    )
    assert changed_result.status == "published_wakeup_pending"
    assert await postgres_case.connection.fetchval(
        "SELECT count(*) FROM qc_h5_inspection_publication WHERE task_id = $1::uuid",
        task_id,
    ) == 2
    content_version_gate = await postgres_case.connection.fetchrow(
        f"""
        WITH target_task AS (
            SELECT id, target_platform, task_config, ruleset_id, channel,
                   finalization_epoch, finalization_agent_run_id
            FROM qc_task
            WHERE id = $1::uuid
        ), {WEB_COMPLETION_GATE_INVENTORY_CTES_SQL}
        SELECT missing_publication_count, missing_member_count
        FROM web_publication_gate
        """,
        task_id,
    )
    assert dict(content_version_gate) == {
        "missing_publication_count": 0,
        "missing_member_count": 0,
    }

    await postgres_case.connection.execute(
        """
        UPDATE qc_task
        SET finalization_state = 'exploration_draining',
            exploration_draining_at = now()
        WHERE id = $1::uuid
        """,
        task_id,
    )
    await postgres_case.connection.execute(
        """
        UPDATE qc_agent_run
        SET status = 'done', stop_reason = 'completed', finished_at = now()
        WHERE id = $1::uuid
        """,
        run_id,
    )
    work_items = InspectionWorkItemService(
        repository=PostgresInspectionWorkItemRepository(pool=postgres_case.pool)
    )
    monkeypatch.setattr(
        rule_auth_gate_module,
        "resolve_blocked_auth_requirement",
        lambda: None,
    )
    capture_publisher = AppCaptureInspectionPublisher(
        task_query=TaskQueryService(
            repository=PostgresTaskRepository(pool=postgres_case.pool)
        ),
        snapshot_provider=_StaticSnapshotProvider(postgres_case),
        work_item_service=work_items,
        annotator=_NoopAnnotator(),
    )
    active_publisher = H5LiveInspectionPublishService(
        gate_reader=PostgresH5LiveInspectionTaskGateReader(pool=postgres_case.pool),
        capture_reader=PostgresH5EvidenceCaptureReader(pool=postgres_case.pool),
        publisher=capture_publisher,
        web_identity_reader=PostgresWebCaptureIdentityReader(pool=postgres_case.pool),
    )
    active_consumer = _DeterministicInspectionConsumer(
        case=postgres_case,
        task_id=task_id,
        run_id=run_id,
        service=work_items,
    )
    finalization = TaskFinalizationService(
        PostgresTaskFinalizationRepository(pool=postgres_case.pool)
    )
    recovery_notification = WebPageInspectionNotification(
        reader=PostgresWebCaptureEvidenceRepository(pool=postgres_case.pool),
        inventory=PostgresH5EvidenceInventoryRepository(pool=postgres_case.pool),
        publication_writer=PostgresH5InspectionPublicationRepository(
            pool=postgres_case.pool
        ),
        wakeup=_FailingWakeup(),
    )

    def _new_reconciliation(
        *,
        rule_execution_service: object,
        finalization_service: TaskFinalizationService,
        snapshot_writer: object | None = None,
    ) -> WebInspectionReconciliation:
        snapshot_repository = PostgresWebCoverageSnapshotRepository(
            pool=postgres_case.pool
        )
        return WebInspectionReconciliation(
            state_reader=snapshot_repository,
            manifest_finalizer=None,
            publication_publisher=None,
            active_session_drainer=worker_module._WebActiveSessionInspectionDrainer(
                publisher=active_publisher,
                consumer=active_consumer,
            ),
            finalization=finalization_service,
            snapshot_repository=snapshot_writer or snapshot_repository,
            gap_rule_reader=snapshot_repository,
            rule_execution_service=rule_execution_service,
            final_handoff=recovery_notification,
        )

    reconciliation = _new_reconciliation(
        rule_execution_service=RuleExecutionService(
            repository=PostgresTaskRepository(pool=postgres_case.pool)
        ),
        finalization_service=finalization,
    )

    first_reconcile = await reconciliation.reconcile(
        ReconcileWebInspectionCommand(
            task_id=task_id,
            agent_run_id=run_id,
            finalization_epoch=0,
        )
    )
    post_drain_content_version_gate = await postgres_case.connection.fetchrow(
        f"""
        WITH target_task AS (
            SELECT id, target_platform, task_config, ruleset_id, channel,
                   finalization_epoch, finalization_agent_run_id
            FROM qc_task
            WHERE id = $1::uuid
        ), {WEB_COMPLETION_GATE_INVENTORY_CTES_SQL}
        SELECT missing_publication_count, missing_member_count
        FROM web_publication_gate
        """,
        task_id,
    )
    assert dict(post_drain_content_version_gate) == {
        "missing_publication_count": 0,
        "missing_member_count": 0,
    }
    assert first_reconcile.status == "waiting_for_run_terminal"
    assert active_consumer.completed_count == 2
    publications = await postgres_case.connection.fetch(
        """
        SELECT id::text AS publication_id, row_version
        FROM qc_h5_inspection_publication
        WHERE task_id = $1::uuid AND finalization_epoch = 0
        ORDER BY id
        """,
        task_id,
    )
    assert len(publications) == 2
    pending_outbox_ids = set(
        await postgres_case.connection.fetchval(
            """
            SELECT array_agg(message_id ORDER BY message_id)
            FROM qc_outbox_event
            WHERE task_id = $1::uuid
              AND status IN ('pending', 'retry_wait')
            """,
            task_id,
        )
        or []
    )
    wakeup_transport = _RecordingWakeupTransport()
    outbox_relay = OutboxRelay(
        repository=_TaskScopedOutboxRepository(
            pool=postgres_case.pool,
            task_id=task_id,
        ),
        transport=wakeup_transport,
        lease_owner=f"web-v2-e2e-outbox:{task_id}",
    )
    initial_relay_results = await outbox_relay.publish_batch(limit=10)
    initial_deliveries = tuple(wakeup_transport.deliveries)
    assert {result.message_id for result in initial_relay_results} == (
        pending_outbox_ids
    )
    assert {lease.envelope.message_id for lease, _ in initial_deliveries} == (
        pending_outbox_ids
    )
    submission_deliveries = [
        delivery
        for delivery in initial_deliveries
        if delivery[0].envelope.message_type == TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE
    ]
    publication_deliveries = [
        delivery
        for delivery in initial_deliveries
        if delivery[0].envelope.message_type
        == H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE
    ]
    work_deliveries = [
        delivery
        for delivery in initial_deliveries
        if delivery[0].envelope.message_type
        == INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE
    ]
    assert len(submission_deliveries) == 1
    assert submission_deliveries[0][0].aggregate_type == "task_submission"
    assert submission_deliveries[0][0].envelope.task_id == task_id
    assert len(publication_deliveries) == 2
    assert len(work_deliveries) == 2
    assert all(
        lease.aggregate_type == "inspection_work_item"
        for lease, _ in work_deliveries
    )
    assert {
        delivery[0].aggregate_id for delivery in publication_deliveries
    } == {publication["publication_id"] for publication in publications}
    publication_handler = H5InspectionPublicationWakeupHandler(
        repository=publication_repository,
        publisher=capture_publisher,
        lease_owner="web-v2-e2e-publication",
        lease_seconds=180,
    )
    for lease, receipt in publication_deliveries:
        envelope = lease.envelope
        assert envelope.message_type == (
            H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE
        )
        assert envelope.aggregate_type == lease.aggregate_type
        assert envelope.aggregate_id == lease.aggregate_id
        assert envelope.aggregate_version == lease.aggregate_version
        assert envelope.task_id == task_id
        assert envelope.idempotency_key == (
            f"h5-inspection-publication:{lease.aggregate_id}:"
            f"{lease.aggregate_version}"
        )
        assert dict(envelope.payload) == {
            "protocol_version": "h5-inspection-publication.v1",
            "publication_id": lease.aggregate_id,
            "task_id": task_id,
            "finalization_epoch": 0,
            "message_version": lease.aggregate_version,
        }
        assert await publication_handler.handle(
            RedisStreamWakeup(
                stream_name=receipt.stream_name,
                stream_entry_id=receipt.stream_entry_id,
                message_id=envelope.message_id,
                message_type=envelope.message_type,
                task_id=envelope.task_id,
                aggregate_type=lease.aggregate_type,
                aggregate_id=lease.aggregate_id,
                aggregate_version=lease.aggregate_version,
                idempotency_key=envelope.idempotency_key,
            )
        )

    completion_gate_inventory = await postgres_case.connection.fetchrow(
        f"""
        WITH target_task AS (
            SELECT id, target_platform, task_config, ruleset_id, channel,
                   finalization_epoch, finalization_agent_run_id
            FROM qc_task
            WHERE id = $1::uuid
        ), {WEB_COMPLETION_GATE_INVENTORY_CTES_SQL}
        SELECT missing_publication_count, missing_member_count
        FROM web_publication_gate
        """,
        task_id,
    )
    assert dict(completion_gate_inventory) == {
        "missing_publication_count": 0,
        "missing_member_count": 0,
    }
    rollback_probe = postgres_case.connection.transaction()
    await rollback_probe.start()
    try:
        delete_status = await postgres_case.connection.execute(
            """
            DELETE FROM qc_h5_inspection_publication_rule
            WHERE publication_id = $1::uuid
              AND rule_id = $2::uuid
            """,
            publications[0]["publication_id"],
            postgres_case.observed_rule_id,
        )
        assert delete_status == "DELETE 1"
        missing_legacy_member = await postgres_case.connection.fetchval(
            f"""
            WITH target_task AS (
                SELECT id, target_platform, task_config, ruleset_id, channel,
                       finalization_epoch, finalization_agent_run_id
                FROM qc_task
                WHERE id = $1::uuid
            ), {WEB_COMPLETION_GATE_INVENTORY_CTES_SQL}
            SELECT missing_member_count FROM web_publication_gate
            """,
            task_id,
        )
        assert missing_legacy_member == 1
    finally:
        await rollback_probe.rollback()

    assert await outbox_relay.publish_batch(limit=10) == ()

    await postgres_case.connection.execute(
        """
        UPDATE qc_surface_session
        SET status = 'closed', draining_at = now(), closed_at = now()
        WHERE task_id = $1::uuid AND session_id = $2
        """,
        task_id,
        session_id,
    )
    consumer_run_ids = await postgres_case.connection.fetch(
        """
        SELECT id::text AS run_id
        FROM qc_agent_run
        WHERE task_id = $1::uuid
          AND agent_type = 'rule_judge'
          AND input_context->>'source' = 'h5_inspection_publication'
        """,
        task_id,
    )
    for consumer_run in consumer_run_ids:
        chain_state = await postgres_case.connection.fetchrow(
            """
            SELECT
                consumer.status AS consumer_status,
                consumer.stop_reason AS consumer_stop_reason,
                (SELECT array_agg(DISTINCT publication.status
                                  ORDER BY publication.status)
                 FROM qc_h5_inspection_publication AS publication
                 WHERE publication.task_id = $1::uuid
                   AND publication.finalization_epoch = 0) AS publication_statuses,
                (SELECT array_agg(DISTINCT work.status ORDER BY work.status)
                 FROM qc_inspection_work_item AS work
                 WHERE work.task_id = $1::uuid
                   AND work.finalization_epoch = 0) AS work_statuses,
                (SELECT array_agg(DISTINCT attempt.status ORDER BY attempt.status)
                 FROM qc_inspection_attempt AS attempt
                 INNER JOIN qc_inspection_work_item AS work
                   ON work.id = attempt.work_item_id
                 WHERE work.task_id = $1::uuid
                   AND work.finalization_epoch = 0) AS attempt_statuses,
                (SELECT count(*) FROM qc_outbox_event AS outbox
                 WHERE outbox.task_id = $1::uuid
                   AND outbox.aggregate_type IN (
                       'h5_inspection_publication', 'inspection_work_item'
                   )
                   AND outbox.status IN (
                       'pending', 'publishing', 'retry_wait'
                   ))::integer AS inflight_outbox_count,
                (SELECT count(*) FROM qc_event_inbox AS inbox
                 WHERE inbox.task_id = $1::uuid
                   AND inbox.aggregate_type IN (
                       'h5_inspection_publication', 'inspection_work_item'
                   )
                   AND inbox.status IN (
                       'pending', 'processing', 'retry_wait'
                   ))::integer AS inflight_inbox_count
            FROM qc_agent_run AS consumer
            WHERE consumer.id = $2::uuid
              AND consumer.task_id = $1::uuid
            """,
            task_id,
            consumer_run["run_id"],
        )
        assert dict(chain_state) == {
            "consumer_status": "done",
            "consumer_stop_reason": "h5_inspection_work_drained",
            "publication_statuses": ["published"],
            "work_statuses": ["succeeded"],
            "attempt_statuses": ["written"],
            "inflight_outbox_count": 0,
            "inflight_inbox_count": 0,
        }
    await postgres_case.connection.execute(
        "UPDATE qc_task SET finalization_retry_at = NULL WHERE id = $1::uuid",
        task_id,
    )

    snapshot_failure = _RaiseAfterCommittedSnapshot(
        PostgresWebCoverageSnapshotRepository(pool=postgres_case.pool)
    )
    crash_after_snapshot_reconciliation = _new_reconciliation(
        rule_execution_service=RuleExecutionService(
            repository=PostgresTaskRepository(pool=postgres_case.pool)
        ),
        finalization_service=TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=postgres_case.pool)
        ),
        snapshot_writer=snapshot_failure,
    )
    with pytest.raises(
        RuntimeError,
        match="injected process exit after committed snapshot",
    ):
        await crash_after_snapshot_reconciliation.reconcile(
            ReconcileWebInspectionCommand(
                task_id=task_id,
                agent_run_id=run_id,
                finalization_epoch=0,
            )
        )
    assert snapshot_failure.raised is True
    committed_snapshot = snapshot_failure.committed_snapshot
    assert committed_snapshot is not None
    async with postgres_case.pool.acquire() as snapshot_boundary_connection:
        snapshot_boundary = await snapshot_boundary_connection.fetchrow(
            """
            SELECT snapshot.id::text AS snapshot_id, snapshot.snapshot_hash,
                   (SELECT count(*) FROM qc_web_coverage_snapshot
                    WHERE task_id = $1::uuid)::integer AS snapshot_count,
                   (SELECT count(*) FROM qc_rule_execution
                    WHERE task_id = $1::uuid
                      AND source_idempotency_key LIKE
                          'web-coverage-gap:%')::integer AS gap_count
            FROM qc_web_coverage_snapshot AS snapshot
            WHERE snapshot.task_id = $1::uuid
              AND snapshot.agent_run_id = $2::uuid
              AND snapshot.finalization_epoch = 0
            """,
            task_id,
            run_id,
        )
    assert snapshot_boundary is not None
    assert snapshot_boundary["snapshot_count"] == 1
    assert snapshot_boundary["gap_count"] == 0
    assert snapshot_boundary["snapshot_id"] == committed_snapshot.snapshot_id
    assert snapshot_boundary["snapshot_hash"] == committed_snapshot.snapshot_hash

    committed_rule_failure = _RaiseAfterCommittedRuleExecution(
        RuleExecutionService(
            repository=PostgresTaskRepository(pool=postgres_case.pool)
        )
    )
    crash_after_rule_reconciliation = _new_reconciliation(
        rule_execution_service=committed_rule_failure,
        finalization_service=TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=postgres_case.pool)
        ),
    )
    with pytest.raises(
        RuntimeError,
        match="injected process exit after committed rule execution",
    ):
        await crash_after_rule_reconciliation.reconcile(
            ReconcileWebInspectionCommand(
                task_id=task_id,
                agent_run_id=run_id,
                finalization_epoch=0,
            )
        )
    assert committed_rule_failure.raised is True
    async with postgres_case.pool.acquire() as committed_boundary_connection:
        boundary_facts = await committed_boundary_connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM qc_web_coverage_snapshot
                 WHERE task_id = $1::uuid)::integer AS snapshot_count,
                (SELECT count(*) FROM qc_rule_execution
                 WHERE task_id = $1::uuid
                   AND source_idempotency_key LIKE 'web-coverage-gap:%')::integer
                    AS gap_count,
                (SELECT count(*) FROM qc_completion_gate_snapshot
                 WHERE task_id = $1::uuid)::integer AS gate_count,
                (SELECT count(*) FROM qc_report_gate_binding
                 WHERE task_id = $1::uuid)::integer AS binding_count,
                (SELECT id::text FROM qc_web_coverage_snapshot
                 WHERE task_id = $1::uuid) AS snapshot_id,
                (SELECT snapshot_hash FROM qc_web_coverage_snapshot
                 WHERE task_id = $1::uuid) AS snapshot_hash
            """,
            task_id,
        )
    assert boundary_facts["snapshot_count"] == 1
    assert boundary_facts["gap_count"] == 1
    assert boundary_facts["gate_count"] == 0
    assert boundary_facts["binding_count"] == 0
    assert boundary_facts["snapshot_id"] == snapshot_boundary["snapshot_id"]
    assert boundary_facts["snapshot_hash"] == snapshot_boundary["snapshot_hash"]

    recovery_reconciliation = _new_reconciliation(
        rule_execution_service=RuleExecutionService(
            repository=PostgresTaskRepository(pool=postgres_case.pool)
        ),
        finalization_service=TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=postgres_case.pool)
        ),
    )

    insights = PostgresTaskInsightRepository(pool=postgres_case.pool)
    monkeypatch.setattr(worker_module, "PostgresTaskInsightRepository", lambda: insights)

    async def _pool(*_args: object):
        return postgres_case.pool

    monkeypatch.setattr(worker_module, "get_postgres_pool", _pool)
    state_store = PostgresAgentRunStateStore(pool=postgres_case.pool)
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key=f"test:web-v2-e2e:retry:{task_id}",
    )
    deterministic_report_time = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def _new_production_report_generator() -> _RecordingReportGenerator:
        return _RecordingReportGenerator(
            ReportGenerationService(
                query_service=TaskQueryService(
                    repository=PostgresTaskRepository(pool=postgres_case.pool)
                ),
                clock=lambda: deterministic_report_time,
            )
        )

    report_generation_failure = _FailOnceReportGenerator(
        ReportGenerationService(
            query_service=TaskQueryService(
                repository=PostgresTaskRepository(pool=postgres_case.pool)
            ),
            clock=lambda: deterministic_report_time,
        )
    )
    failed_report_retry = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=postgres_case.pool)
        ),
        final_report_generator=report_generation_failure,
        web_inspection_reconciliation=recovery_reconciliation,
    )
    assert failed_report_retry == 0
    assert report_generation_failure.calls == 1
    async with postgres_case.pool.acquire() as failed_report_connection:
        failed_report_facts = await failed_report_connection.fetchrow(
            """
            SELECT
                task.finalization_state,
                task.completion_gate_snapshot_id,
                (SELECT count(*) FROM qc_completion_gate_snapshot
                 WHERE task_id = task.id)::integer AS gate_count,
                (SELECT count(*) FROM qc_report_gate_binding
                 WHERE task_id = task.id)::integer AS binding_count,
                (SELECT count(*)
                 FROM qc_report_payload_snapshot AS payload
                 INNER JOIN qc_report_gate_binding AS binding
                   ON binding.report_id = payload.report_id
                  AND binding.report_version = payload.report_version
                 WHERE binding.task_id = task.id)::integer AS report_payload_count
            FROM qc_task AS task WHERE task.id = $1::uuid
            """,
            task_id,
        )
    assert dict(failed_report_facts) == {
        "finalization_state": "session_draining",
        "completion_gate_snapshot_id": None,
        "gate_count": 0,
        "binding_count": 0,
        "report_payload_count": 0,
    }

    async def _load_real_completion_gate(connection):
        row = await connection.fetchrow(
            _GATE_FACTS_SQL,
            task_id,
            "completed_with_gaps",
        )
        assert row is not None
        return _parse_gate_facts(row)

    baseline_gate = await _load_real_completion_gate(postgres_case.connection)
    assert baseline_gate.candidate_rule_count == baseline_gate.terminal_candidate_rule_count
    nonterminal_work = [
        dict(row)
        for row in await postgres_case.connection.fetch(
            """
            SELECT id::text AS work_item_id, page_capture_id::text AS capture_id,
                   status, session_dependency
            FROM qc_inspection_work_item
            WHERE task_id = $1::uuid
              AND status NOT IN (
                  'succeeded', 'partial', 'failed', 'cancelled', 'superseded'
              )
            ORDER BY id
            """,
            task_id,
        )
    ]
    assert nonterminal_work == []
    assert baseline_gate.gate_passed is True, baseline_gate.block_reasons
    auth_result = await postgres_case.connection.fetchrow(
        """
        SELECT execution.execution_status, execution.source_idempotency_key,
               execution.rule_id::text, execution.page_capture_id::text,
               execution.judge_input,
               execution.judge_output,
               count(*) FILTER (
                   WHERE execution.source_idempotency_key LIKE 'web-coverage-gap:%'
               ) OVER ()::integer AS gap_result_count
        FROM qc_rule_execution AS execution
        INNER JOIN qc_rule AS rule ON rule.id = execution.rule_id
        WHERE execution.task_id = $1::uuid
          AND rule.rule_code = 'alioth_8'
        """,
        task_id,
    )
    assert auth_result is not None
    assert auth_result["execution_status"] == "skipped"
    assert auth_result["source_idempotency_key"] == (
        f"web-auth-not-applicable:{task_id}:{run_id}:0:"
        f"{auth_result['rule_id']}:{auth_result['page_capture_id']}"
    )
    auth_judge_input = json.loads(auth_result["judge_input"])
    assert auth_judge_input["task_id"] == task_id
    assert auth_judge_input["agent_run_id"] == run_id
    assert auth_judge_input["page_capture_id"] == auth_result["page_capture_id"]
    assert auth_judge_input["finalization_epoch"] == 0
    assert json.loads(auth_result["judge_output"])["reason"] == (
        "auth_requirement_not_met"
    )
    assert auth_result["gap_result_count"] == 0

    missing_legacy_gap = postgres_case.connection.transaction()
    await missing_legacy_gap.start()
    try:
        deleted = await postgres_case.connection.execute(
            """
            DELETE FROM qc_rule_execution AS execution
            USING qc_rule AS rule
            WHERE execution.rule_id = rule.id
              AND execution.task_id = $1::uuid
              AND rule.rule_code = 'web_e2e_legacy_gap'
              AND execution.source_idempotency_key LIKE 'web-coverage-gap:%'
            """,
            task_id,
        )
        assert deleted == "DELETE 1"
        blocked_gate = await _load_real_completion_gate(postgres_case.connection)
        assert blocked_gate.candidate_rule_count == (
            blocked_gate.terminal_candidate_rule_count + 1
        )
        assert blocked_gate.gate_passed is False
        assert CompletionBlockReason.CANDIDATE_RULE_COVERAGE_NONTERMINAL in (
            blocked_gate.block_reasons
        )
    finally:
        await missing_legacy_gap.rollback()

    restored_gate = await _load_real_completion_gate(postgres_case.connection)
    assert restored_gate.candidate_rule_count == restored_gate.terminal_candidate_rule_count
    assert restored_gate.gate_passed is True
    await postgres_case.connection.execute(
        "UPDATE qc_task SET finalization_retry_at = NULL WHERE id = $1::uuid",
        task_id,
    )

    binding_report_generator = _new_production_report_generator()
    failing_binding_pool = _FailOnceAtReportBindingPool(postgres_case.pool)
    failed_gate_retry = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=failing_binding_pool)
        ),
        final_report_generator=binding_report_generator,
        web_inspection_reconciliation=recovery_reconciliation,
    )
    assert failed_gate_retry == 0
    assert failing_binding_pool.failed is True, await postgres_case.connection.fetchval(
        "SELECT finalization_last_error FROM qc_task WHERE id=$1::uuid", task_id,
    )
    async with postgres_case.pool.acquire() as failed_gate_connection:
        failed_gate_facts = await failed_gate_connection.fetchrow(
            """
            SELECT
                task.finalization_state,
                task.completion_gate_snapshot_id,
                (SELECT count(*) FROM qc_completion_gate_snapshot
                 WHERE task_id = task.id)::integer AS gate_count,
                (SELECT count(*) FROM qc_report_gate_binding
                 WHERE task_id = task.id)::integer AS binding_count,
                (SELECT count(*)
                 FROM qc_report_payload_snapshot AS payload
                 INNER JOIN qc_report_gate_binding AS binding
                   ON binding.report_id = payload.report_id
                  AND binding.report_version = payload.report_version
                 WHERE binding.task_id = task.id)::integer AS report_payload_count
            FROM qc_task AS task WHERE task.id = $1::uuid
            """,
            task_id,
        )
    assert dict(failed_gate_facts) == {
        "finalization_state": "session_draining",
        "completion_gate_snapshot_id": None,
        "gate_count": 0,
        "binding_count": 0,
        "report_payload_count": 0,
    }
    await postgres_case.connection.execute(
        "UPDATE qc_task SET finalization_retry_at = NULL WHERE id = $1::uuid",
        task_id,
    )

    recovery_report_generator = _new_production_report_generator()
    successful_retry = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=postgres_case.pool)
        ),
        final_report_generator=recovery_report_generator,
        web_inspection_reconciliation=_new_reconciliation(
            rule_execution_service=RuleExecutionService(
                repository=PostgresTaskRepository(pool=postgres_case.pool)
            ),
            finalization_service=TaskFinalizationService(
                PostgresTaskFinalizationRepository(pool=postgres_case.pool)
            ),
        ),
    )
    replayed_retry = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=postgres_case.pool)
        ),
        final_report_generator=recovery_report_generator,
        web_inspection_reconciliation=_new_reconciliation(
            rule_execution_service=RuleExecutionService(
                repository=PostgresTaskRepository(pool=postgres_case.pool)
            ),
            finalization_service=TaskFinalizationService(
                PostgresTaskFinalizationRepository(pool=postgres_case.pool)
            ),
        ),
    )

    task = await postgres_case.connection.fetchrow(
        """
        SELECT finalization_state, finalization_epoch, execution_status,
               completion_gate_snapshot_id
        FROM qc_task WHERE id = $1::uuid
        """,
        task_id,
    )
    counts = await postgres_case.connection.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM qc_h5_inspection_publication
             WHERE task_id = $1::uuid)::integer AS publication_count,
            (SELECT count(*) FROM qc_inspection_work_item
             WHERE task_id = $1::uuid)::integer AS work_item_count,
            (SELECT count(*) FROM qc_rule_execution
             WHERE task_id = $1::uuid AND page_capture_id IS NOT NULL)::integer
                AS observed_result_count,
            (SELECT count(*) FROM qc_rule_execution
             WHERE task_id = $1::uuid AND page_capture_id IS NULL)::integer
                AS gap_result_count,
            (SELECT count(*) FROM qc_web_coverage_snapshot
             WHERE task_id = $1::uuid)::integer AS snapshot_count,
            (SELECT count(*) FROM qc_completion_gate_snapshot
             WHERE task_id = $1::uuid AND gate_passed)::integer AS passed_gate_count,
            (SELECT count(*) FROM qc_report_gate_binding
             WHERE task_id = $1::uuid)::integer AS report_binding_count,
            (SELECT count(*)
             FROM qc_report_payload_snapshot AS payload
             INNER JOIN qc_report_gate_binding AS binding
               ON binding.report_id = payload.report_id
              AND binding.report_version = payload.report_version
             WHERE binding.task_id = $1::uuid)::integer AS report_payload_count,
            (SELECT count(*) FROM qc_review_item
             WHERE task_id = $1::uuid)::integer AS review_item_count
        """,
        task_id,
    )
    assert successful_retry == 1
    assert replayed_retry == 0
    assert task is not None
    assert task["finalization_state"] == "sealed"
    assert task["execution_status"] == "completed_with_gaps"
    assert task["completion_gate_snapshot_id"] is not None
    assert dict(counts) == {
        "publication_count": 2,
        "work_item_count": 2,
        "observed_result_count": 3,
        "gap_result_count": 5,
        "snapshot_count": 1,
        "passed_gate_count": 1,
        "report_binding_count": 1,
        "report_payload_count": 1,
        "review_item_count": 0,
    }
    assert binding_report_generator.calls == 1
    assert recovery_report_generator.calls == 1
    assert binding_report_generator is not recovery_report_generator
    assert binding_report_generator.reports[0]["agent_run_id"] == run_id
    assert recovery_report_generator.reports[0]["agent_run_id"] == run_id

    lineage = await postgres_case.connection.fetchrow(
        """
        SELECT
            task.finalization_epoch AS task_epoch,
            snapshot.finalization_epoch AS snapshot_epoch,
            gate.finalization_epoch AS gate_epoch,
            binding.finalization_epoch AS binding_epoch,
            (payload.report_payload #>> '{completion_gate,finalization_epoch}')::integer
                AS report_epoch
        FROM qc_task AS task
        INNER JOIN qc_web_coverage_snapshot AS snapshot
          ON snapshot.task_id = task.id
         AND snapshot.agent_run_id = task.finalization_agent_run_id
        INNER JOIN qc_completion_gate_snapshot AS gate
          ON gate.task_id = task.id
         AND gate.gate_snapshot_id = task.completion_gate_snapshot_id
        INNER JOIN qc_report_gate_binding AS binding
          ON binding.task_id = task.id
         AND binding.gate_snapshot_id = gate.gate_snapshot_id
        INNER JOIN qc_report_payload_snapshot AS payload
          ON payload.report_id = binding.report_id
         AND payload.report_version = binding.report_version
        WHERE task.id = $1::uuid
        """,
        task_id,
    )
    assert lineage is not None
    assert len(set(dict(lineage).values())) == 1, dict(lineage)
    finalization_epoch = int(lineage["task_epoch"])

    publication_lineage = await postgres_case.connection.fetch(
        """
        SELECT id::text AS publication_id, task_id::text,
               source_agent_run_id::text AS run_id,
               finalization_epoch, page_capture_id::text AS capture_id
        FROM qc_h5_inspection_publication
        WHERE task_id = $1::uuid ORDER BY page_capture_id
        """,
        task_id,
    )
    assert len(publication_lineage) == 2
    assert {row["capture_id"] for row in publication_lineage} == {
        capture_ids[0],
        capture_ids[2],
    }
    assert capture_ids[1] not in {
        row["capture_id"] for row in publication_lineage
    }
    assert all(
        row["task_id"] == task_id
        and row["run_id"] == run_id
        and row["finalization_epoch"] == finalization_epoch
        for row in publication_lineage
    )
    publication_by_id = {
        row["publication_id"]: row for row in publication_lineage
    }
    work_lineage = await postgres_case.connection.fetch(
        """
        SELECT work.task_id::text, work.producer_agent_run_id::text AS run_id,
               work.finalization_epoch, work.page_capture_id::text AS capture_id
        FROM qc_inspection_work_item AS work
        WHERE work.task_id = $1::uuid ORDER BY work.page_capture_id
        """,
        task_id,
    )
    assert len(work_lineage) == 2
    assert {row["capture_id"] for row in work_lineage} == {
        capture_ids[0],
        capture_ids[2],
    }
    assert capture_ids[1] not in {row["capture_id"] for row in work_lineage}
    assert all(
        row["task_id"] == task_id
        and row["run_id"] == run_id
        and row["finalization_epoch"] == finalization_epoch
        for row in work_lineage
    )
    rule_lineage = await postgres_case.connection.fetch(
        """
        SELECT execution.task_id::text,
               execution.agent_run_id::text AS run_id,
               rule.rule_code,
               execution.page_capture_id::text AS capture_id,
               execution.source_idempotency_key,
               COALESCE(
                   work.finalization_epoch,
                   snapshot.finalization_epoch,
                   (execution.judge_input->>'finalization_epoch')::integer
               )
                   AS finalization_epoch
        FROM qc_rule_execution AS execution
        INNER JOIN qc_rule AS rule ON rule.id = execution.rule_id
        LEFT JOIN qc_inspection_work_item_rule AS work_rule
          ON work_rule.rule_execution_id = execution.id
        LEFT JOIN qc_inspection_work_item AS work
          ON work.id = work_rule.work_item_id
        LEFT JOIN qc_web_coverage_snapshot AS snapshot
          ON snapshot.task_id = execution.task_id
         AND snapshot.agent_run_id = execution.agent_run_id
         AND execution.judge_input->>'coverage_snapshot_id' = snapshot.id::text
        WHERE execution.task_id = $1::uuid
        ORDER BY execution.page_capture_id NULLS LAST
        """,
        task_id,
    )
    assert len(rule_lineage) == 8
    invalid_rule_lineage = [
        dict(row)
        for row in rule_lineage
        if row["task_id"] != task_id
        or row["run_id"] != run_id
        or row["finalization_epoch"] != finalization_epoch
    ]
    assert invalid_rule_lineage == []
    observed_rule_lineage = [
        row for row in rule_lineage if row["capture_id"] is not None
    ]
    gap_rule_lineage = [row for row in rule_lineage if row["capture_id"] is None]
    assert {row["capture_id"] for row in observed_rule_lineage} == {
        capture_ids[0],
        capture_ids[2],
    }
    assert len(gap_rule_lineage) == 5
    assert all(
        str(row["source_idempotency_key"]).startswith("web-coverage-gap:")
        for row in gap_rule_lineage
    )

    publication_outbox = await postgres_case.connection.fetch(
        """
        SELECT message_id, message_type, aggregate_type, aggregate_id,
               aggregate_version, idempotency_key, status, envelope_payload
        FROM qc_outbox_event
        WHERE task_id = $1::uuid AND message_type = $2
        ORDER BY aggregate_id
        """,
        task_id,
        H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
    )
    assert len(publication_outbox) == 2
    for outbox_row in publication_outbox:
        envelope = json.loads(outbox_row["envelope_payload"])
        payload = envelope["payload"]
        assert outbox_row["status"] == "published"
        assert envelope["message_id"] == outbox_row["message_id"]
        assert envelope["message_type"] == outbox_row["message_type"]
        assert envelope["aggregate_type"] == outbox_row["aggregate_type"]
        assert envelope["aggregate_id"] == outbox_row["aggregate_id"]
        assert envelope["aggregate_version"] == outbox_row["aggregate_version"]
        assert envelope["idempotency_key"] == outbox_row["idempotency_key"]
        assert envelope["task_id"] == task_id
        assert payload["task_id"] == task_id
        assert payload["finalization_epoch"] == finalization_epoch
        assert payload["publication_id"] == outbox_row["aggregate_id"]
        publication = publication_by_id[outbox_row["aggregate_id"]]
        assert publication["run_id"] == run_id
        assert publication["capture_id"] in capture_ids

    snapshot = await postgres_case.connection.fetchrow(
        """
        SELECT id::text AS snapshot_id, snapshot_hash, finalization_epoch,
               observed_page_types, missing_page_types, normalization_version
        FROM qc_web_coverage_snapshot
        WHERE task_id = $1::uuid AND agent_run_id = $2::uuid
          AND finalization_epoch = 0
        """,
        task_id,
        run_id,
    )
    assert snapshot is not None
    observed_page_types = json.loads(snapshot["observed_page_types"])
    missing_page_types = json.loads(snapshot["missing_page_types"])
    assert set(observed_page_types) == {"content_list", "content_detail"}
    assert missing_page_types == ["home", "video_detail", "video_list"]
    assert snapshot["normalization_version"] == "web-page-type-assessment.v3"
    assert snapshot["finalization_epoch"] == finalization_epoch
    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        async with postgres_case.connection.transaction():
            await postgres_case.connection.execute(
                """
                UPDATE qc_web_coverage_snapshot
                SET observed_page_types = '[]'::jsonb
                WHERE id = $1::uuid
                """,
                snapshot["snapshot_id"],
            )

    gap = await postgres_case.connection.fetchrow(
        """
        SELECT execution_status, page_capture_id, source_idempotency_key,
               judge_input
        FROM qc_rule_execution
        WHERE task_id = $1::uuid AND rule_id = $2::uuid
        """,
        task_id,
        postgres_case.gap_rule_id,
    )
    assert gap is not None
    assert gap["execution_status"] == "evidence_insufficient"
    assert gap["page_capture_id"] is None
    assert gap["source_idempotency_key"] == (
        f"web-coverage-gap:{snapshot['snapshot_id']}:video_detail:web_e2e_gap"
    )
    assert json.loads(gap["judge_input"])["coverage_snapshot_id"] == (
        snapshot["snapshot_id"]
    )
    gap_rule_codes = await postgres_case.connection.fetchval(
        """
        SELECT array_agg(rule.rule_code ORDER BY rule.rule_code)
        FROM qc_rule_execution AS execution
        INNER JOIN qc_rule AS rule ON rule.id = execution.rule_id
        WHERE execution.task_id = $1::uuid
          AND execution.page_capture_id IS NULL
        """,
        task_id,
    )
    assert gap_rule_codes == [
        "alioth_21",
        "web_e2e_gap",
        "web_e2e_legacy_gap",
        "web_e2e_legacy_target_empty",
        "web_e2e_legacy_target_missing",
    ]

    auth_not_applicable = await postgres_case.connection.fetchrow(
        """
        SELECT execution.execution_status, execution.page_capture_id::text,
               execution.judge_input, execution.judge_output
        FROM qc_rule_execution AS execution
        INNER JOIN qc_rule AS rule ON rule.id = execution.rule_id
        WHERE execution.task_id = $1::uuid
          AND rule.rule_code = 'alioth_8'
        """,
        task_id,
    )
    assert auth_not_applicable is not None
    assert auth_not_applicable["execution_status"] == "skipped"
    assert auth_not_applicable["page_capture_id"] == capture_ids[-1]
    assert json.loads(auth_not_applicable["judge_input"])["bootstrap"] is False
    assert json.loads(auth_not_applicable["judge_output"])["reason"] == (
        "auth_requirement_not_met"
    )

    bound_report = await postgres_case.connection.fetchrow(
        """
        SELECT binding.report_id, binding.report_version,
               binding.report_payload_hash, binding.finalization_epoch,
               binding.gate_snapshot_id,
               payload.report_payload_hash AS snapshot_payload_hash,
               payload.report_payload
        FROM qc_report_gate_binding AS binding
        INNER JOIN qc_report_payload_snapshot AS payload
          ON payload.report_id = binding.report_id
         AND payload.report_version = binding.report_version
        WHERE binding.task_id = $1::uuid
        """,
        task_id,
    )
    assert bound_report is not None
    assert bound_report["report_id"] == f"task:{task_id}:run:{run_id}"
    assert bound_report["report_version"] == 2
    assert bound_report["report_payload_hash"] == bound_report["snapshot_payload_hash"]
    assert bound_report["finalization_epoch"] == finalization_epoch
    assert bound_report["gate_snapshot_id"] == task["completion_gate_snapshot_id"]
    report_payload = json.loads(bound_report["report_payload"])
    explanation = report_payload["page_exploration"]["web_report_explanation"]
    assert explanation["task_id"] == task_id and explanation["agent_run_id"] == run_id
    assert explanation["finalization_epoch"] == finalization_epoch
    assert explanation["rule_materials"]
    assert len(report_payload["page_exploration"]["web_logical_pages"]) == 1
    assert report_payload["page_exploration"]["web_logical_pages"][0]["primary_capture_id"] == capture_ids[2]
    from services.api.app.application.report_insight import ReportInsightService

    frozen_insight = await ReportInsightService(repository=object(),
        final_report_snapshot_repository=PostgresTaskRepository(pool=postgres_case.pool)).report_insight(
            task_id, access_scope=postgres_case.access_scope)
    assert frozen_insight["web_report_explanation"] == explanation
    assert frozen_insight["web_logical_pages"][0]["primary_capture_id"] == capture_ids[2]
    _, exported = await ReportInsightService(repository=object(),
        final_report_snapshot_repository=PostgresTaskRepository(pool=postgres_case.pool)).export_html(
            task_id, access_scope=postgres_case.access_scope)
    assert 'data-web-report-explanation="v1"' in exported
    assert explanation["rule_materials"][0]["work_item_id"] in exported
    assert report_payload["task"]["task_id"] == task_id
    assert report_payload["agent_run_id"] == run_id
    assert report_payload["completion_gate"] == {
        "gate_passed": True,
        "gate_snapshot_id": task["completion_gate_snapshot_id"],
        "finalization_epoch": finalization_epoch,
        "block_reasons": [],
    }
    report_coverage = report_payload["page_exploration"]["web_coverage"]
    assert report_coverage["coverage_snapshot_id"] == snapshot["snapshot_id"]
    assert report_coverage["snapshot_hash"] == snapshot["snapshot_hash"]
    assert report_coverage["coverage_status"] == "completed_with_gaps"
    assert report_coverage["observed_page_types"] == observed_page_types
    assert report_coverage["missing_page_types"] == missing_page_types
    assert report_coverage["unrecognized_page_types"] == []
    assert report_coverage["normalization_version"] == "web-page-type-assessment.v3"
    assert {
        (
            item["raw_page_type"],
            item["canonical_page_type"],
            item["capture_count"],
        )
        for item in report_coverage["normalization_records"]
    } == {
        ("content_list", "content_list", 2),
        ("content_detail", "content_detail", 1),
    }
    assert report_coverage["gap_rule_codes"] == [
        "alioth_21",
        "web_e2e_gap",
        "web_e2e_legacy_gap",
        "web_e2e_legacy_target_empty",
        "web_e2e_legacy_target_missing",
    ]
    assert report_payload["summary"]["total_rules"] == 8
    assert report_payload["summary"]["pass"] == 2
    assert report_payload["summary"]["skipped"] == 1
    assert report_payload["summary"]["evidence_insufficient"] == 5
    assert report_payload["summary"]["coverage_gap_rules"] == 5
    assert report_payload["summary"]["overall_status"] == "manual_review"
    assert len(report_payload["rule_results"]) == 8
    persisted_report_rules = await postgres_case.connection.fetch(
        """
        SELECT execution.id::text AS execution_id,
               execution.rule_id::text AS rule_id,
               rule.rule_code,
               execution.page_capture_id::text AS page_capture_id,
               execution.execution_status
        FROM qc_rule_execution AS execution
        INNER JOIN qc_rule AS rule ON rule.id = execution.rule_id
        WHERE execution.task_id = $1::uuid
          AND execution.agent_run_id = $2::uuid
        """,
        task_id,
        run_id,
    )
    assert {
        (
            result["execution_id"],
            result["rule_id"],
            result["rule_code"],
            result["page_capture_id"],
            result["execution_status"],
        )
        for result in report_payload["rule_results"]
    } == {
        (
            row["execution_id"],
            row["rule_id"],
            row["rule_code"],
            row["page_capture_id"],
            row["execution_status"],
        )
        for row in persisted_report_rules
    }

    workstation = PostgresWorkstationQueryRepository(pool=postgres_case.pool)
    detail = await workstation.get_task_detail(
        task_id,
        access_scope=postgres_case.access_scope,
    )
    listing = await workstation.list_tasks(
        WorkstationTaskListFilter(task_name="Web V2 正式闭环"),
        access_scope=postgres_case.access_scope,
    )
    assert detail is not None
    assert detail.run_id == run_id
    assert detail.report_available is True
    assert detail.coverage_status == "completed_with_gaps"
    assert detail.finalization_state == "sealed"
    assert detail.completion_gate_passed is True
    assert detail.execution_status == "completed_with_gaps"
    assert set(detail.coverage_observed_page_types) == {
        "content_list",
        "content_detail",
    }
    assert detail.coverage_missing_page_types == ("home", "video_detail", "video_list")
    assert detail.coverage_unrecognized_page_types == ()
    assert detail.coverage_normalization_protocol_version == (
        "web-page-type-assessment.v3"
    )
    assert set(detail.coverage_observed_page_types) == set(
        report_coverage["observed_page_types"]
    )
    assert detail.coverage_missing_page_types == tuple(
        report_coverage["missing_page_types"]
    )
    assert detail.coverage_unrecognized_page_types == tuple(
        report_coverage["unrecognized_page_types"]
    )
    assert listing.total == 1
    assert listing.items[0].task_id == task_id
    assert listing.items[0].report_status == "ready"
