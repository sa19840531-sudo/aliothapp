"""H5 补证收敛只读取同 Run/Epoch 的实际证据，不使用 URL 粗缓存。"""

import json
from typing import Any

from services.api.app.application.h5_recapture_coalescing import H5GapEvidence, unchanged_gap
from services.api.app.domain.inspection import EvidenceRequest
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.pool import get_postgres_pool


def _mapping(value):
    return json.loads(value) if isinstance(value, str) else dict(value or {})


def _facts(row, *, required_artifacts: tuple[str, ...] = ()) -> H5GapEvidence:
    manifest = _mapping(row["manifest_payload"])
    artifacts = manifest.get("artifacts", [])
    # 只比较合同所需制品，不能让无关 HAR 时钟阻止同缺口收敛。
    relevant = [item for item in artifacts if item.get("artifact_type") != "action_trace"
                and (not required_artifacts or item.get("artifact_type") in required_artifacts)]
    content_hash = ""
    if (relevant and all(item.get("checksum") for item in relevant)
        and set(required_artifacts) <= {item["artifact_type"] for item in relevant}):
        content_hash = stable_protocol_hash(
            sorted(
                (
                    item["artifact_type"],
                    item["checksum"],
                    item.get("size_bytes"),
                    json.dumps(_content_metadata(item), sort_keys=True, ensure_ascii=False),
                )
                for item in relevant
            )
        )
    return H5GapEvidence(
        task_id=row["task_id"],
        agent_run_id=row["agent_run_id"],
        finalization_epoch=row["finalization_epoch"],
        session_id=row["session_id"],
        logical_page_key=row["logical_page_key"] or "",
        state_fingerprint=row["state_fingerprint"] or "",
        contract_hash=stable_protocol_hash(
            {
                "rule": row["contract_hash"],
                "ruleset": row["ruleset_snapshot_hash"],
            }
        )
        if row["contract_hash"] and row["ruleset_snapshot_hash"]
        else "",
        content_hash=content_hash,
        identity_confirmed=row["identity_status"] == "confirmed",
        page_state=row["page_state"],
        is_overlay=row["is_overlay"],
        is_external=row["is_external"],
    )


def _content_metadata(item):
    metadata = dict(item.get("metadata", {}))
    presentation = metadata.get("presentation")
    if isinstance(presentation, dict):
        # 文档时钟是来源标识，不是内容；遮挡、认证、可见性仍参与比较。
        metadata["presentation"] = {key: value for key, value in presentation.items()
                                    if key != "document_epoch_ms"}
    return metadata


