"""App F01 短流程驱动：冻结计划上的播放器暂停/恢复与支付只读展示返回。

在当前帧候选中选语义动作，经既有 Coordinator 执行；进度与回执写入
observation lineage，供规则／报表串联。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.exploration import ExplorationWork
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
)

APP_SHORTFLOW_PROGRESS_KEY = "app_shortflow_progress"
APP_SHORTFLOW_BINDING_KEY = "app_shortflow_binding"

_PLAYER_PAGES = frozenset({"video_detail", "player", "content_detail"})
_PAYMENT_PAGES = frozenset({"payment", "membership", "wallet", "recharge"})

_PLAYER_PAUSE_LABELS = ("暂停", "pause", "||")
_PLAYER_RESUME_LABELS = ("播放", "继续播放", "play", "▶")
_PAYMENT_PLAN_LABELS = (
    "会员档位",
    "开通会员",
    "连续包月",
    "连续包季",
    "连续包年",
    "月卡",
    "季卡",
    "年卡",
    "套餐",
)
_PAYMENT_BACK_LABELS = ("返回", "关闭", "取消", "暂不开通", "以后再说")


def try_app_shortflow_driver(work: ExplorationWork) -> ActionIntent | None:
    """当覆盖元数据已锁定 App 短流程计划时，推进当前可执行检查点。"""

    coverage = _coverage(work)
    plans = coverage.get("journey_execution_plans")
    if not isinstance(plans, Sequence) or not plans:
        return None
    progress = _progress_map(coverage)
    page = _page_type(work)

    for plan in plans:
        if not isinstance(plan, Mapping):
            continue
        definition_id = str(plan.get("definition_id") or "").strip()
        execution_plan_id = str(plan.get("execution_plan_id") or "").strip()
        if not definition_id or not execution_plan_id:
            continue
        row = progress.get(definition_id)
        if isinstance(row, Mapping) and str(row.get("status") or "") in {
            "succeeded",
            "completed_with_gaps",
            "failed",
            "cancelled",
            "unsupported",
        }:
            continue
        if definition_id == "app-player-pause-resume":
            intent = _drive_player(
                work=work,
                definition_id=definition_id,
                execution_plan_id=execution_plan_id,
                progress_row=row if isinstance(row, Mapping) else {},
                page=page,
            )
            if intent is not None:
                return intent
        elif definition_id == "app-payment-display-return":
            intent = _drive_payment(
                work=work,
                definition_id=definition_id,
                execution_plan_id=execution_plan_id,
                progress_row=row if isinstance(row, Mapping) else {},
                page=page,
            )
            if intent is not None:
                return intent
    return None


def merge_shortflow_progress_from_intent_parameters(
    *,
    prior_metadata: Mapping[str, Any] | None,
    parameters: Mapping[str, Any] | None,
    result_capture_id: str | None = None,
    action_execution_id: str | None = None,
    ok: bool = True,
) -> dict[str, Any]:
    """把短流程 binding 与回执写回下一帧 metadata。"""

    prior = dict(prior_metadata) if isinstance(prior_metadata, Mapping) else {}
    params = parameters if isinstance(parameters, Mapping) else {}
    binding = params.get(APP_SHORTFLOW_BINDING_KEY)
    if not isinstance(binding, Mapping):
        return prior
    coverage = prior.get("coverage")
    base = dict(coverage) if isinstance(coverage, Mapping) else dict(prior)
    updated = record_app_shortflow_receipt(
        base,
        binding=binding,
        result_capture_id=result_capture_id,
        action_execution_id=action_execution_id,
        ok=ok,
    )
    # 计划列表保留在 coverage／顶层均可被驱动读取
    for key in ("journey_execution_plans", "app_journey_plans_ready", APP_SHORTFLOW_PROGRESS_KEY):
        if key in updated:
            prior[key] = updated[key]
    if "app_shortflow_completed" in updated:
        prior["app_shortflow_completed"] = updated["app_shortflow_completed"]
    if isinstance(coverage, Mapping) or "coverage" in prior:
        prior["coverage"] = {
            **(dict(coverage) if isinstance(coverage, Mapping) else {}),
            **{
                k: updated[k]
                for k in (
                    APP_SHORTFLOW_PROGRESS_KEY,
                    "app_shortflow_completed",
                    "journey_execution_plans",
                    "app_journey_plans_ready",
                )
                if k in updated
            },
        }
    return prior


def record_app_shortflow_receipt(
    coverage_metadata: Mapping[str, Any] | None,
    *,
    binding: Mapping[str, Any] | None,
    result_capture_id: str | None,
    action_execution_id: str | None = None,
    ok: bool = True,
) -> dict[str, Any]:
    """动作回执后推进短流程游标。"""

    out = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    if not isinstance(binding, Mapping):
        return out
    definition_id = str(binding.get("definition_id") or "").strip()
    checkpoint_id = str(binding.get("checkpoint_id") or "").strip()
    execution_plan_id = str(binding.get("execution_plan_id") or "").strip()
    if not definition_id or not checkpoint_id:
        return out
    progress = _progress_map(out)
    row = dict(progress.get(definition_id) or {})
    receipts = [
        item for item in list(row.get("receipts") or []) if isinstance(item, Mapping)
    ]
    receipt = {
        "checkpoint_id": checkpoint_id,
        "execution_plan_id": execution_plan_id,
        "result_capture_id": str(result_capture_id or "").strip() or None,
        "action_execution_id": str(action_execution_id or "").strip() or None,
        "ok": bool(ok),
    }
    if receipt not in receipts:
        receipts.append(receipt)
    row["receipts"] = receipts
    row["execution_plan_id"] = execution_plan_id or row.get("execution_plan_id")
    row["last_checkpoint_id"] = checkpoint_id
    if not ok:
        row["status"] = "failed"
        row["gap_reason"] = str(binding.get("purpose") or "shortflow_step_failed")
    elif definition_id == "app-player-pause-resume":
        row["status"] = "running"
        row["next_checkpoint_id"] = "resume" if checkpoint_id == "pause" else None
        if checkpoint_id == "resume":
            row["status"] = "succeeded"
    elif definition_id == "app-payment-display-return":
        row["status"] = "running"
        row["next_checkpoint_id"] = (
            "return_source" if checkpoint_id == "select_plan" else None
        )
        if checkpoint_id == "return_source":
            row["status"] = "succeeded"
    progress[definition_id] = row
    out[APP_SHORTFLOW_PROGRESS_KEY] = progress
    if str(row.get("status") or "") == "succeeded":
        done = {
            str(item)
            for item in (
                out.get("app_shortflow_completed")
                if isinstance(out.get("app_shortflow_completed"), Sequence)
                else ()
            )
        }
        done.add(definition_id)
        out["app_shortflow_completed"] = sorted(done)
    return out


def _drive_player(
    *,
    work: ExplorationWork,
    definition_id: str,
    execution_plan_id: str,
    progress_row: Mapping[str, Any],
    page: str,
) -> ActionIntent | None:
    if page not in _PLAYER_PAGES:
        return None
    next_id = str(progress_row.get("next_checkpoint_id") or "pause").strip() or "pause"
    if next_id == "pause":
        candidate = _find_candidate(work, labels=_PLAYER_PAUSE_LABELS) or _center_candidate(
            work
        )
        if candidate is None:
            return None
        return _click_intent(
            work,
            candidate=candidate,
            definition_id=definition_id,
            execution_plan_id=execution_plan_id,
            checkpoint_id="pause",
            purpose="F01 播放器短流程：暂停并采集呈现帧（只读）",
        )
    if next_id == "resume":
        candidate = _find_candidate(work, labels=_PLAYER_RESUME_LABELS) or _center_candidate(
            work
        )
        if candidate is None:
            return None
        return _click_intent(
            work,
            candidate=candidate,
            definition_id=definition_id,
            execution_plan_id=execution_plan_id,
            checkpoint_id="resume",
            purpose="F01 播放器短流程：恢复播放并留证（只读）",
        )
    return None


def _drive_payment(
    *,
    work: ExplorationWork,
    definition_id: str,
    execution_plan_id: str,
    progress_row: Mapping[str, Any],
    page: str,
) -> ActionIntent | None:
    next_id = str(progress_row.get("next_checkpoint_id") or "select_plan").strip() or "select_plan"
    if next_id == "select_plan":
        if page not in _PAYMENT_PAGES and not _looks_like_payment(work):
            return None
        candidate = _find_candidate(work, labels=_PAYMENT_PLAN_LABELS)
        if candidate is None:
            return None
        return _click_intent(
            work,
            candidate=candidate,
            definition_id=definition_id,
            execution_plan_id=execution_plan_id,
            checkpoint_id="select_plan",
            purpose="F01 支付短流程：只读选档留证，禁止确认付款",
        )
    if next_id == "return_source":
        candidate = _find_candidate(work, labels=_PAYMENT_BACK_LABELS)
        if candidate is None:
            return _back_intent(
                work,
                definition_id=definition_id,
                execution_plan_id=execution_plan_id,
                checkpoint_id="return_source",
                purpose="F01 支付短流程：返回来源页（语义 BACK）",
            )
        return _click_intent(
            work,
            candidate=candidate,
            definition_id=definition_id,
            execution_plan_id=execution_plan_id,
            checkpoint_id="return_source",
            purpose="F01 支付短流程：返回支付前来源页",
        )
    return None


def _click_intent(
    work: ExplorationWork,
    *,
    candidate: Any,
    definition_id: str,
    execution_plan_id: str,
    checkpoint_id: str,
    purpose: str,
) -> ActionIntent:
    binding = {
        "definition_id": definition_id,
        "execution_plan_id": execution_plan_id,
        "checkpoint_id": checkpoint_id,
        "purpose": purpose,
        "side_effect_policy": "read_only",
    }
    digest = sha256(
        f"{work.task_id}|{definition_id}|{checkpoint_id}|{work.source_capture_id}|{candidate.candidate_id}".encode()
    ).hexdigest()
    parameters: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "exploration_work_id": work.work_id,
        "app_shortflow_driver": True,
        APP_SHORTFLOW_BINDING_KEY: binding,
        "forbid_confirm_pay": True,
        "require_screenshot_evidence": True,
    }
    meta = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
    if "normalized_bounds" in meta:
        parameters["normalized_bounds"] = meta["normalized_bounds"]
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=f"action-intent:{digest}",
        idempotency_key=f"action-intent:{digest}",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_target,
        parameters=parameters,
        purpose=purpose,
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            candidate.action_type,
        ),
        deadline=work.deadline,
    )


def _back_intent(
    work: ExplorationWork,
    *,
    definition_id: str,
    execution_plan_id: str,
    checkpoint_id: str,
    purpose: str,
) -> ActionIntent:
    binding = {
        "definition_id": definition_id,
        "execution_plan_id": execution_plan_id,
        "checkpoint_id": checkpoint_id,
        "purpose": purpose,
        "side_effect_policy": "read_only",
        "recovery_action": "system_back",
    }
    digest = sha256(
        f"{work.task_id}|{definition_id}|{checkpoint_id}|back|{work.source_capture_id}".encode()
    ).hexdigest()
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=f"action-intent:{digest}",
        idempotency_key=f"action-intent:{digest}",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.BACK,
        semantic_target="返回来源",
        parameters={
            "exploration_work_id": work.work_id,
            "app_shortflow_driver": True,
            APP_SHORTFLOW_BINDING_KEY: binding,
            "forbid_confirm_pay": True,
        },
        purpose=purpose,
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            InteractionOperation.BACK,
        ),
        deadline=work.deadline,
    )


def _find_candidate(work: ExplorationWork, *, labels: Sequence[str]):
    lowered = tuple(str(item).casefold() for item in labels)
    for candidate in work.frontier.candidates:
        text = " ".join(
            str(part or "")
            for part in (
                getattr(candidate, "semantic_target", None),
                (getattr(candidate, "metadata", None) or {}).get("text")
                if isinstance(getattr(candidate, "metadata", None), Mapping)
                else None,
            )
        ).casefold()
        if any(token in text for token in lowered if token):
            return candidate
    return None


def _center_candidate(work: ExplorationWork):
    """仅复用帧内已有中心区域候选，不合成坐标。"""

    for candidate in work.frontier.candidates:
        meta = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
        if meta.get("player_center") is True or meta.get("surface_center") is True:
            return candidate
        bounds = meta.get("normalized_bounds")
        if not isinstance(bounds, Mapping):
            continue
        try:
            x = float(bounds.get("x", 0))
            y = float(bounds.get("y", 0))
            w = float(bounds.get("width", 0))
            h = float(bounds.get("height", 0))
        except (TypeError, ValueError):
            continue
        cx, cy = x + w / 2.0, y + h / 2.0
        if 0.35 <= cx <= 0.65 and 0.35 <= cy <= 0.65:
            return candidate
    return None


def _looks_like_payment(work: ExplorationWork) -> bool:
    meta = work.frontier.observation.metadata
    summary = ""
    if isinstance(meta, Mapping):
        summary = str(meta.get("visual_summary") or "")
    blob = f"{_page_type(work)} {summary}"
    return any(token in blob for token in ("会员", "支付", "充值", "开通", "档位", "套餐"))


def _page_type(work: ExplorationWork) -> str:
    page = str(getattr(work.frontier.observation, "page_type", None) or "").strip().casefold()
    if page:
        return page
    meta = work.frontier.observation.metadata
    if isinstance(meta, Mapping):
        for key in ("page_type", "visual_page_type", "resolved_page_type"):
            text = str(meta.get(key) or "").strip().casefold()
            if text:
                return text
    return ""


def _coverage(work: ExplorationWork) -> Mapping[str, Any]:
    meta = work.frontier.observation.metadata
    if not isinstance(meta, Mapping):
        return {}
    coverage = meta.get("coverage")
    if isinstance(coverage, Mapping):
        merged = dict(coverage)
        for key in (
            "journey_execution_plans",
            APP_SHORTFLOW_PROGRESS_KEY,
            "app_journey_plans_ready",
        ):
            if key in meta and key not in merged:
                merged[key] = meta[key]
        return merged
    return meta


def _progress_map(coverage: Mapping[str, Any]) -> dict[str, Any]:
    raw = coverage.get(APP_SHORTFLOW_PROGRESS_KEY)
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}
