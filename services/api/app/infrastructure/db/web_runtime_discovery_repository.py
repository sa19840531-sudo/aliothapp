"""复用 Web 菜单台账保存发现审计，不创建第二套槽位。"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import urlsplit

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
)
from services.api.app.application.web_page_applicability import (
    infer_runtime_entry_page_type,
)
from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.application.web_runtime_discovery import (
    WebRuntimeMenuEntry,
    discovery_entry_id,
    discovery_safety_reason,
)
from services.api.app.domain.page.web_origin import canonical_web_origin, web_origin_matches
from services.api.app.infrastructure.db.web_logical_target_repository import _require_scope
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)


async def record_discovery(
    connection, *, command, site, candidate, source_capture_id, error_code, discovery_rank=None,
):
    if discovery_rank is not None and (
        isinstance(discovery_rank, bool)
        or not isinstance(discovery_rank, int)
        or discovery_rank < 0
    ):
        raise ValueError("discovery_rank 必须为非负整数")
    if (
        candidate.source_page_url
        and discovery_safety_reason(candidate, site_origin=site.origin)
        == "WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED"
    ):
        raise WebExplorationBudgetLedgerConflictError(
            "WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED"
        )
    budget = await connection.fetchrow(
        """
        SELECT budget.* FROM qc_web_exploration_budget budget
        JOIN qc_task task ON task.id=budget.task_id AND task.target_platform='web'
        JOIN qc_agent_run run ON run.id=budget.agent_run_id AND run.task_id=task.id
        JOIN qc_surface_session session ON session.task_id=task.id
          AND session.session_id=budget.session_id
        JOIN qc_capture source ON source.id=$5::uuid AND source.task_id=task.id
          AND source.agent_run_id=run.id AND source.surface_session_id=session.session_id
          AND source.surface_kind='web' AND source.capture_status='captured'
        WHERE budget.task_id=$1::uuid AND budget.agent_run_id=$2::uuid
          AND budget.session_id=$3 AND budget.site_id=$4 AND budget.state='open'
          AND task.status='running' AND task.finalization_state='open' AND run.status='running'
          AND session.session_owner='web-v2-workflow' AND session.status IN ('active','draining')
          AND session.finalization_epoch=task.finalization_epoch
          AND session.current_capture_id=source.id
        FOR UPDATE OF budget
    """,
        command.task_id,
        command.agent_run_id,
        command.session_id,
        site.site_id,
        source_capture_id,
    )
    if budget is None:
        raise WebExplorationBudgetLedgerConflictError("WEB_DISCOVERY_SCOPE_INVALID")
    entry_id = discovery_entry_id(candidate)
    inferred_page_type = infer_runtime_entry_page_type(
        label=candidate.label,
        url=candidate.url,
    )
    await connection.execute(
        """
        INSERT INTO qc_web_menu_ledger_entry (
          agent_run_id, site_id, menu_entry_id, catalog_position, decision, menu_path,
          canonical_url_sha256, status, diagnostic, source_kind,
          discovery_source_capture_id, discovery_manifest, parent_menu_entry_id,
          menu_depth, exploration_path, logical_target_identity
        ) VALUES ($1::uuid,$2,$3,$4,'runtime_record_only',$5::jsonb,$6,
          'runtime_discovered_unreviewed',$7::jsonb,'runtime_discovered',$8::uuid,$9::jsonb,
          $10,$11,$12::jsonb,$13)
        ON CONFLICT (agent_run_id,site_id,menu_entry_id)
        DO UPDATE SET diagnostic=EXCLUDED.diagnostic
        WHERE qc_web_menu_ledger_entry.source_kind='runtime_discovered'
          AND qc_web_menu_ledger_entry.decision='runtime_record_only'
          AND qc_web_menu_ledger_entry.status='runtime_discovered_unreviewed'
          AND qc_web_menu_ledger_entry.menu_action_count=0
    """,
        command.agent_run_id,
        site.site_id,
        entry_id,
        # 规划顺序独立于同一链接的重复出现次数，后者仍参与原候选身份。
        len(site.entries) + (candidate.menu_position if discovery_rank is None else discovery_rank),
        json.dumps(list(candidate.menu_path), ensure_ascii=False),
        sha256(
            (WebRuntimeMenuEntry.from_candidate(candidate).canonical_url or entry_id).encode(
                "utf-8"
            )
        ).hexdigest(),
        json.dumps({"error_code": error_code}),
        source_capture_id,
        json.dumps(
            {
                "version": "web-discovery-manifest.v1",
                "entry_id": entry_id,
                "candidate": candidate.to_dict(),
                **({
                    "ordering_version": "web-frontier-plan-order.v1",
                    "discovery_rank": discovery_rank,
                } if discovery_rank is not None else {}),
                "applicability": {
                    "page_type": inferred_page_type,
                    "evidence": "runtime_href_semantics",
                },
            },
            ensure_ascii=False,
        ),
        candidate.parent_menu_entry_id,
        candidate.menu_depth,
        json.dumps(list(candidate.exploration_path), ensure_ascii=False),
        candidate.logical_target_identity,
    )


async def authorize_discovery_slot(connection, *, command, entry, source_capture_id):
    """同事务复核真实 Web Run/current/Epoch，并占用目录共用的唯一槽位。"""
    scope, identity_policy = await _require_scope(
        connection,
        command.task_id,
        command.agent_run_id,
        command.session_id,
        source_capture_id,
    )
    snapshot = await read_web_policy_on_connection(
        connection, command.task_id, command.agent_run_id
    )
    policy = materialize_web_policy(snapshot)
    budget = await connection.fetchrow(
        """
        SELECT budget.* FROM qc_web_exploration_budget budget
        JOIN qc_web_menu_ledger_entry entry ON entry.agent_run_id=budget.agent_run_id
          AND entry.site_id=budget.site_id
        WHERE budget.agent_run_id=$1::uuid AND budget.task_id=$2::uuid AND budget.session_id=$3
          AND entry.menu_entry_id=$4 AND entry.source_kind='runtime_discovered'
          AND budget.state='open'
        FOR UPDATE OF budget
    """,
        command.agent_run_id,
        command.task_id,
        command.session_id,
        entry.entry_id,
    )
    if budget is None:
        return False
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        require_budget_policy_match,
    )

    require_budget_policy_match(budget, policy)
    row = await connection.fetchrow(
        """
        SELECT * FROM qc_web_menu_ledger_entry
        WHERE agent_run_id=$1::uuid AND site_id=$2 AND menu_entry_id=$3 FOR UPDATE
    """,
        command.agent_run_id,
        budget["site_id"],
        entry.entry_id,
    )
    if row["menu_action_count"]:
        # 未知或进行中的动作只返回原 reservation，调用者不得再次执行。
        return (
            row["status"] in {"menu_reserved", "detail_reserved"}
            and str(row["menu_source_capture_id"]) == source_capture_id
        )
    persisted_candidate, reason = _read_persisted_discovery_candidate(row, entry)
    discovery_source_capture_id = row.get("discovery_source_capture_id")
    if reason is None:
        try:
            # 当前 Capture 只证明本次 GOTO 的动作源；原始 DOM href 的发现 Capture
            # 允许是同一任务/Run/会话内的历史材料，但必须重新做非 current 作用域校验。
            await _require_scope(
                connection,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                str(discovery_source_capture_id),
                require_current=False,
            )
        except ValueError:
            reason = "WEB_DISCOVERY_SOURCE_SCOPE_INVALID"
    candidate = entry.candidate
    candidate_site_origin = candidate.frontier_site_origin or command.target_url
    if candidate.url and not web_origin_matches(candidate.url, candidate_site_origin):
        # 已由 _require_scope 锁定 current/Epoch；升级只信该 Capture 的实际页面 URL。
        source_url = await connection.fetchval(
            "SELECT url FROM qc_page_capture WHERE id=$1::uuid "
            "AND task_id=$2::uuid AND agent_run_id=$3::uuid",
            source_capture_id,
            command.task_id,
            command.agent_run_id,
        )
        candidate = replace(candidate, source_page_url=source_url)
    if reason is None and not policy.discovery_policy.enabled:
        reason = "WEB_DISCOVERY_DISABLED"
    elif reason is None and discovery_safety_reason(
        persisted_candidate,
        site_origin=persisted_candidate.frontier_site_origin or command.target_url,
    ):
        reason = discovery_safety_reason(
            persisted_candidate,
            site_origin=persisted_candidate.frontier_site_origin or command.target_url,
        )
    elif reason is None and discovery_safety_reason(candidate, site_origin=candidate_site_origin):
        reason = discovery_safety_reason(candidate, site_origin=candidate_site_origin)
    elif (
        reason is None
        and (min(policy.deadline, command.deadline) - datetime.now(UTC)).total_seconds() <= 15
    ):
        reason = "WEB_DISCOVERY_CLOSEOUT_RESERVED"
    occupied = await connection.fetch(
        """
        SELECT execution_slot, source_kind, status, discovery_manifest
        FROM qc_web_menu_ledger_entry
        WHERE agent_run_id=$1::uuid AND site_id=$2 AND execution_slot IS NOT NULL
    """,
        command.agent_run_id,
        budget["site_id"],
    )
    free_slot = await connection.fetchval(
        """
        SELECT COALESCE(MAX(execution_slot), 0) + 1 FROM qc_web_menu_ledger_entry
        WHERE agent_run_id=$1::uuid AND site_id=$2
        """,
        command.agent_run_id,
        budget["site_id"],
    )
    if reason is None:
        reason = frozen_frontier_rejection(
            policy=policy.frontier_policy,
            candidate=persisted_candidate,
            ledger_rows=occupied,
            site_origin=persisted_candidate.frontier_site_origin or command.target_url,
        )
    if reason is None and (
        (budget["menu_limit"] is not None and free_slot > budget["menu_limit"])
        or (
            policy.discovery_policy.max_unlisted_menus is not None
            and sum(item["source_kind"] == "runtime_discovered" for item in occupied)
            >= policy.discovery_policy.max_unlisted_menus
        )
    ):
        reason = "WEB_DISCOVERY_BUDGET_EXHAUSTED"
    if reason is not None:
        await connection.execute(
            """
            UPDATE qc_web_menu_ledger_entry SET diagnostic=jsonb_build_object('error_code',$4::text)
            WHERE agent_run_id=$1::uuid AND site_id=$2 AND menu_entry_id=$3 AND menu_action_count=0
        """,
            command.agent_run_id,
            budget["site_id"],
            entry.entry_id,
            reason,
        )
        return False
    authorization = {
        "source_capture_id": source_capture_id,
        "candidate_id": entry.candidate.entry_id,
        "candidate": entry.candidate.to_dict(),
        "policy_sha256": snapshot.payload_sha256,
        "finalization_epoch": scope["finalization_epoch"],
    }
    await connection.execute(
        """
        UPDATE qc_web_menu_ledger_entry SET execution_slot=$4, decision='runtime_observe_only',
          status='pending', discovery_manifest=discovery_manifest
            || jsonb_build_object('authorization',$5::jsonb),
          diagnostic='{}'::jsonb
        WHERE agent_run_id=$1::uuid AND site_id=$2 AND menu_entry_id=$3
          AND source_kind='runtime_discovered' AND menu_action_count=0 AND execution_slot IS NULL
    """,
        command.agent_run_id,
        budget["site_id"],
        entry.entry_id,
        free_slot,
        json.dumps(authorization),
    )
    return True


def frozen_frontier_rejection(*, policy, candidate, ledger_rows, site_origin):
    """在持久 execution slot 边界二次执行冻结 frontier 约束。"""
    if candidate.menu_depth > policy.max_depth:
        return "WEB_FRONTIER_DEPTH_EXHAUSTED"
    claimed_rows = tuple(
        row
        for row in ledger_rows
        if row.get("execution_slot") is not None
        and row.get("status") not in {"pending", "runtime_discovered_unreviewed"}
    )
    if len(claimed_rows) >= policy.max_pages:
        return "WEB_FRONTIER_PAGE_BUDGET_EXHAUSTED"
    persisted_rows = tuple(
        (row, item)
        for row in claimed_rows
        if (item := _frontier_candidate_manifest(row)) is not None
    )
    if sum(
        item.get("frontier_structure_cluster_key")
        == getattr(candidate, "frontier_structure_cluster_key", None)
        for _row, item in persisted_rows
        if item.get("frontier_structure_cluster_key") is not None
        and getattr(candidate, "frontier_structure_cluster_key", None) is not None
    ) >= policy.max_samples_per_structure:
        return "WEB_FRONTIER_STRUCTURE_BUDGET_EXHAUSTED"
    candidate_parts = urlsplit(candidate.url)
    if candidate_parts.query:
        variants = {
            urlsplit(str(item.get("url"))).query
            for _row, item in persisted_rows
            if item.get("url")
            and _same_origin_path(str(item["url"]), candidate.url)
            and urlsplit(str(item["url"])).query
        }
        if (
            candidate_parts.query not in variants
            and len(variants) >= policy.max_query_variants_per_path
        ):
            return "WEB_FRONTIER_QUERY_BUDGET_EXHAUSTED"
    inflight_origins = [
        canonical_web_origin(str(item["url"]))
        for row, item in persisted_rows
        if row.get("status") in {"menu_reserved", "detail_reserved"} and item.get("url")
    ]
    candidate_origin = canonical_web_origin(candidate.url)
    if (
        sum(origin == candidate_origin for origin in inflight_origins)
        >= policy.max_concurrency_per_origin
    ):
        return "WEB_FRONTIER_ORIGIN_CONCURRENCY_BUSY"
    if not web_origin_matches(candidate.url, site_origin):
        return "WEB_DISCOVERY_CROSS_ORIGIN_BLOCKED"
    return None


def _frontier_candidate_manifest(row):
    manifest = row.get("discovery_manifest")
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except json.JSONDecodeError:
            return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("candidate"), dict):
        return None
    return manifest["candidate"]


def _same_origin_path(left: str, right: str) -> bool:
    try:
        return canonical_web_origin(left) == canonical_web_origin(right) and (
            urlsplit(left).path.rstrip("/") or "/"
        ) == (urlsplit(right).path.rstrip("/") or "/")
    except ValueError:
        return False


def _read_persisted_discovery_candidate(row, entry):
    """恢复冻结候选，并核对不会随页面观察变化的稳定身份与目标。"""
    manifest = row.get("discovery_manifest")
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except json.JSONDecodeError:
            return None, "WEB_DISCOVERY_MANIFEST_INVALID"
    if not isinstance(manifest, dict):
        return None, "WEB_DISCOVERY_MANIFEST_INVALID"
    raw = manifest.get("candidate")
    if not isinstance(raw, dict):
        return None, "WEB_DISCOVERY_MANIFEST_INVALID"
    raw = dict(raw)
    for key in ("menu_path", "exploration_path", "navigation_evidence"):
        if isinstance(raw.get(key), list):
            raw[key] = tuple(raw[key])
    try:
        candidate = WebNavigationCandidate(**raw)
        persisted_entry = WebRuntimeMenuEntry.from_candidate(candidate)
    except (TypeError, ValueError):
        return None, "WEB_DISCOVERY_MANIFEST_INVALID"
    if (
        manifest.get("version") != "web-discovery-manifest.v1"
        or manifest.get("entry_id") != entry.entry_id
        or persisted_entry.entry_id != entry.entry_id
        or persisted_entry.canonical_url != entry.canonical_url
        or row.get("discovery_source_capture_id") is None
    ):
        return None, "WEB_DISCOVERY_MANIFEST_INVALID"
    return candidate, None
