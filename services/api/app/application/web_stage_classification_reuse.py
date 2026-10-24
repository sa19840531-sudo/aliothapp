"""阶段分类复用只认可持久原材料，原帧与本次动作帧始终分开。"""

from dataclasses import replace

from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage


async def validate_reused_classification(evidence, cursor, result):
    if result is None or result.reused_capture_id is None:
        return None
    if result.cursor_payload.get("capture_id") != cursor.cursor_payload.get("capture_id"):
        raise WebStageFailure("WEB_CLASSIFICATION_CAPTURE_MISMATCH")
    reader = getattr(evidence, "read_reused_page_type_assessment", None)
    facts = (
        await reader(
            capture_id=cursor.cursor_payload["capture_id"],
            finalization_epoch=cursor.cursor_payload["finalization_epoch"],
            reused_capture_id=result.reused_capture_id,
            reused_logical_page_id=result.reused_logical_page_id,
        )
        if callable(reader)
        else None
    )
    if facts is None:
        raise WebStageFailure("WEB_CLASSIFICATION_REUSE_INVALID")
    return facts


async def read_reused_classification(evidence, cursor):
    reader = getattr(evidence, "find_completed", None)
    if not callable(reader):
        return None
    result = await reader(
        replace(cursor, current_stage=WebRunStage.CLASSIFICATION), operation="classification"
    )
    facts = await validate_reused_classification(evidence, cursor, result)
    return (result, facts) if facts is not None else None
