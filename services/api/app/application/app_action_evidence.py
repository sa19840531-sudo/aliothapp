"""App 动作轮的结构化操作日志：写入 Capture metadata，供 Observation Packet / Judge 使用。

``qc_task_log`` 旁路过程日志不得当作可信证据；本模块只投影已发生的 Interaction
事实到 ``qc_capture.metadata.action_log``。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

logger = logging.getLogger(__name__)

_APP_SURFACES = frozenset({SurfaceKind.ANDROID, SurfaceKind.IOS})
_APP_AD_LANDING_SCHEMA_VERSION = "ad-navigation-evidence.v1"
_APP_AD_LANDING_CONTRACT_VERSION = "app-ad-landing-consistency.v1"
_ANDROID_BROWSER_PACKAGES = frozenset(
    {
        "com.android.chrome",
        "com.chrome.beta",
        "com.chrome.canary",
        "com.microsoft.emmx",
        "org.mozilla.firefox",
    }
)


def build_app_action_log_entry(
    *,
    intent: ActionIntent,
    interaction: InteractionResult,
    before_capture_id: str,
    after_capture_id: str | None,
    refreshed_capture_id: str | None = None,
) -> dict[str, Any]:
    """把一轮 App Interaction 投影为 Packet 可压缩的 action_log 条目。"""
    status = interaction.status
    if status is InteractionResultStatus.SUCCEEDED:
        result = "success"
    elif status is InteractionResultStatus.EVIDENCE_PENDING:
        result = "evidence_pending"
    else:
        result = "failed"

    entry: dict[str, Any] = {
        "action": intent.action_type.value,
        "result": result,
        "status": status.value,
        "execution_outcome": interaction.execution_outcome.value,
        "verification_outcome": interaction.verification_outcome.value,
        "purpose": intent.purpose,
        "intent_id": intent.intent_id,
        "action_execution_id": interaction.action_execution_id,
        "source_capture_id": before_capture_id,
    }
    if intent.semantic_target:
        entry["selector"] = intent.semantic_target
        entry["semantic_target"] = intent.semantic_target
    candidate_id = intent.parameters.get("candidate_id")
    if isinstance(candidate_id, str) and candidate_id.strip():
        # candidate_id 已由 AppCandidateResolver 绑定到动作前 Capture；这里只投影
        # 已验签身份，不复写坐标或猜测动作后的控件状态。
        entry["target_before"] = {
            "candidate_id": candidate_id,
            "source_capture_id": before_capture_id,
            "semantic_target": intent.semantic_target,
        }
    if after_capture_id:
        entry["result_capture_id"] = after_capture_id
    from services.api.app.application.app_capture_role_supplement import (
        interaction_feedback_capture_set,
        is_interaction_feedback_action,
        merge_evidence_capture_roles,
    )

    entry["evidence_capture_roles"] = merge_evidence_capture_roles(
        primary_capture_id=after_capture_id or before_capture_id,
        before_capture_id=before_capture_id if after_capture_id else None,
        supplemental=(
            intent.parameters.get("evidence_capture_roles")
            if isinstance(intent.parameters.get("evidence_capture_roles"), Mapping)
            else None
        ),
        requirement_role=(
            str(intent.parameters.get("requirement_role")).strip()
            if isinstance(intent.parameters.get("requirement_role"), str)
            else None
        ),
        requirement_capture_id=(
            str(intent.parameters.get("requirement_capture_id")).strip()
            if isinstance(intent.parameters.get("requirement_capture_id"), str)
            else None
        ),
    )
    if is_interaction_feedback_action(
        action=intent.action_type.value,
        semantic_target=intent.semantic_target,
    ):
        primary_id = after_capture_id or before_capture_id
        feedback = interaction_feedback_capture_set(
            before_capture_id=before_capture_id,
            primary_capture_id=primary_id,
            refreshed_capture_id=refreshed_capture_id,
        )
        entry["interaction_feedback"] = feedback
        if feedback["complete"] and feedback["refreshed_capture_id"]:
            roles = dict(entry["evidence_capture_roles"])
            roles["refreshed"] = str(feedback["refreshed_capture_id"])
            entry["evidence_capture_roles"] = roles
        else:
            entry["gap_codes"] = list(feedback["gap_codes"])
    if (
        intent.parameters.get("capture_ad_landing_evidence") is True
        and intent.parameters.get("ad_landing_capability") == "ad_outbound"
        and intent.parameters.get("app_ad_landing_contract")
        == _APP_AD_LANDING_CONTRACT_VERSION
    ):
        entry["ad_navigation_evidence"] = {
            "schema_version": _APP_AD_LANDING_SCHEMA_VERSION,
            "status": "pending",
            "navigation_mode": "external_browser",
            "pair_complete": False,
            "source": {
                "capture_id": before_capture_id,
                "screenshot_ref": "ad_landing_consistency#frame-0",
            },
            "landing": {
                "capture_id": after_capture_id,
                "screenshot_ref": "ad_landing_consistency#frame-1",
            },
        }
    if interaction.error_code:
        entry["error_code"] = interaction.error_code
    # 有则写，禁止编造 target_before/target_after
    summary = dict(interaction.state_summary or {})
    script_recording = summary.get("script_recording")
    if isinstance(script_recording, Mapping):
        entry["script_recording"] = {
            "verified": script_recording.get("verified") is True,
            "shots": list(script_recording.get("shots") or []),
            "operations": list(script_recording.get("operations") or []),
        }
    for key in ("target_before", "target_after", "transition_kind", "navigation"):
        value = summary.get(key)
        if value is not None:
            entry[key] = value
    diff = dict(interaction.state_diff or {})
    if diff:
        entry["state_diff"] = {
            key: value
            for key, value in diff.items()
            if key in {"package_name", "activity", "url", "page_changed"}
        }
    return entry


def is_app_surface(surface_kind: SurfaceKind) -> bool:
    return surface_kind in _APP_SURFACES


class AppActionEvidenceWriter(Protocol):
    """把 action_log 合并写入 after_action Capture metadata。"""

    async def attach_action_log(
        self,
        *,
        task_id: str,
        after_capture_id: str,
        before_capture_id: str | None = None,
        entry: Mapping[str, Any],
    ) -> None: ...


class AppEvidenceCaptureRoleResolver(Protocol):
    """从 Capture 父子关系推导 Judge 角色映射。"""

    async def resolve(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> dict[str, str]: ...


class PostgresAppActionEvidenceStore:
    """PostgreSQL 实现：合并 action_log，并解析 before/primary 角色。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def attach_action_log(
        self,
        *,
        task_id: str,
        after_capture_id: str,
        before_capture_id: str | None = None,
        entry: Mapping[str, Any],
    ) -> None:
        action_execution_id = entry.get("action_execution_id")
        if (
            not isinstance(action_execution_id, str)
            or not action_execution_id.strip()
            or action_execution_id != action_execution_id.strip()
        ):
            raise ValueError("App action_log 缺少 action_execution_id")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            if before_capture_id is None:
                raise ValueError("App action_log 缺少 before_capture_id")
            pair = await connection.fetchrow(
                """
                SELECT
                    before_app.image_signature AS before_image_signature,
                    after_app.image_signature AS after_image_signature,
                    before_app.package_name AS before_package_name,
                    after_app.package_name AS after_package_name,
                    before_app.activity AS before_activity,
                    after_app.activity AS after_activity,
                    before_capture.metadata->>'page_type' AS before_page_type,
                    after_capture.metadata->>'page_type' AS after_page_type,
                    after_capture.metadata#>>'{outbound_landing,url}' AS after_landing_url,
                    after_capture.metadata#>'{outbound_landing,url_candidates}'
                        AS after_landing_url_candidates,
                    (
                        SELECT artifact.id::text
                        FROM qc_evidence_artifact AS artifact
                        WHERE artifact.task_id = before_capture.task_id
                          AND artifact.page_capture_id = before_capture.id
                          AND artifact.artifact_type = 'screenshot'
                          AND artifact.source IN ('baseline', 'app_runtime')
                        ORDER BY artifact.created_at, artifact.id
                        LIMIT 1
                    ) AS before_screenshot_artifact_id,
                    (
                        SELECT artifact.id::text
                        FROM qc_evidence_artifact AS artifact
                        WHERE artifact.task_id = after_capture.task_id
                          AND artifact.page_capture_id = after_capture.id
                          AND artifact.artifact_type = 'screenshot'
                          AND artifact.source IN ('baseline', 'app_runtime')
                        ORDER BY artifact.created_at, artifact.id
                        LIMIT 1
                    ) AS after_screenshot_artifact_id
                FROM qc_capture AS after_capture
                INNER JOIN qc_capture AS before_capture
                  ON before_capture.id = after_capture.parent_capture_id
                 AND before_capture.task_id = after_capture.task_id
                 AND before_capture.agent_run_id = after_capture.agent_run_id
                 AND before_capture.action_execution_id
                     = after_capture.action_execution_id
                INNER JOIN qc_app_capture AS before_app
                  ON before_app.capture_id = before_capture.id
                INNER JOIN qc_app_capture AS after_app
                  ON after_app.capture_id = after_capture.id
                WHERE after_capture.id = $1::uuid
                  AND after_capture.task_id = $2::uuid
                  AND before_capture.id = $3::uuid
                  AND after_capture.action_execution_id = $4
                  AND before_capture.capture_role = 'before_action'
                  AND after_capture.capture_role = 'after_action'
                  AND before_capture.capture_status = 'captured'
                  AND after_capture.capture_status = 'captured'
                """,
                after_capture_id,
                task_id,
                before_capture_id,
                action_execution_id,
            )
            if pair is None:
                raise RuntimeError("App action_log 写入失败：前后 Capture 证据对不存在")
            enriched_entry = _enrich_app_action_pair(dict(entry), pair)
            entry_json = json.dumps([enriched_entry], ensure_ascii=False)
            updated = await connection.fetchval(
                """
                UPDATE qc_capture AS capture
                SET metadata = jsonb_set(
                    COALESCE(capture.metadata, '{}'::jsonb),
                    '{action_log}',
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(
                                COALESCE(capture.metadata->'action_log', '[]'::jsonb)
                            ) AS item
                            WHERE item->>'action_execution_id' = $4
                        ) THEN COALESCE(
                            capture.metadata->'action_log', '[]'::jsonb
                        )
                        ELSE COALESCE(
                            capture.metadata->'action_log', '[]'::jsonb
                        ) || $3::jsonb
                    END,
                    true
                )
                WHERE capture.id = $1::uuid
                  AND capture.task_id = $2::uuid
                  AND capture.capture_role = 'after_action'
                  AND capture.capture_status = 'captured'
                  AND capture.action_execution_id = $4
                  AND ($5::uuid IS NULL OR capture.parent_capture_id = $5::uuid)
                RETURNING capture.id::text
                """,
                after_capture_id,
                task_id,
                entry_json,
                action_execution_id,
                before_capture_id,
            )
            if updated is None:
                raise RuntimeError("App action_log 写入失败：after_action Capture 不存在")
            if before_capture_id:
                before_updated = await connection.fetchval(
                    """
                    UPDATE qc_capture AS capture
                    SET metadata = jsonb_set(
                        COALESCE(capture.metadata, '{}'::jsonb),
                        '{action_log}',
                        CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM jsonb_array_elements(
                                    COALESCE(
                                        capture.metadata->'action_log', '[]'::jsonb
                                    )
                                ) AS item
                                WHERE item->>'action_execution_id' = $4
                            ) THEN COALESCE(
                                capture.metadata->'action_log', '[]'::jsonb
                            )
                            ELSE COALESCE(
                                capture.metadata->'action_log', '[]'::jsonb
                            ) || $3::jsonb
                        END,
                        true
                    )
                    WHERE capture.id = $1::uuid
                      AND capture.task_id = $2::uuid
                      AND capture.capture_role = 'before_action'
                      AND capture.capture_status = 'captured'
                      AND capture.action_execution_id = $4
                      AND EXISTS (
                          SELECT 1
                          FROM qc_capture AS after_capture
                          WHERE after_capture.id = $5::uuid
                            AND after_capture.task_id = capture.task_id
                            AND after_capture.parent_capture_id = capture.id
                            AND after_capture.action_execution_id
                                = capture.action_execution_id
                            AND after_capture.capture_role = 'after_action'
                            AND after_capture.capture_status = 'captured'
                      )
                    RETURNING capture.id::text
                    """,
                    before_capture_id,
                    task_id,
                    entry_json,
                    action_execution_id,
                    after_capture_id,
                )
                if before_updated is None:
                    raise RuntimeError(
                        "App action_log 写入失败：before_action Capture 归属无效"
                    )

    async def resolve(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> dict[str, str]:
        """after_action 配对 before_action → before/primary；否则仅 primary。"""
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    child.id::text AS capture_id,
                    child.capture_role AS capture_role,
                    parent.id::text AS parent_capture_id,
                    parent.capture_role AS parent_capture_role
                FROM qc_capture AS child
                LEFT JOIN qc_capture AS parent
                  ON parent.id = child.parent_capture_id
                 AND parent.task_id = child.task_id
                WHERE child.id = $1::uuid
                  AND child.task_id = $2::uuid
                """,
                page_capture_id,
                task_id,
            )
            refreshed_id = ""
            if row is not None:
                parent_id = row["parent_capture_id"]
                parent_role = str(row["parent_capture_role"] or "")
                child_role = str(row["capture_role"] or "")
                if (
                    child_role == "after_action"
                    and parent_id
                    and parent_role == "before_action"
                    and str(parent_id) != str(row["capture_id"])
                ):
                    refreshed_row = await connection.fetchrow(
                        """
                        SELECT id::text AS capture_id
                        FROM qc_capture
                        WHERE task_id = $2::uuid
                          AND parent_capture_id = $1::uuid
                          AND capture_role = 'observation'
                          AND capture_status = 'captured'
                          AND COALESCE(metadata->>'requirement_role', '') = 'refreshed'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        page_capture_id,
                        task_id,
                    )
                    if refreshed_row is not None:
                        refreshed_id = str(refreshed_row["capture_id"] or "")
        if row is None:
            return {"primary": page_capture_id}
        roles = {"primary": str(row["capture_id"])}
        parent_id = row["parent_capture_id"]
        parent_role = str(row["parent_capture_role"] or "")
        child_role = str(row["capture_role"] or "")
        if (
            child_role == "after_action"
            and parent_id
            and parent_role == "before_action"
            and str(parent_id) != str(row["capture_id"])
        ):
            roles["before"] = str(parent_id)
        if refreshed_id and refreshed_id not in {roles["primary"], roles.get("before")}:
            roles["refreshed"] = refreshed_id
        return roles


