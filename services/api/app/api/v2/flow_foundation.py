"""流程基建只读 API：flows／observations／flow-links（P5.1）。"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from services.api.app.api.v1.object_access import require_task_read_access
from services.api.app.application.flow_foundation_runtime import (
    AppFlowFoundationRuntime,
    build_app_flow_foundation_runtime,
)
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.flow_foundation_repository import (
    PostgresFlowFoundationRepository,
)

router = APIRouter()


@lru_cache
def _get_runtime() -> AppFlowFoundationRuntime:
    return build_app_flow_foundation_runtime(repo=PostgresFlowFoundationRepository())


@router.get("/tasks/{task_id}/flows")
async def list_task_flows(
    task_id: UUID,
    scope: Annotated[AccessScope, Depends(require_task_read_access)],
    runtime: Annotated[AppFlowFoundationRuntime, Depends(_get_runtime)],
):
    del scope
    try:
        return {"items": await runtime.list_flows_for_task(task_id=str(task_id))}
    except Exception as exc:  # noqa: BLE001 — 空库／缺表时仍返回 200 空列表
        if _is_empty_store_error(exc):
            return {"items": []}
        raise


@router.get("/tasks/{task_id}/flows/{phase_run_id}")
async def get_task_flow(
    task_id: UUID,
    phase_run_id: str,
    scope: Annotated[AccessScope, Depends(require_task_read_access)],
    runtime: Annotated[AppFlowFoundationRuntime, Depends(_get_runtime)],
):
    del scope
    try:
        row = await runtime.list_flow(task_id=str(task_id), phase_run_id=phase_run_id)
    except Exception as exc:  # noqa: BLE001
        if _is_empty_store_error(exc):
            raise AliothError(message="流程实例不存在", status_code=404) from exc
        raise
    if row is None:
        raise AliothError(message="流程实例不存在", status_code=404)
    return row


@router.get("/tasks/{task_id}/flows/{phase_run_id}/observations")
async def list_flow_observations(
    task_id: UUID,
    phase_run_id: str,
    scope: Annotated[AccessScope, Depends(require_task_read_access)],
    runtime: Annotated[AppFlowFoundationRuntime, Depends(_get_runtime)],
):
    del scope
    try:
        return {
            "items": await runtime.list_observations_for_phase(
                task_id=str(task_id),
                phase_run_id=phase_run_id,
            )
        }
    except Exception as exc:  # noqa: BLE001
        if _is_empty_store_error(exc):
            return {"items": []}
        raise


@router.get("/tasks/{task_id}/captures/{capture_id}/flow-links")
async def list_capture_flow_links(
    task_id: UUID,
    capture_id: str,
    scope: Annotated[AccessScope, Depends(require_task_read_access)],
    runtime: Annotated[AppFlowFoundationRuntime, Depends(_get_runtime)],
):
    del scope
    try:
        return {
            "items": await runtime.list_flow_links_for_capture(
                task_id=str(task_id),
                capture_id=capture_id,
            )
        }
    except Exception as exc:  # noqa: BLE001
        if _is_empty_store_error(exc):
            return {"items": []}
        raise


def _is_empty_store_error(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(
        token in text
        for token in (
            "does not exist",
            "undefinedtable",
            "relation",
            "qc_flow_",
        )
    )
