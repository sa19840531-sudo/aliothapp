"""App Journey 执行桥：把 P1-4 与 F01 短流程种子接到执行计划。

探索稳定后（home 到达且弹层满足）为任务锁定 Journey 执行计划；
F01 播放器／支付短流程使用 Run 场景计划（可同时锁定多份），
由 app_shortflow_driver 推进检查点并写回执。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)
_SEED_PATH = (
    Path(__file__).resolve().parents[4]
    / "docs"
    / "data"
    / "journey_definitions"
    / "p1_4_journeys.json"
)
_SHORTFLOW_SEED_PATH = (
    Path(__file__).resolve().parents[4]
    / "docs"
    / "data"
    / "journey_definitions"
    / "app_shortflows_v2.json"
)
APP_JOURNEY_PLAN_META_KEY = "journey_execution_plan_id"
APP_JOURNEY_EXECUTION_META_KEY = "journey_execution_id"
APP_JOURNEY_DEFINITION_META_KEY = "journey_definition_id"
APP_SHORTFLOW_DEFINITION_IDS: tuple[str, ...] = (
    "app-player-pause-resume",
    "app-payment-display-return",
)


def load_p1_4_android_journey_ids(
    *,
    path: Path | None = None,
) -> tuple[str, ...]:
    """读取种子中声明 android 的 definition_id 列表（稳定顺序）。"""

    seed = path or _SEED_PATH
    if not seed.is_file():
        return ()
    try:
        raw = json.loads(seed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, list):
        return ()
    ordered: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        definition_id = str(item.get("definition_id") or "").strip()
        if not definition_id or definition_id in seen:
            continue
        surfaces = item.get("surface_kinds")
        if isinstance(surfaces, (list, tuple)):
            kinds = {str(s).strip().casefold() for s in surfaces}
            if kinds and "android" not in kinds and "app" not in kinds:
                continue
        seen.add(definition_id)
        ordered.append(definition_id)
    return tuple(ordered)


def load_app_shortflow_definition_ids(
    *,
    path: Path | None = None,
) -> tuple[str, ...]:
    """读取 App F01 短流程 definition_id（播放器暂停恢复 + 支付只读返回）。"""

    seed = path or _SHORTFLOW_SEED_PATH
    if not seed.is_file():
        return APP_SHORTFLOW_DEFINITION_IDS
    try:
        raw = json.loads(seed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return APP_SHORTFLOW_DEFINITION_IDS
    if not isinstance(raw, list):
        return APP_SHORTFLOW_DEFINITION_IDS
    ordered: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        definition_id = str(item.get("definition_id") or "").strip()
        if not definition_id or definition_id in seen:
            continue
        surfaces = item.get("surface_kinds")
        if isinstance(surfaces, (list, tuple)):
            kinds = {str(s).strip().casefold() for s in surfaces}
            if kinds and "android" not in kinds and "ios" not in kinds and "app" not in kinds:
                continue
        seen.add(definition_id)
        ordered.append(definition_id)
    return tuple(ordered) or APP_SHORTFLOW_DEFINITION_IDS


async def ensure_app_journey_execution_plans(
    *,
    service: Any,
    task_id: str,
    definition_ids: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """为任务锁定一份 P1-4 Journey 执行计划（任务级唯一）；已有计划则复用。"""

    ids = tuple(definition_ids or load_p1_4_android_journey_ids())
    locked: list[dict[str, str]] = []
    for definition_id in ids:
        try:
            latest = await service._repository.get_latest(definition_id=definition_id)
            if latest is None:
                _LOGGER.info(
                    "app_journey_definition_missing",
                    extra={"definition_id": definition_id, "task_id": task_id},
                )
                continue
            plan = await service.create_execution_plan(
                task_id=task_id,
                definition_id=latest.definition_id,
                version=latest.version,
                expected_content_hash=latest.content_hash,
            )
            locked.append(
                {
                    "definition_id": latest.definition_id,
                    "execution_plan_id": plan.execution_plan_id,
                    "version": str(latest.version),
                    "plan_kind": "p1_4",
                }
            )
            # 任务级计划唯一：首个成功锁定后停止，避免冲突。
            break
        except Exception as exc:  # noqa: BLE001 - 单个 Journey 失败不得阻断探索
            _LOGGER.warning(
                "app_journey_plan_lock_failed",
                extra={
                    "definition_id": definition_id,
                    "task_id": task_id,
                    "error": str(exc),
                },
            )
    return locked


async def ensure_app_shortflow_execution_plans(
    *,
    service: Any,
    task_id: str,
    agent_run_id: str,
    definition_ids: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """为当前 Run 锁定 F01 播放器／支付短流程计划（可同时多份）。"""

    run_id = str(agent_run_id or "").strip()
    if not run_id:
        return []
    ids = tuple(definition_ids or load_app_shortflow_definition_ids())
    locked: list[dict[str, str]] = []
    for definition_id in ids:
        try:
            latest = await service._repository.get_latest(definition_id=definition_id)
            if latest is None:
                _LOGGER.info(
                    "app_shortflow_definition_missing",
                    extra={"definition_id": definition_id, "task_id": task_id},
                )
                continue
            scenario_key = f"app-shortflow:{definition_id}"
            plan = await service.create_run_execution_plan(
                task_id=task_id,
                agent_run_id=run_id,
                definition_id=latest.definition_id,
                version=latest.version,
                scenario_key=scenario_key,
                parameters={"side_effect_policy": "read_only", "f01": True},
                expected_content_hash=latest.content_hash,
            )
            locked.append(
                {
                    "definition_id": latest.definition_id,
                    "execution_plan_id": plan.execution_plan_id,
                    "version": str(latest.version),
                    "scenario_key": scenario_key,
                    "plan_kind": "app_shortflow",
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单个短流程失败不得阻断探索
            _LOGGER.warning(
                "app_shortflow_plan_lock_failed",
                extra={
                    "definition_id": definition_id,
                    "task_id": task_id,
                    "agent_run_id": run_id,
                    "error": str(exc),
                },
            )
    return locked


def attach_journey_plan_to_coverage(
    coverage_metadata: Mapping[str, Any] | None,
    *,
    plans: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    """把已锁定计划写入覆盖元数据，供规则／报表与短流程驱动读取。"""

    out = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    if not plans:
        return out
    existing = [
        dict(item)
        for item in (out.get("journey_execution_plans") or ())
        if isinstance(item, Mapping)
    ]
    by_plan = {
        str(item.get("execution_plan_id") or ""): dict(item)
        for item in existing
        if str(item.get("execution_plan_id") or "")
    }
    for item in plans:
        plan_id = str(item.get("execution_plan_id") or "")
        if not plan_id:
            continue
        by_plan[plan_id] = dict(item)
    merged = list(by_plan.values())
    out["journey_execution_plans"] = merged
    out["app_journey_plans_ready"] = True
    # 主指针优先短流程播放器，否则第一份计划（兼容旧读侧）
    primary = next(
        (
            item
            for item in merged
            if str(item.get("definition_id") or "") == "app-player-pause-resume"
        ),
        merged[0],
    )
    out[APP_JOURNEY_PLAN_META_KEY] = str(primary.get("execution_plan_id") or "")
    out[APP_JOURNEY_DEFINITION_META_KEY] = str(primary.get("definition_id") or "")
    shortflows = [
        item for item in merged if str(item.get("plan_kind") or "") == "app_shortflow"
    ]
    if shortflows:
        out["app_shortflow_plans_ready"] = True
        out["app_shortflow_plan_ids"] = [
            str(item.get("execution_plan_id") or "")
            for item in shortflows
            if item.get("execution_plan_id")
        ]
    return out


def flow_ready_for_app_journey(flow_contract: Mapping[str, Any] | None) -> bool:
    """首页到达且弹层满足后才允许跑 Journey（避免在广告墙上执行）。"""

    if not isinstance(flow_contract, Mapping):
        return False
    if flow_contract.get("home_arrived") is not True:
        return False
    if flow_contract.get("popup_satisfied") is not True:
        return False
    return True