def _enrich_app_action_pair(
    entry: dict[str, Any],
    pair: Mapping[str, Any],
) -> dict[str, Any]:
    """用已落库的 App 前后 Capture 生成有界状态差异，禁止只凭 tap 成功过规则。"""

    enriched = dict(entry)
    if str(enriched.get("action") or "") not in {"click", "tap", "back"}:
        return enriched
    before_signature = str(pair.get("before_image_signature") or "")
    after_signature = str(pair.get("after_image_signature") or "")
    before_package = str(pair.get("before_package_name") or "")
    after_package = str(pair.get("after_package_name") or "")
    before_activity = str(pair.get("before_activity") or "")
    after_activity = str(pair.get("after_activity") or "")
    before_page_type = str(pair.get("before_page_type") or "")
    after_page_type = str(pair.get("after_page_type") or "")
    state_diff = {
        "image_changed": bool(
            before_signature
            and after_signature
            and before_signature != after_signature
        ),
        "package_changed": before_package != after_package,
        "activity_changed": before_activity != after_activity,
        "page_type_changed": bool(
            before_page_type
            and after_page_type
            and before_page_type != after_page_type
        ),
    }
    state_diff["page_changed"] = any(state_diff.values())
    enriched["state_diff"] = state_diff
    if state_diff["page_changed"]:
        enriched["verification_outcome"] = "verified"
    else:
        enriched["result"] = "no_effect"
        enriched["verification_outcome"] = "no_effect"
    if isinstance(enriched.get("ad_navigation_evidence"), Mapping):
        enriched["ad_navigation_evidence"] = _build_app_ad_navigation_evidence(
            entry=enriched,
            pair=pair,
        )
    return enriched


