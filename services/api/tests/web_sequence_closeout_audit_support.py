"""只导出本用例随机租户的失败现场，不修改被审计库存或门禁。"""

import json
import os
from pathlib import Path


def export_sequence_formal_html(task_id, content):
    directory = os.getenv("ALIOTH_TEST_SEQUENCE_AUDIT_DIR")
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        with (path / f"{task_id}.html").open("x", encoding="utf-8") as stream:
            stream.write(content)


async def export_sequence_closeout_case(case):
    directory = os.getenv("ALIOTH_TEST_SEQUENCE_AUDIT_DIR")
    if not directory:
        return
    connection = case.connection
    tasks = await connection.fetch(
        "SELECT id FROM qc_task WHERE tenant_id=$1::uuid",
        case.tenant_id,
    )
    ids = [row["id"] for row in tasks]
    result = {"tenant_id": case.tenant_id, "tasks": [str(value) for value in ids]}
    tables = (
        "qc_task",
        "qc_agent_run",
        "qc_v2_work_item",
        "qc_surface_session",
        "qc_capture",
        "qc_page_capture",
        "qc_action_execution",
        "qc_evidence_manifest",
        "qc_evidence_artifact",
        "qc_web_run_policy_snapshot",
        "qc_web_evidence_sequence",
        "qc_web_evidence_sequence_member",
        "qc_h5_inspection_publication",
        "qc_h5_inspection_publication_rule",
        "qc_h5_inspection_publication_work_item",
        "qc_inspection_work_item",
        "qc_inspection_work_item_rule",
        "qc_inspection_attempt",
        "qc_evidence_input_set",
        "qc_evidence_input_set_member",
        "qc_completion_gate_snapshot",
        "qc_report_gate_binding",
        "qc_rule_execution",
        "qc_web_coverage_snapshot",
    )
    for table in tables:
        columns = {
            row["column_name"]
            for row in await connection.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=$1",
                table,
            )
        }
        if table == "qc_task":
            condition = "id=ANY($1::uuid[])"
        elif "task_id" in columns:
            condition = "task_id=ANY($1::uuid[])"
        elif "publication_id" in columns:
            condition = "publication_id IN (SELECT id FROM qc_h5_inspection_publication "
            condition += "WHERE task_id=ANY($1::uuid[]))"
        elif "work_item_id" in columns:
            condition = "work_item_id IN (SELECT id FROM qc_inspection_work_item "
            condition += "WHERE task_id=ANY($1::uuid[]))"
        else:
            raise AssertionError(f"失败现场导出缺少归属条件：{table}")
        result[table] = [
            dict(row)
            for row in await connection.fetch(
                f'SELECT * FROM "{table}" WHERE {condition}',
                ids,
            )
        ]
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    with (path / f"{case.tenant_id}.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, default=str)
