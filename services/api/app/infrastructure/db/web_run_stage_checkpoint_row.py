"""Web checkpoint 完整数据库行与不可变领域游标互转。"""

import json
from collections.abc import Mapping
from dataclasses import asdict, fields

from services.api.app.domain.exploration.web_run_stage_checkpoint import (
    WebRunStage,
    WebStageCursor,
    WebStageGap,
    WebStageStatus,
)

_JSON_FIELDS = {
    "cursor_payload",
    "last_error_detail",
    "gaps",
    "gap_codes",
    "artifact_ids",
    "obstruction_attempts",
}


def web_run_stage_checkpoint_values(cursor: WebStageCursor) -> dict[str, object]:
    values = {item.name: getattr(cursor, item.name) for item in fields(cursor)}
    values["stage_status"] = values.pop("status").value
    values["current_stage"] = cursor.current_stage.value
    values["cursor_payload"] = dict(cursor.cursor_payload)
    values["last_error_detail"] = dict(cursor.last_error_detail)
    values["obstruction_attempts"] = dict(cursor.obstruction_attempts)
    values["gaps"] = [asdict(gap) for gap in cursor.gaps]
    values["gap_codes"] = list(cursor.gap_codes)
    for key in _JSON_FIELDS:
        values[key] = json.dumps(values[key], ensure_ascii=False)
    return values


def parse_web_run_stage_checkpoint_row(row: Mapping[str, object]) -> WebStageCursor:
    values = {item.name: row[item.name] for item in fields(WebStageCursor) if item.name != "status"}
    for key in _JSON_FIELDS - {"gap_codes"}:
        value = values[key]
        values[key] = json.loads(value) if isinstance(value, str) else value
    values["run_id"] = str(values["run_id"])
    values["claim_token"] = str(values["claim_token"]) if values["claim_token"] else None
    values["current_stage"] = WebRunStage(values["current_stage"])
    values["status"] = WebStageStatus(row["stage_status"])
    values["gaps"] = tuple(
        WebStageGap(**{**item, "stage": WebRunStage(item["stage"])}) for item in values["gaps"]
    )
    cursor = WebStageCursor(**values)
    gap_codes = row["gap_codes"]
    if isinstance(gap_codes, str):
        gap_codes = json.loads(gap_codes)
    if tuple(gap_codes) != cursor.gap_codes:
        raise ValueError("checkpoint gap_codes 与结构化 gap 不一致")
    return cursor