def _build_app_ad_navigation_evidence(
    *,
    entry: Mapping[str, Any],
    pair: Mapping[str, Any],
) -> dict[str, Any]:
    """把同一次 App 点击前后截图绑定成广告源页/浏览器落地页证据。"""

    from services.api.app.application.app_evidence_role_contract import (
        ROLE_AD_OUTBOUND_LANDING,
        ROLE_AD_OUTBOUND_SOURCE,
        build_ad_outbound_triplet_evidence,
    )

    before_capture_id = str(entry.get("source_capture_id") or "")
    after_capture_id = str(entry.get("result_capture_id") or "")
    before_artifact_id = str(pair.get("before_screenshot_artifact_id") or "")
    after_artifact_id = str(pair.get("after_screenshot_artifact_id") or "")
    before_package = str(pair.get("before_package_name") or "")
    after_package = str(pair.get("after_package_name") or "")
    after_activity = str(pair.get("after_activity") or "")
    browser_opened = _is_android_browser_package(after_package)
    package_changed = bool(after_package and before_package and after_package != before_package)
    pair_complete = bool(
        str(entry.get("action") or "") in {"click", "tap"}
        and before_capture_id
        and after_capture_id
        and before_artifact_id
        and after_artifact_id
        and before_package
        and package_changed
        and browser_opened
    )
    # 最小可用：包名已切到外部（含非浏览器）也标落地角色，供 ops_6／外跳规则消费截图
    landing_observed = bool(
        before_capture_id
        and after_capture_id
        and package_changed
        and after_package
    )
    landing_url = _resolve_outbound_landing_url(pair)
    evidence: dict[str, Any] = {
        "schema_version": _APP_AD_LANDING_SCHEMA_VERSION,
        "status": "observed" if pair_complete else (
            "partial" if landing_observed else "evidence_insufficient"
        ),
        "navigation_mode": "external_browser" if browser_opened else (
            "external_activity" if landing_observed else "unknown"
        ),
        "pair_complete": pair_complete,
        "source": {
            "capture_id": before_capture_id,
            "artifact_id": before_artifact_id,
            "package_name": before_package,
            "activity": str(pair.get("before_activity") or ""),
            "page_type": str(pair.get("before_page_type") or ""),
            "screenshot_ref": "ad_landing_consistency#frame-0",
            "evidence_role": ROLE_AD_OUTBOUND_SOURCE,
        },
        "landing": {
            "capture_id": after_capture_id if landing_observed else "",
            "artifact_id": after_artifact_id if landing_observed else "",
            "package_name": after_package if landing_observed else "",
            "activity": after_activity if landing_observed else "",
            "screenshot_ref": "ad_landing_consistency#frame-1",
            "semantic_anchors": [],
            "evidence_role": ROLE_AD_OUTBOUND_LANDING if landing_observed else None,
            "counts_as_business_coverage": False,
        },
        "app_evidence_role": build_ad_outbound_triplet_evidence(
            source_capture_id=before_capture_id,
            landing_capture_id=after_capture_id if landing_observed else None,
            external_activity_kind=(
                "browser" if browser_opened else ("other" if landing_observed else None)
            ),
            landing_url=landing_url,
        ),
    }
    if landing_url and landing_observed:
        evidence["landing"]["url"] = landing_url
    if not pair_complete:
        evidence["reason"] = (
            "动作后未形成目标 App 广告源图与 Android 浏览器落地页图的完整证据对"
            if not landing_observed
            else "已观察到包名外跳落地截图，但未确认为浏览器包；已标记 ad_outbound_landing"
        )
    return evidence


