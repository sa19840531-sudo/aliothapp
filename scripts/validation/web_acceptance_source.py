"""实况验收的 HTTP 边界和事务只读数据库投影。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime

import asyncpg
import httpx

from scripts.validation.web_acceptance_evidence import (
    failure_witnesses,
    full_page_associations,
    obstruction_witness,
    popup_completed,
)
from scripts.validation.web_acceptance_reporter import (
    TERMINAL,
    is_nonblocking_supplemental_failure,
    safe_projection,
)


def mapping(value):
    return json.loads(value) if isinstance(value, str) else value or {}


class AcceptanceClient:
    def __init__(self, base_url, *, token="", username="", password=""):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=30, trust_env=False, follow_redirects=False
        )
        self.token, self.username, self.password = token, username, password

    async def request(self, method, path, *, payload=None, idempotency_key=None):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            result = await self.http.request(method, path, json=payload, headers=headers)
            result.raise_for_status()
            body = result.json()
            if not isinstance(body, dict):
                raise ValueError("响应必须为对象")
            return body
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("VALIDATION_API_REQUEST_FAILED") from None

    async def login(self):
        if not self.token:
            response = await self.request(
                "POST",
                "/api/v1/auth/login",
                payload={"username": self.username, "password": self.password},
            )
            self.token = response["token"]

    async def close(self):
        await self.http.aclose()


async def read_database_state(dsn, task_id, run_id):
    """单次一致性只读事务只查询元数据，不加载 DOM/正文或连接配置。"""
    connection = await asyncpg.connect(dsn, timeout=10, command_timeout=15)
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            run = await connection.fetchrow(
                """SELECT run.status::text AS status, task.finalization_state,
                   task.target_platform::text AS target_platform,
                   run.input_context #>> '{task_config,execution_mode}' AS execution_mode,
                   jsonb_build_object(
                     'agent_run_id',run.output_context #>> '{final_report,agent_run_id}',
                     'task_id',run.output_context #>> '{final_report,task,task_id}',
                     'is_final',run.output_context #> '{final_report,is_final}',
                     'report_stage',run.output_context #>> '{final_report,report_stage}',
                     'technical_execution_complete',
                       run.output_context #> '{final_report,summary,technical_execution_complete}'
                   ) AS final_report_witness,
                   session.status AS session_status, policy.payload,policy.payload_sha256
                   FROM qc_agent_run run JOIN qc_task task ON task.id=run.task_id
                   LEFT JOIN qc_surface_session session ON session.task_id=task.id
                     AND session.session_id='web:' || run.id::text
                   LEFT JOIN qc_web_run_policy_snapshot policy ON policy.agent_run_id=run.id
                   WHERE run.id=$1::uuid AND task.id=$2::uuid AND task.target_platform='web'""",
                run_id,
                task_id,
            )
            if run is None:
                raise ValueError("VALIDATION_RUN_OWNERSHIP_MISSING")
            verify_available_frozen_policy(
                run["payload"],
                run["payload_sha256"],
                run_status=str(run["status"]),
                task_id=task_id,
                run_id=run_id,
            )
            budgets = await connection.fetch(
                "SELECT site_id,menu_limit FROM qc_web_exploration_budget "
                "WHERE task_id=$1::uuid AND agent_run_id=$2::uuid ORDER BY site_id",
                task_id,
                run_id,
            )
            checkpoints = await connection.fetch(
                """SELECT logical_page_key,current_stage,stage_status,row_version,attempt_count,
                   next_attempt_at,last_error_code,last_error_detail,gap_codes,gaps,
                   stage_generation,cursor_payload,
                   artifact_ids
                   FROM qc_web_run_stage_checkpoint WHERE run_id=$1::uuid
                   ORDER BY logical_page_key""",
                run_id,
            )
            captures = await connection.fetch(
                """SELECT page.id::text AS capture_id,page.url AS final_url,page.page_type,
                   common.capture_role AS role,common.parent_capture_id::text AS parent_capture_id,
                   common.action_execution_id,
                   common.metadata->>'capture_purpose' AS capture_purpose,
                   page.metadata->>'capture_purpose' AS page_capture_purpose,
                   common.metadata->>'finalization_epoch' AS finalization_epoch,
                   page.metadata->>'finalization_epoch' AS page_finalization_epoch,
                   page.parent_page_capture_id::text AS page_parent_capture_id,
                   page.capture_role AS page_role,
                   common.session_command_sequence AS command_sequence,
                   page.metadata->>'status_code' AS status_code,
                   CASE WHEN jsonb_typeof(page.metadata->'reachable')='boolean'
                     THEN (page.metadata->>'reachable')::boolean END AS reachable,
                   page.detection_confidence AS confidence,
                   page.metadata->'web_page_type_assessment' AS assessment,
                   COALESCE(common.metadata #> '{pipeline_metrics,obstruction_chain}',
                     page.metadata #> '{pipeline_metrics,obstruction_chain}') AS obstruction_chain,
                   common.metadata #> '{pipeline_metrics,web_natural_redirect_chain}' AS navigation
                   FROM qc_page_capture page JOIN qc_capture common ON common.id=page.id
                   WHERE page.task_id=$1::uuid AND page.agent_run_id=$2::uuid
                   AND common.task_id=page.task_id AND common.agent_run_id=page.agent_run_id
                   AND common.surface_kind='web' AND common.capture_status='captured'
                   AND page.capture_status='captured' ORDER BY common.created_at,common.id""",
                task_id,
                run_id,
            )
            artifacts = await connection.fetch(
                """SELECT artifact.id::text AS artifact_id,artifact.artifact_type,
                   artifact.content_type,artifact.storage_path,
                   artifact.metadata->>'purpose' AS screenshot_purpose,
                   CASE WHEN artifact.artifact_type='page_coverage' THEN jsonb_build_object(
                     'protocol_version',artifact.metadata->'protocol_version',
                     'capture_mode',artifact.metadata->'capture_mode',
                     'associated_page_capture_id',artifact.metadata->'associated_page_capture_id',
                     'associated_screenshot_artifact_ids',
                       artifact.metadata->'associated_screenshot_artifact_ids'
                   ) END AS coverage_metadata,
                   artifact.page_capture_id::text AS capture_id
                   FROM qc_evidence_artifact artifact
                   JOIN qc_capture capture ON capture.id=artifact.page_capture_id
                   WHERE capture.task_id=$1::uuid AND capture.agent_run_id=$2::uuid
                   AND artifact.task_id=capture.task_id
                   AND artifact.lifecycle_status IN ('active','archived')""",
                task_id,
                run_id,
            )
            actions = await connection.fetch(
                """SELECT action_execution_id,status,error_code,intent_id,idempotency_key,
                   command_sequence,finalization_epoch,
                   requested_source_capture_id::text,result_capture_id::text,
                   intent_payload->>'source_capture_id' AS intent_source_capture_id,
                   intent_payload #> '{parameters,initial_surface_action}'
                     AS initial_surface_action,
                   intent_payload #>> '{parameters,web_capture_stage}' AS web_capture_stage,
                   intent_payload #>> '{parameters,web_menu_entry_id}' AS web_menu_entry_id,
                   result_payload->'state_summary' AS state_summary,
                   intent_payload->>'action_type' AS action FROM qc_action_execution
                   WHERE task_id=$1::uuid AND session_id='web:' || $2::text
                   ORDER BY command_sequence""",
                task_id,
                run_id,
            )
            frontier = await connection.fetch(
                """SELECT menu_entry_id,status,
                   discovery_manifest #>> '{candidate,url}' AS canonical_url,
                   diagnostic #>> '{error_code}' AS error_code,execution_slot,
                   menu_intent_id,menu_action_count,
                   discovery_manifest #>> '{candidate,frontier_structure_cluster_key}'
                     AS structure_cluster_key
                   FROM qc_web_menu_ledger_entry
                   WHERE agent_run_id=$1::uuid ORDER BY menu_entry_id""",
                run_id,
            )
            candidates = await connection.fetch(
                """SELECT 'menu'::text AS candidate_kind,
                   COALESCE(entry.discovery_manifest #>> '{candidate,entry_id}',
                     entry.menu_entry_id) AS candidate_id,
                   entry.menu_entry_id,
                   entry.discovery_manifest #>> '{candidate,target_hint}' AS raw_href,
                   CASE WHEN entry.discovery_manifest #>> '{candidate,target_hint}' IS NULL
                     THEN 'WEB_MENU_RAW_HREF_NOT_PERSISTED' END AS raw_href_gap,
                   entry.discovery_manifest #>> '{candidate,url}' AS normalized_url,
                   entry.discovery_manifest #>> '{candidate,url}' AS canonical_url,
                   COALESCE(entry.discovery_source_capture_id,
                     entry.menu_source_capture_id)::text AS source_capture_id,
                   entry.discovery_manifest #>> '{candidate,source_kind}' AS candidate_source_kind,
                   entry.discovery_manifest #>> '{candidate,frontier_structure_cluster_key}'
                     AS structure_cluster_key,
                   entry.status AS frontier_status,entry.execution_slot,
                   NULL::integer AS source_menu_execution_slot,
                   CASE WHEN entry.execution_slot IS NULL THEN 'not_allocated'
                     ELSE 'allocated' END AS budget_state,
                   CASE WHEN entry.execution_slot IS NULL THEN entry.decision END AS budget_reason,
                   entry.menu_action_count=1 AS executed,
                   menu_intent.status AS action_status
                   FROM qc_web_menu_ledger_entry entry
                   LEFT JOIN qc_action_execution menu_intent
                     ON menu_intent.action_execution_id=entry.menu_action_execution_id
                     AND menu_intent.task_id=$1::uuid
                     AND menu_intent.session_id='web:' || $2::text
                   WHERE entry.agent_run_id=$2::uuid
                   UNION ALL
                   SELECT 'detail'::text AS candidate_kind,entry.detail_candidate_id,
                   entry.menu_entry_id,NULL::text AS raw_href,
                   'WEB_DETAIL_RAW_HREF_NOT_PERSISTED'::text AS raw_href_gap,
                   detail_intent.destination_url AS normalized_url,
                   detail_intent.destination_url AS canonical_url,
                   entry.detail_source_capture_id::text AS source_capture_id,
                   NULL::text AS candidate_source_kind,NULL::text AS structure_cluster_key,
                   entry.status AS frontier_status,NULL::integer AS execution_slot,
                   entry.execution_slot AS source_menu_execution_slot,
                   'not_applicable'::text AS budget_state,NULL::text AS budget_reason,
                   entry.detail_action_count=1 AS executed,
                   detail_intent.action_status
                   FROM qc_web_menu_ledger_entry entry
                   LEFT JOIN LATERAL (
                     SELECT action.status AS action_status,
                       action.intent_payload #>> '{parameters,destination_url}' AS destination_url
                     FROM qc_action_execution action
                     WHERE action.action_execution_id=entry.detail_action_execution_id
                       AND action.task_id=$1::uuid
                       AND action.session_id='web:' || $2::text
                   ) detail_intent ON TRUE
                   WHERE entry.agent_run_id=$2::uuid
                     AND entry.detail_candidate_id IS NOT NULL
                   ORDER BY menu_entry_id,candidate_kind,candidate_id""",
                task_id,
                run_id,
            )
            phases = await connection.fetch(
                """SELECT phase_key,status,phase_protocol_version,
                   source_capture_id::text AS source_capture_id,evidence_artifact_ids,
                   CASE WHEN phase_key IN (
                     'web_stage_classification','web_stage_candidate_navigation'
                   )
                     AND phase_protocol_version='web-run-stage-evidence.v1'
                     THEN context_payload->>'optional_primary_error_code'
                   END AS optional_primary_error_code,
                   finalization_epoch,command_sequence FROM qc_surface_phase_run
                   WHERE task_id=$1::uuid AND session_id='web:' || $2::text""",
                task_id,
                run_id,
            )
        return project_database_rows(
            {
                "run": dict(run),
                "budgets": budgets,
                "checkpoints": checkpoints,
                "captures": captures,
                "artifacts": artifacts,
                "actions": actions,
                "frontier": frontier,
                "candidates": candidates,
                "phases": phases,
            },
            task_id=task_id,
            run_id=run_id,
        )
    finally:
        await connection.close(timeout=5)


def report_is_finalized(run, *, task_id, run_id):
    """纯探索读同轮终稿事实，不将技术收口等同于无缺口或正式质检封存。"""
    if run["finalization_state"] == "sealed":
        return True
    if not (
        run.get("target_platform") == "web"
        and run.get("execution_mode") == "exploration_only"
        and run["status"] in TERMINAL
        and run["session_status"] == "closed"
    ):
        return False
    try:
        witness = mapping(run.get("final_report_witness"))
    except (ValueError, TypeError):
        return False
    return (
        isinstance(witness, dict)
        and witness.get("agent_run_id") == run_id
        and witness.get("task_id") == task_id
        and witness.get("is_final") is True
        and witness.get("report_stage") == "final"
        and witness.get("technical_execution_complete") is True
    )


def project_database_rows(rows, *, task_id, run_id):
    """将真实列映射为报告事实；缺失能力不推断为成功。"""
    run = rows["run"]
    checkpoints = [
        {
            **dict(row),
            "gap_codes": mapping(row["gap_codes"]),
            "artifact_ids": mapping(row.get("artifact_ids")) or [],
            "cursor_payload": mapping(row["cursor_payload"]),
        }
        for row in rows["checkpoints"]
    ]
    captures = []
    full_pages = full_page_associations(rows["captures"], rows["artifacts"], rows["phases"])
    for row in rows["captures"]:
        assessment, navigation = mapping(row["assessment"]), mapping(row["navigation"])
        captures.append(
            {
                **dict(row),
                "requested_url": navigation[0] if navigation else row["final_url"],
                "structure_fingerprint": assessment.get("structure_fingerprint"),
                "subject_region_facts": assessment.get("subject_region_facts", {}),
                "obstruction_chain": obstruction_witness(row.get("obstruction_chain")),
                **(
                    {"full_page_screenshot_artifact_id": full_pages[row["capture_id"]]}
                    if row["capture_id"] in full_pages
                    else {}
                ),
                "artifacts": [
                    dict(item)
                    for item in rows["artifacts"]
                    if item["capture_id"] == row["capture_id"]
                ],
            }
        )
    root = next((row for row in checkpoints if row["logical_page_key"] == "home"), None)
    root_gap = bool(
        root
        and any(
            not is_nonblocking_supplemental_failure(code) for code in root["gap_codes"]
        )
    )
    phases = {row["phase_key"] for row in rows["phases"] if row["status"] == "completed"}
    home_capture_id = root["cursor_payload"].get("capture_id") if root else None
    navigation_ok = any(
        row["role"] == "bootstrap" or row["capture_id"] == home_capture_id for row in captures
    )
    failures = failure_witnesses(
        checkpoints, rows["captures"], rows["actions"], frontier=rows["frontier"], run_id=run_id
    )
    if run["status"] == "failed" and run["payload"] is None:
        failures.append({
            "error_code": "VALIDATION_POLICY_MISSING",
            "category": "preflight_execution",
            "blocks_code_acceptance": True,
        })
    for phase in rows["phases"]:
        phase = dict(phase)
        code = phase.get("optional_primary_error_code")
        if (
            phase.get("status") == "completed"
            and isinstance(code, str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", code)
        ):
            failures.append(
                {
                    "error_code": code,
                    "category": "optional_primary",
                    "blocks_code_acceptance": False,
                    "capture_id": phase.get("source_capture_id"),
                }
            )
    capabilities = {}
    for family in ("list", "detail"):
        capabilities[f"{family}_structure_clusters"] = sorted(
            {
                row["structure_fingerprint"]
                for row in captures
                if row.get("structure_fingerprint")
                and row.get("page_type") in {f"video_{family}", f"content_{family}"}
            }
        )
    capabilities["long_page"] = bool(full_pages)
    capabilities["ad_isolation"] = "web_stage_ad_capture" in phases
    capabilities["age_gate"] = any(row["page_type"] == "age_gate" for row in captures)
    readiness_dismissals = sum(
        1
        for row in rows["actions"]
        if row.get("status") == "completed"
        and row.get("action") == "click"
        and str(row.get("intent_id") or "").startswith("web-readiness:click:")
        and row.get("result_capture_id")
    )
    capabilities["popup"] = any(
        popup_completed(row["obstruction_chain"]) for row in captures
    ) or readiness_dismissals >= 2
    policy_menu_limit = mapping(run["payload"]).get("budget", {}).get("menu_limit")
    budgets = [dict(row) for row in rows.get("budgets", ())]
    execution_limits = {row["menu_limit"] for row in budgets}
    execution_menu_limit = next(iter(execution_limits)) if len(execution_limits) == 1 else None
    menu_limit_consistent = (
        all(row["menu_limit"] == policy_menu_limit for row in budgets)
        if budgets and policy_menu_limit is not None
        else None
    )
    if menu_limit_consistent is False:
        failures.append(
            {
                "error_code": "WEB_BUDGET_POLICY_MISMATCH",
                "category": "contract_consistency",
                "blocks_code_acceptance": True,
            }
        )
    state = {
        "task_id": task_id,
        "run_id": run_id,
        "status": run["status"],
        "session_closed": run["session_status"] == "closed",
        "report_finalized": report_is_finalized(run, task_id=task_id, run_id=run_id),
        "checkpoints": checkpoints,
        "captures": captures,
        "actions": [dict(row) for row in rows["actions"]],
        "frontier": [dict(row) for row in rows["frontier"]],
        "candidates": [dict(row) for row in rows.get("candidates", ())],
        "capabilities": capabilities,
        "failures": failures,
        "menu_limit": policy_menu_limit,
        "policy_menu_limit": policy_menu_limit,
        "execution_menu_limit": execution_menu_limit,
        "menu_limit_consistent": menu_limit_consistent,
        "execution_budgets": budgets,
        "candidate_filter_observed": "web_stage_href_discovery" in phases
        or (root_gap and not captures),
        "root_navigation": "succeeded" if navigation_ok else None,
        "root_gap": root_gap,
        "root_failure": next(
            (
                row
                for row in failures
                if row["category"] == "external_transport" and row.get("logical_page_key") == "home"
            ),
            None,
        ),
        "gap_codes": [code for row in checkpoints for code in row["gap_codes"]],
    }
    return safe_projection(state)


def referenced_artifact_ids(state):
    """收集报告和 checkpoint 中所有证据引用，供新查询结果交叉核对。"""
    result = set()
    if isinstance(state, dict):
        for key, value in state.items():
            if key.endswith("artifact_id") and isinstance(value, str):
                result.add(value)
            elif key == "artifact_ids" and isinstance(value, (list, tuple)):
                result.update(value)
            else:
                result.update(referenced_artifact_ids(value))
    elif isinstance(state, (list, tuple)):
        for row in state:
            result.update(referenced_artifact_ids(row))
    return result


def available_web_workers(heartbeats, *, now):
    return sum(
        1
        for row in heartbeats
        if "web_explorer" in row.get("roles", [])
        and row.get("heartbeat_at")
        and 0 <= (now - row["heartbeat_at"]).total_seconds() <= 90
    )


async def read_web_worker_count(redis_url):
    from redis.asyncio import Redis

    from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue

    client = Redis.from_url(
        redis_url, decode_responses=True, socket_timeout=10, socket_connect_timeout=10
    )
    try:
        heartbeats = await RedisAgentRunQueue(client, ttl_seconds=86400).read_worker_heartbeats()
        return available_web_workers(heartbeats, now=datetime.now(UTC))
    finally:
        await client.aclose()


def verify_frozen_policy(body, digest, *, task_id, run_id):
    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
    from services.api.app.application.web_run_policy import materialize_web_policy
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.app.domain.page.web_page_catalog import CURRENT_WEB_CATALOG_VERSION

    snapshot = WebRunPolicySnapshot(
        task_id=task_id, agent_run_id=run_id, payload=body, payload_sha256=digest
    )
    if body.get("guide_version") != CURRENT_WEB_CATALOG_VERSION:
        # 历史目录只做只读审计，正式执行仍按当前版本合同处理。
        audit = LegacyWebRunAuditAdapter.load(snapshot)
        budget = audit.frozen_fields.get("budget")
        menu_limit = budget.get("menu_limit") if isinstance(budget, Mapping) else None
        if type(menu_limit) is not int or not 1 <= menu_limit <= 1000:
            raise ValueError("VALIDATION_POLICY_MENU_LIMIT_INVALID")
        return menu_limit
    policy = materialize_web_policy(snapshot)
    return policy.menu_limit


def verify_available_frozen_policy(body, digest, *, run_status, task_id, run_id):
    """冻结前失败作为 Run 失败事实返回，不中断其他站点验收。"""
    if body is None and digest is None:
        if run_status in {"pending", "queued", "running", "failed"}:
            return None
        raise ValueError("VALIDATION_POLICY_MISSING")
    if body is None or digest is None:
        raise ValueError("VALIDATION_POLICY_INCOMPLETE")
    return verify_frozen_policy(mapping(body), digest, task_id=task_id, run_id=run_id)


def submission_payload(site_url):
    """验证站点仅来自命令行，不向生产执行器注入站点流程或历史覆盖。"""
    return {
        "task_name": "Web V2 分层验收",
        "target": {
            "platform": "web",
            "url": site_url,
            "browser_profile_id": "desktop_fhd_1920x1080_v1",
            "adult_self_attestation": {"declared": True, "statement_version": "adult-v1"},
        },
        "channel": "api",
        "scan_mode": "full",
        "timeout_minutes": 180,
        "product": {
            "department_name": "质量保障部",
            "product_code": "WEB-V2-ACCEPTANCE",
            "product_name": "Web V2 验证",
        },
        "remark": "分层实况验收",
    }
