"""Web 分类身份的只读认证；不接受页面、请求或模型声明的版本。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicyError
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)


def resolve_persisted_capture_identity(row, policy):
    unavailable = {
        "logical_page_id": None,
        "logical_page_identity_status": "WEB_LOGICAL_ID_UNTRUSTED",
    }
    if not row["assessment_present"] and not row.get("target_material_present"):
        return unavailable
    try:
        expected = policy.logical_id(row["url"])
    except ValueError:
        return dict(unavailable, logical_page_identity_status="WEB_LOGICAL_ID_INVALID_URL")
    if any(row.get(key) != expected for key in ("page_logical_id", "common_logical_id")):
        return dict(unavailable, logical_page_identity_status="WEB_LOGICAL_ID_CONFLICT")
    trusted_target = (
        row.get("target_material_present")
        and row.get("target_logical_id") == expected
        and row.get("target_identity_version") == policy.version
    )
    if any(
        (row.get(key) or (policy.version if trusted_target else None)) != policy.version
        for key in ("page_identity_version", "common_identity_version")
    ):
        return dict(unavailable, logical_page_identity_status="WEB_LOGICAL_ID_VERSION_MISMATCH")
    return {
        "logical_page_id": expected,
        "logical_page_identity_status": "verified",
        "canonical_url": policy.canonical(row["url"]),
    }


async def read_web_capture_identities(connection, *, task_id, agent_run_id):
    snapshot = await read_web_policy_on_connection(connection, task_id, agent_run_id)
    if snapshot is None:
        raise WebRunPolicyError("WEB_POLICY_HISTORY_MISSING")
    policy = LegacyWebRunAuditAdapter.load(snapshot).identity_policy
    # 先探测本项迁移，缺迁移以稳定诊断关闭 Web 身份能力，不触发 UndefinedTable。
    if not await connection.fetchval(
        "SELECT to_regclass('qc_web_logical_target') IS NOT NULL "
        "AND to_regclass('qc_web_logical_target_alias') IS NOT NULL"
    ):
        raise WebRunPolicyError("WEB_LOGICAL_TARGET_SCHEMA_MISSING")
    rows = await connection.fetch(
        """
        SELECT page.id::text AS capture_id, page.url, page.page_type,
               page.metadata->>'logical_page_id' AS page_logical_id,
               capture.metadata->>'logical_page_id' AS common_logical_id,
               page.metadata->>'identity_policy_version' AS page_identity_version,
               capture.metadata->>'identity_policy_version' AS common_identity_version,
               target.logical_page_id AS target_logical_id,
               target.identity_policy_version AS target_identity_version,
               (target.state='materialized' AND EXISTS(
                  SELECT 1 FROM qc_h5_page_type_assessment original
                  WHERE original.task_id=task.id AND original.agent_run_id=$2::uuid
                    AND original.page_capture_id=target.assessment_capture_id
                    AND original.session_id=session.session_id
                    AND original.schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4'))) AS
                    target_material_present,
               EXISTS(SELECT 1 FROM qc_h5_page_type_assessment assessment
                      WHERE assessment.task_id=task.id AND assessment.agent_run_id=$2::uuid
                        AND assessment.session_id=session.session_id
                        AND assessment.page_capture_id=page.id
                        AND assessment.schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4')) AS
                        assessment_present
        FROM qc_page_capture page JOIN qc_capture capture ON capture.id=page.id
        JOIN qc_task task ON task.id=page.task_id
        JOIN qc_surface_session session ON session.session_id=capture.surface_session_id
          AND session.task_id=task.id AND session.surface_kind='web'
          AND session.session_owner='web-v2-workflow'
        LEFT JOIN qc_web_logical_target target ON target.task_id=task.id AND
        target.agent_run_id=$2::uuid
          AND target.session_id=session.session_id AND
          target.finalization_epoch=session.finalization_epoch
          AND (target.assessment_capture_id=page.id OR target.primary_capture_id=page.id)
        WHERE task.id=$1::uuid AND task.target_platform='web'
          AND page.agent_run_id=$2::uuid AND capture.agent_run_id=$2::uuid
          AND page.task_id=capture.task_id AND capture.surface_kind='web'
          AND session.session_id='web:' || $2::uuid::text
          AND page.surface_session_id=session.session_id
          AND page.capture_status='captured' AND capture.capture_status='captured'
          AND capture.metadata->>'finalization_epoch'=session.finalization_epoch::text
          AND page.metadata->>'finalization_epoch'=capture.metadata->>'finalization_epoch'
        """,
        task_id,
        agent_run_id,
    )
    identities = {
        row["capture_id"]: resolve_persisted_capture_identity(row, policy) for row in rows
    }
    aliases = await connection.fetch(
        "SELECT alias_logical_page_id,logical_page_id FROM qc_web_logical_target_alias WHERE "
        "agent_run_id=$1::uuid AND task_id=$2::uuid AND evidence_kind='natural_redirect'",
        agent_run_id,
        task_id,
    )
    alias_targets = {item["alias_logical_page_id"]: item["logical_page_id"] for item in aliases}
    from urllib.parse import urlsplit, urlunsplit

    for row in rows:
        identity = identities[row["capture_id"]]
        if identity["logical_page_identity_status"] != "verified":
            continue
        parsed = urlsplit(identity["canonical_url"])
        root_ids = {
            policy.logical_id(urlunsplit((scheme, parsed.netloc, "/", "", "")))
            for scheme in (("http", "https") if parsed.scheme == "https" else ("http",))
        }
        # 只有可信根入口自然重定向、无业务 query/hash 且已分类列表，才恢复子路径首页角色。
        identity["home_role_verified"] = bool(
            not parsed.query
            and not parsed.fragment
            and row["page_type"] in {"video_list", "content_list"}
            and any(
                alias_targets.get(root_id) == identity["logical_page_id"] for root_id in root_ids
            )
        )
    return identities


class PostgresWebCaptureIdentityReader:
    def __init__(self, *, pool: Any = None, database_url: str | None = None):
        self._pool = pool
        self._database_url = database_url

    async def read_for_run(self, *, task_id, agent_run_id):
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            return await read_web_capture_identities(
                connection, task_id=task_id, agent_run_id=agent_run_id
            )