def _resolve_outbound_landing_url(pair: Mapping[str, Any]) -> str | None:
    """从 after Capture 元数据取真实落地 URL；无则空，不 invent。"""

    direct = str(pair.get("after_landing_url") or "").strip()
    if direct.startswith(("http://", "https://")):
        return direct[:2048]
    raw_candidates = pair.get("after_landing_url_candidates")
    if isinstance(raw_candidates, str) and raw_candidates.strip().startswith("["):
        try:
            raw_candidates = json.loads(raw_candidates)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_candidates = None
    if isinstance(raw_candidates, Sequence) and not isinstance(
        raw_candidates, (str, bytes, bytearray)
    ):
        for item in raw_candidates:
            text = str(item or "").strip()
            if text.startswith(("http://", "https://")):
                return text[:2048]
    return None


def _is_android_browser_package(package_name: str) -> bool:
    normalized = str(package_name or "").strip().casefold()
    return normalized in _ANDROID_BROWSER_PACKAGES or bool(
        normalized
        and normalized != "com.android.browser.provider"
        and any(marker in normalized for marker in ("browser", "chrome", "firefox"))
    )


async def attach_app_action_log_safely(
    writer: AppActionEvidenceWriter | None,
    *,
    intent: ActionIntent,
    interaction: InteractionResult,
    before_capture_id: str,
    after_capture_id: str,
    refreshed_capture_id: str | None = None,
) -> None:
    """协调成功后旁路写入；失败只告警，不回滚已完成的动作轮。"""
    if writer is None or not is_app_surface(intent.surface_kind):
        return
    try:
        entry = build_app_action_log_entry(
            intent=intent,
            interaction=interaction,
            before_capture_id=before_capture_id,
            after_capture_id=after_capture_id,
            refreshed_capture_id=refreshed_capture_id,
        )
        await writer.attach_action_log(
            task_id=intent.task_id,
            after_capture_id=after_capture_id,
            before_capture_id=before_capture_id,
            entry=entry,
        )
    except Exception:  # noqa: BLE001 - 操作日志旁路不得阻断探索
        logger.warning(
            "task_id=%s after_capture=%s App action_log 写入失败",
            intent.task_id,
            after_capture_id,
            exc_info=True,
        )
