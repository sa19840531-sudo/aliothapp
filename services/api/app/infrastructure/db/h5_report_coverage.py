"""按 Gate 实际消费的身份读取 H5 Coverage，供两条报告绑定入口共用。"""

import json
from typing import Any

from services.api.app.application.h5_report_coverage import validated_h5_report_coverage


async def load_h5_report_coverage(
    connection: Any, *, task_id: str, agent_run_id: str,
    snapshot_id: str | None, snapshot_hash: str | None, snapshot_epoch: int | None,
) -> dict[str, Any]:
    """缺少绑定或内容失配时拒绝；不猜测历史 Epoch 或最新修订。"""

    row = await connection.fetchrow(
        """
        SELECT coverage_snapshot_id, task_id::text AS task_id,
               agent_run_id::text AS agent_run_id, finalization_epoch,
               revision, authority_source, required_page_types, covered_page_types,
               missing_page_types, coverage_scopes AS scopes,
               assessment_scope_ready, frontier_terminal, completion_ready,
               schema_version, payload_hash
        FROM qc_h5_coverage_snapshot
        WHERE task_id = $1::uuid AND agent_run_id = $2::uuid
          AND coverage_snapshot_id = $3 AND payload_hash = $4
          AND finalization_epoch = $5
        """,
        task_id, agent_run_id, snapshot_id, snapshot_hash, snapshot_epoch,
    )
    if row is None:
        raise ValueError("缺少 Gate 绑定的 H5 Coverage Snapshot")
    coverage = dict(row)
    for key in ("required_page_types", "covered_page_types", "missing_page_types", "scopes"):
        if isinstance(coverage[key], str):
            coverage[key] = json.loads(coverage[key])
    return validated_h5_report_coverage(coverage, task_id=task_id, agent_run_id=agent_run_id)