class PostgresH5RecaptureCoalescingRepository:
    def __init__(self, *, pool: Any | None = None):
        self._pool = pool

    async def has_unchanged_gap(
        self,
        *,
        request: EvidenceRequest,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> str | None:
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT request.request_id, request.request_payload,
                    request.task_id::text AS task_id, capture.agent_run_id::text AS agent_run_id,
                    request.finalization_epoch, capture.surface_session_id AS session_id,
                    state.logical_page_key, state.state_fingerprint, state.identity_status,
                    state.is_overlay, state.is_external, page.page_state,
                    member.contract_hash, work.ruleset_snapshot_hash, manifest.manifest_payload,
                    failed_source.facts AS failed_source_facts
                FROM qc_evidence_request AS request
                JOIN qc_task AS task ON task.id = request.task_id
                    AND task.target_platform = 'h5' AND task.status = 'running'
                    AND task.finalization_epoch = request.finalization_epoch
                    AND task.finalization_state IN (
                        'open', 'exploration_draining', 'session_draining')
                JOIN qc_capture AS capture ON capture.id = request.target_capture_id
                    AND capture.task_id = task.id AND capture.agent_run_id = $2::uuid
                    AND capture.capture_status = 'captured' AND capture.surface_kind = 'web'
                JOIN qc_page_capture AS page ON page.id = capture.id AND page.task_id = task.id
                JOIN qc_page_graph_state AS state ON state.state_id = capture.id
                    AND state.task_id = task.id AND state.agent_run_id = capture.agent_run_id
                    AND state.finalization_epoch = request.finalization_epoch
                    AND state.surface_session_id = capture.surface_session_id
                    AND state.identity_source = 'logical_surface'
                JOIN qc_inspection_work_item AS work ON work.id = request.work_item_id
                    AND work.task_id = task.id
                    AND work.finalization_epoch = request.finalization_epoch
                JOIN qc_inspection_work_item_rule AS member ON member.work_item_id = work.id
                    AND member.rule_id = request.rule_id AND member.rule_code = request.rule_code
                JOIN LATERAL (
                    SELECT manifest_payload, quality_status FROM qc_evidence_manifest AS manifest
                    WHERE manifest.task_id = task.id AND manifest.capture_id = capture.id
                    ORDER BY manifest.manifest_version DESC LIMIT 1
                ) AS manifest ON manifest.quality_status = 'pass'
                LEFT JOIN LATERAL (
                    SELECT jsonb_build_object(
                        'capture_id', source.id::text,
                        'task_id', request.task_id::text, 'agent_run_id', source.agent_run_id::text,
                        'finalization_epoch', probe.finalization_epoch,
                        'session_id', source.surface_session_id,
                        'logical_page_key', source_state.logical_page_key,
                        'state_fingerprint', source_state.state_fingerprint,
                        'identity_status', source_state.identity_status,
                        'is_overlay', source_state.is_overlay,
                        'is_external', source_state.is_external,
                        'page_state', source_page.page_state,
                        'manifest_payload', source_manifest.manifest_payload
                    ) AS facts
                    FROM qc_v2_work_item AS probe
                    LEFT JOIN qc_action_execution AS action ON action.task_id = task.id
                        AND action.session_id = probe.session_id
                        AND action.finalization_epoch = probe.finalization_epoch
                        AND action.status = 'completed'
                        AND action.intent_payload #>> '{parameters,exploration_work_id}'
                            = probe.work_id
                    JOIN qc_capture AS source ON source.id = CASE
                        WHEN probe.status = 'succeeded' THEN action.result_capture_id
                        ELSE probe.source_capture_id END
                        AND source.task_id = task.id AND source.agent_run_id = $2::uuid
                        AND source.capture_status = 'captured'
                        AND source.surface_kind = 'web'
                        AND source.surface_session_id = probe.session_id
                    JOIN qc_page_capture AS source_page ON source_page.id = source.id
                        AND source_page.task_id = task.id
                    JOIN qc_page_graph_state AS source_state ON source_state.state_id = source.id
                        AND source_state.task_id = task.id AND source_state.agent_run_id = $2::uuid
                        AND source_state.finalization_epoch = probe.finalization_epoch
                        AND source_state.surface_session_id = probe.session_id
                        AND source_state.identity_source = 'logical_surface'
                    JOIN LATERAL (
                        SELECT manifest_payload, quality_status FROM qc_evidence_manifest
                        WHERE task_id = task.id AND capture_id = source.id
                        ORDER BY manifest_version DESC LIMIT 1
                    ) AS source_manifest ON source_manifest.quality_status = 'pass'
                    WHERE probe.task_id = task.id AND probe.evidence_request_id = request.request_id
                        AND probe.work_type = 'probe' AND probe.finalization_epoch = $3
                        AND (probe.status IN ('blocked', 'no_progress') OR (
                            probe.status = 'succeeded' AND action.result_capture_id = source.id
                            AND source.action_execution_id = action.action_execution_id
                            AND source.capture_role = 'probe_result_replay'
                            AND EXISTS (SELECT 1 FROM qc_evidence_supplement AS supplement
                                JOIN qc_evidence_manifest AS attached
                                    ON attached.manifest_id = supplement.manifest_id
                                    AND attached.task_id = task.id
                                    AND attached.capture_id = source.id
                                WHERE supplement.task_id = task.id
                                    AND supplement.request_id = request.request_id
                                    AND supplement.probe_work_id = probe.work_id)))
                        AND probe.last_error_code IS NULL
                        AND probe.terminal_payload->>'status' = probe.status
                    ORDER BY probe.finished_at DESC LIMIT 1
                ) AS failed_source ON true
                WHERE request.task_id = $1::uuid AND request.finalization_epoch = $3
                    AND request.rule_id = $4::uuid AND request.requirement_hash = $5
                    AND (request.request_id = $6 OR (
                        ((request.status = 'unfulfillable' AND request.resolution_reason IN (
                            'h5_probe_terminal:blocked', 'h5_probe_terminal:no_progress'))
                            OR request.status = 'satisfied')
                        AND EXISTS (SELECT 1 FROM qc_v2_work_item AS probe
                            WHERE probe.task_id = task.id
                              AND probe.evidence_request_id = request.request_id
                              AND probe.work_type = 'probe' AND probe.finalization_epoch = $3
                              AND probe.status IN ('blocked', 'no_progress', 'succeeded'))
                    ))
                ORDER BY (request.request_id = $6) DESC, request.created_at DESC LIMIT 33
                """,
                request.task_id,
                agent_run_id,
                finalization_epoch,
                request.rule_id,
                request.requirement_hash,
                request.request_id,
            )
        current = next((row for row in rows if row["request_id"] == request.request_id), None)
        if current is None:
            return None
        current_facts = _facts(current, required_artifacts=request.required_artifact_types)
        failed_sources: dict[str, str] = {}
        for row in rows:
            if row["request_id"] == request.request_id:
                continue
            prior = EvidenceRequest.from_dict(_mapping(row["request_payload"]))
            source = _mapping(row["failed_source_facts"])
            if not source:
                continue
            source.update(
                contract_hash=row["contract_hash"],
                ruleset_snapshot_hash=row["ruleset_snapshot_hash"],
            )
            prior_facts = _facts(row, required_artifacts=prior.required_artifact_types)
            if (
                prior_facts == _facts(source, required_artifacts=prior.required_artifact_types)
                and unchanged_gap(prior, prior_facts, request, current_facts)
            ):
                if source.get("capture_id"):
                    failed_sources[source["capture_id"]] = prior.request_id
                # 两次独立新观察都没有收益才收敛；复制同一帧或单次模型决策不够。
                if len(failed_sources) >= 2:
                    return ",".join(sorted(failed_sources.values()))
        return None
