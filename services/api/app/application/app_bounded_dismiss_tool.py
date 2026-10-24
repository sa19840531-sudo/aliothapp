"""限步无状态清障工具＝①限步 + ②无状态 Tool + ③约束输出。

全部产品（含未知包）的开屏／弹层统一采用本工具，
不得交给长对话 Agent 自由烧步，也不再由产品专属脚本接管。

① 开屏≤3、弹层≤2；超时 Fail-safe 交还业务（禁止空转）。
② 每次只看当前帧，不保留对话上下文（Stateless tool call）。
③ 决策面仅两种：
   - {"found": true, "coordinate": [x, y]}  → 点该坐标关闭／跳过
   - {"found": false}                      → 立即交还业务探索
   禁止自选 BACK／滑动／输入／底栏等越权动作（首页 BACK 会退到桌面）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.exploration import CandidateAction, ExplorationWork
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
)

# 显式允许名单（历史试点）；无专属编排的包也会走 Agent 三合同（见 package_uses_bounded_dismiss）。
BOUNDED_DISMISS_PACKAGES = frozenset({"com.alz7xm2.drama"})
SPLASH_MAX_STEPS = 3
POPUP_MAX_STEPS = 2

_SPLASH_PAGE_TYPES = frozenset({"startup_ad", "splash", "countdown", "guide_page"})
_SPLASH_PAGE_STATES = frozenset({"startup_ad", "splash", "countdown"})
_POPUP_PAGE_STATES = frozenset(
    {"popup", "modal", "dialog", "system_dialog", "overlay"}
)
_SKIP_MARKERS = (
    "跳过",
    "跳過",
    "skip",
    "进入",
    "進入",
    "立即进入",
    "我知道了",
    "知道了",
)
_CLOSE_MARKERS = (
    "关闭",
    "關閉",
    "关闭弹层",
    "關閉彈層",
    "关闭广告",
    "關閉廣告",
    "dismiss",
    "close",
)
_PURCHASE_MARKERS = (
    "抢购",
    "搶購",
    "购买",
    "購買",
    "开通会员",
    "立即开通",
    "¥",
    "支付",
)


@dataclass(frozen=True)
class ConstrainedDismissVerdict:
    """③ 约束输出：只表达「找到关闭点」或「画面已干净」。"""

    found: bool
    phase: Literal["splash", "popup"]
    coordinate: tuple[float, float] | None = None
    normalized_bounds: Mapping[str, float] | None = None
    candidate_id: str | None = None
    semantic_target: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        if not self.found:
            return {"found": False, "phase": self.phase, "reason": self.reason}
        payload: dict[str, object] = {
            "found": True,
            "phase": self.phase,
            "reason": self.reason,
        }
        if self.coordinate is not None:
            payload["coordinate"] = [self.coordinate[0], self.coordinate[1]]
        if self.normalized_bounds is not None:
            payload["normalized_bounds"] = dict(self.normalized_bounds)
        if self.candidate_id:
            payload["candidate_id"] = self.candidate_id
        if self.semantic_target:
            payload["semantic_target"] = self.semantic_target
        return payload


def package_uses_bounded_dismiss(*, package_name: str | None) -> bool:
    """①②③ 清障：无产品专属编排时启用；有编排执行器的包不抢步进。"""

    package = str(package_name or "").strip()
    if not package:
        return False
    if package.casefold() in {item.casefold() for item in BOUNDED_DISMISS_PACKAGES}:
        return True
    from services.api.app.application.app_product_orchestration_policy import (
        uses_agent_generic_contract,
    )

    return uses_agent_generic_contract(package)


def judge_dismiss_frame(work: ExplorationWork) -> ConstrainedDismissVerdict | None:
    """②+③：单帧无状态判决；非清障相位返回 None。"""

    if not isinstance(work, ExplorationWork):
        return None
    if not getattr(work, "_app_three_contracts", False) and not package_uses_bounded_dismiss(
        package_name=_work_package_name(work)
    ):
        return None
    phase = _detect_dismiss_phase(work)
    if phase is None:
        return None
    hit = _pick_coordinate_dismiss_candidate(
        eligible=tuple(work.frontier.candidates),
        phase=phase,
    )
    if hit is None:
        if getattr(work, "_app_three_contracts", False):
            # 正式候选只带 ID，坐标留在执行层；不得因此漏掉明确关闭按钮。
            semantic = [item for item in work.frontier.candidates
                        if item.action_type in {
                            InteractionOperation.CLICK, InteractionOperation.TAP,
                        }
                        and item.risk_level is InteractionRiskLevel.LOW
                        and _candidate_dismiss_rank(item, phase=phase) > 0
                        and item.metadata.get("source_capture_id", work.source_capture_id)
                        == work.source_capture_id]
            if semantic:
                chosen = max(semantic, key=lambda item: _candidate_dismiss_rank(item, phase=phase))
                return ConstrainedDismissVerdict(
                    found=True, phase=phase, candidate_id=chosen.candidate_id,
                    semantic_target=chosen.semantic_target, reason="current_frame_candidate",
                )
        return ConstrainedDismissVerdict(
            found=False,
            phase=phase,
            reason="no_skip_or_close_coordinate",
        )
    candidate, coordinate, bounds = hit
    return ConstrainedDismissVerdict(
        found=True,
        phase=phase,
        coordinate=coordinate,
        normalized_bounds=bounds,
        candidate_id=candidate.candidate_id,
        semantic_target=str(candidate.semantic_target or "").strip() or None,
        reason="skip_or_close_coordinate",
    )


def try_bounded_dismiss_tool(work: ExplorationWork) -> ActionIntent | None:
    """①+②+③：限步清障；found 则点坐标，false／超步则交还业务（永不 BACK）。"""

    if not isinstance(work, ExplorationWork):
        return None
    package = _work_package_name(work)
    if not getattr(work, "_app_three_contracts", False) and not package_uses_bounded_dismiss(
        package_name=package
    ):
        return None

    phase = _detect_dismiss_phase(work)
    if phase is None:
        return None

    # 账号凭证二段式／试看付费墙：交还确定性业务路径，禁止抢点假「关闭」或中部广告格。
    if _should_yield_to_business_popup(work):
        return None
    # 通用 Agent 包：帧上已有底栏／上方分类寻页时，让覆盖合同优先（假关闭／软遮罩不得堵课纲）。
    # 糖果显式名单仍先清障，避免广告挡路烧步。
    if package.casefold() not in {item.casefold() for item in BOUNDED_DISMISS_PACKAGES}:
        if _should_yield_to_coverage_seek(work):
            return None

    max_steps = SPLASH_MAX_STEPS if phase == "splash" else POPUP_MAX_STEPS
    steps = _dismiss_step_count(work=work, phase=phase)

    # ① Fail-safe：步数用尽 → found:false 语义，交还业务；禁止 BACK（会退桌面）。
    if steps >= max_steps:
        return None

    verdict = judge_dismiss_frame(work)
    if verdict is None:
        return None

    if not verdict.found:
        # ③ found:false → 弹层相位立即放行；开屏可再观察一帧找「跳过」。
        if phase == "splash" and steps + 1 < max_steps:
            return _observe_intent(
                work=work,
                phase=phase,
                steps=steps,
                max_steps=max_steps,
                verdict=verdict,
            )
        return None

    candidate = _candidate_by_id(
        eligible=tuple(work.frontier.candidates),
        candidate_id=verdict.candidate_id,
    )
    if candidate is None:
        # 判决有坐标但候选已丢：用合成点击承载约束输出。
        return _click_from_verdict(
            work=work,
            verdict=verdict,
            steps=steps,
            max_steps=max_steps,
        )
    return _intent_from_candidate(
        work=work,
        candidate=candidate,
        phase=phase,
        steps=steps,
        max_steps=max_steps,
        verdict=verdict,
    )


def _work_package_name(work: ExplorationWork) -> str:
    meta = work.frontier.observation.metadata
    if isinstance(meta, Mapping):
        package = str(meta.get("package_name") or "").strip()
        if package:
            return package
    if work.app_goal is not None:
        for item in work.app_goal.allowed_packages or ():
            text = str(item or "").strip()
            if text:
                return text
    return ""


def _should_yield_to_business_popup(work: ExplorationWork) -> bool:
    """凭证保存／试看结束付费墙不得由清障工具代劳。"""

    for candidate in work.frontier.candidates:
        if candidate.metadata.get("profile_credential_save") is True:
            return True
        if candidate.metadata.get("profile_credential_dismiss") is True:
            return True
        role = str(candidate.metadata.get("control_role") or "").casefold()
        if role in {"profile_credential_confirm", "profile_credential_dismiss"}:
            return True
        target = str(candidate.semantic_target or "")
        if "保存凭证" in target or "我已保存" in target:
            return True
    meta = work.frontier.observation.metadata
    if not isinstance(meta, Mapping):
        meta = {}
    blob = " ".join(
        str(part or "")
        for part in (
            work.frontier.observation.page_type,
            work.frontier.observation.page_state,
            meta.get("visual_summary"),
            meta.get("ocr_text"),
        )
    )
    paywall_markers = (
        "试看结束",
        "开通预售",
        "全站视频免费看",
        "开通会员",
        "立即开通",
        "预售卡",
    )
    return any(marker in blob for marker in paywall_markers)


def _should_yield_to_coverage_seek(work: ExplorationWork) -> bool:
    """当前帧已提升底栏／上方分类／宫格寻页时，清障让路给覆盖合同。"""

    for candidate in work.frontier.candidates:
        meta = candidate.metadata
        if meta.get("bottom_navigation_tab_seek") is True:
            return True
        if meta.get("home_top_category_tab_seek") is True:
            return True
        if meta.get("home_category_grid_seek") is True:
            return True
    return False


def _detect_dismiss_phase(work: ExplorationWork) -> Literal["splash", "popup"] | None:
    observation = work.frontier.observation
    page_type = str(observation.page_type or "").casefold()
    page_state = str(observation.page_state or "").casefold()
    meta = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    facts = meta.get("surface_facts")
    if not isinstance(facts, Mapping):
        facts = {}

    if page_type in _SPLASH_PAGE_TYPES or page_state in _SPLASH_PAGE_STATES:
        return "splash"
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if dialog_kind in {"ad_popup", "promo_popup", "promo_ad", "startup_ad"}:
        return "popup"
    if page_state in _POPUP_PAGE_STATES:
        return "popup"
    if facts.get("popup_visible") is True or observation.overlay_signature is not None:
        # 无关闭坐标时不抢业务（避免详情／飘条误标）。
        if _frame_has_coordinate_dismiss(work.frontier.candidates, phase="popup"):
            return "popup"
    return None


def _dismiss_step_count(
    *,
    work: ExplorationWork,
    phase: Literal["splash", "popup"],
) -> int:
    meta = work.frontier.observation.metadata
    if not isinstance(meta, Mapping):
        return 0
    key = (
        "bounded_dismiss_splash_steps"
        if phase == "splash"
        else "bounded_dismiss_popup_steps"
    )
    raw = meta.get(key)
    explicit = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0
    if phase == "splash":
        close = meta.get("surface_phase_close_attempt_count")
        observe = meta.get("surface_phase_countdown_observation_count")
        close_n = (
            close if isinstance(close, int) and not isinstance(close, bool) and close >= 0 else 0
        )
        observe_n = (
            observe
            if isinstance(observe, int) and not isinstance(observe, bool) and observe >= 0
            else 0
        )
        return max(explicit, close_n + observe_n)
    same = meta.get("surface_phase_same_popup_attempt_count")
    noop = meta.get("dismiss_noop_streak")
    same_n = (
        same if isinstance(same, int) and not isinstance(same, bool) and same >= 0 else 0
    )
    noop_n = (
        noop if isinstance(noop, int) and not isinstance(noop, bool) and noop >= 0 else 0
    )
    return max(explicit, same_n, noop_n)


def _frame_has_coordinate_dismiss(
    eligible: Sequence[CandidateAction],
    *,
    phase: Literal["splash", "popup"],
) -> bool:
    return _pick_coordinate_dismiss_candidate(eligible=eligible, phase=phase) is not None


def _pick_coordinate_dismiss_candidate(
    *,
    eligible: Sequence[CandidateAction],
    phase: Literal["splash", "popup"],
) -> tuple[CandidateAction, tuple[float, float], dict[str, float]] | None:
    """③：只收 CLICK/TAP 且带归一化坐标的关闭／跳过点；忽略 BACK。"""

    ranked: list[
        tuple[tuple[int, int, int], CandidateAction, tuple[float, float], dict[str, float]]
    ] = []
    for candidate in eligible:
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        # ③ 硬禁：清障工具不得选用 BACK／cloud_back 候选。
        if candidate.action_type is InteractionOperation.BACK:
            continue
        if candidate.metadata.get("cloud_back_popup_close") is True:
            continue
        if candidate.metadata.get("forced_detail_exit_back") is True:
            continue
        rank = _candidate_dismiss_rank(candidate, phase=phase)
        if rank <= 0:
            continue
        parsed = _normalized_bounds_and_center(candidate)
        if parsed is None:
            continue
        bounds, center = parsed
        ranked.append(
            (
                (rank, candidate.exploration_value, -candidate.navigation_depth),
                candidate,
                center,
                bounds,
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    _, candidate, center, bounds = ranked[0]
    return candidate, center, bounds


def _normalized_bounds_and_center(
    candidate: CandidateAction,
) -> tuple[dict[str, float], tuple[float, float]] | None:
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        contract = candidate.metadata.get("popup_close_corner_contract")
        if isinstance(contract, Mapping):
            raw = contract.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return None
    try:
        left = float(raw["left"])
        top = float(raw["top"])
        right = float(raw["right"])
        bottom = float(raw["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    bounds = {"left": left, "top": top, "right": right, "bottom": bottom}
    return bounds, ((left + right) / 2.0, (top + bottom) / 2.0)


def _candidate_by_id(
    *,
    eligible: Sequence[CandidateAction],
    candidate_id: str | None,
) -> CandidateAction | None:
    if not candidate_id:
        return None
    for candidate in eligible:
        if candidate.candidate_id == candidate_id:
            return candidate
    return None


def _candidate_dismiss_rank(
    candidate: CandidateAction,
    *,
    phase: Literal["splash", "popup"],
) -> int:
    role = str(candidate.metadata.get("control_role") or "").casefold()
    hint = str(candidate.metadata.get("expected_state_hint") or "").casefold()
    terms = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("text") or ""),
            hint,
            role,
        )
    )
    folded = terms.casefold()
    if any(marker in terms for marker in _PURCHASE_MARKERS):
        return 0
    if any(marker in folded for marker in ("开通会员", "立即开通", "purchase", "payment")):
        return 0
    # ③：文案像返回／底栏的一律不收。
    if any(
        marker in terms
        for marker in ("系统 BACK", "cloud_back", "底部导航栏「<", "返回桌面")
    ):
        return 0
    score = 0
    if phase == "splash":
        if any(marker in terms for marker in _SKIP_MARKERS) or hint in {
            "tap_skip",
            "skip_ad",
            "app_ready",
        }:
            score += 50
        if role in {"skip", "enter"}:
            score += 20
    else:
        if any(marker in terms for marker in _CLOSE_MARKERS) or role in {
            "dismiss",
            "close",
        }:
            score += 50
        if candidate.metadata.get("popup_close_corner_fallback") is True:
            score += 30
        if candidate.metadata.get("popup_close_pixel_visual") is True:
            score += 25
        if any(marker in terms for marker in _SKIP_MARKERS):
            score += 15
    return score


def _intent_from_candidate(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
    phase: Literal["splash", "popup"],
    steps: int,
    max_steps: int,
    verdict: ConstrainedDismissVerdict,
) -> ActionIntent:
    next_step = steps + 1
    target = str(candidate.semantic_target or "").strip() or "清障控件"
    purpose = (
        f"限步清障工具（{phase} {next_step}/{max_steps}）："
        f"约束输出 found=true 点{target}，禁止 BACK／长对话自由探索。"
    )
    metadata = {
        **dict(candidate.metadata),
        "bounded_dismiss_tool": True,
        "bounded_dismiss_phase": phase,
        "bounded_dismiss_step": next_step,
        "bounded_dismiss_max_steps": max_steps,
        "bounded_dismiss_failsafe": False,
        "bounded_dismiss_constrained": True,
        "bounded_dismiss_verdict": verdict.as_dict(),
        "purpose": purpose,
    }
    if verdict.normalized_bounds is not None and "normalized_bounds" not in metadata:
        metadata["normalized_bounds"] = dict(verdict.normalized_bounds)
    wrapped = CandidateAction(
        candidate_id=candidate.candidate_id,
        semantic_target=candidate.semantic_target,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=candidate.exploration_value,
        navigation_depth=candidate.navigation_depth,
        expected_surface_kind=candidate.expected_surface_kind,
        prior_failure_count=candidate.prior_failure_count,
        metadata=metadata,
        protocol_version=candidate.protocol_version,
    )
    return _build_intent(work=work, candidate=wrapped, purpose=purpose, verdict=verdict)


def _click_from_verdict(
    *,
    work: ExplorationWork,
    verdict: ConstrainedDismissVerdict,
    steps: int,
    max_steps: int,
) -> ActionIntent:
    next_step = steps + 1
    phase = verdict.phase
    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    target = verdict.semantic_target or "清障坐标"
    purpose = (
        f"限步清障工具（{phase} {next_step}/{max_steps}）："
        f"约束输出 found=true coordinate={list(verdict.coordinate or ())} 点{target}。"
    )
    metadata: dict[str, object] = {
        "purpose": purpose,
        "frontier_scope": "local",
        "bounded_dismiss_tool": True,
        "bounded_dismiss_phase": phase,
        "bounded_dismiss_step": next_step,
        "bounded_dismiss_max_steps": max_steps,
        "bounded_dismiss_constrained": True,
        "bounded_dismiss_verdict": verdict.as_dict(),
        "expected_state_hint": "close_popup_x" if phase == "popup" else "tap_skip",
        "control_role": "dismiss" if phase == "popup" else "skip",
    }
    if verdict.normalized_bounds is not None:
        metadata["normalized_bounds"] = dict(verdict.normalized_bounds)
    candidate = CandidateAction(
        candidate_id=f"bounded-dismiss-coord:{phase}:{capture_id}",
        semantic_target=target,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata=metadata,
    )
    return _build_intent(work=work, candidate=candidate, purpose=purpose, verdict=verdict)


def _observe_intent(
    *,
    work: ExplorationWork,
    phase: Literal["splash", "popup"],
    steps: int,
    max_steps: int,
    verdict: ConstrainedDismissVerdict,
) -> ActionIntent:
    next_step = steps + 1
    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    purpose = (
        f"限步清障工具（{phase} {next_step}/{max_steps}）："
        "约束输出 found=false，开屏再观察一帧找跳过。"
    )
    candidate = CandidateAction(
        candidate_id=f"bounded-dismiss-observe:{phase}:{capture_id}",
        semantic_target="观察等待清障控件",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=40,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "bounded_dismiss_tool": True,
            "bounded_dismiss_phase": phase,
            "bounded_dismiss_step": next_step,
            "bounded_dismiss_max_steps": max_steps,
            "bounded_dismiss_constrained": True,
            "bounded_dismiss_verdict": verdict.as_dict(),
            "forced_popup_clearance_observe": True,
            "expected_state_hint": "popup_clearance_reobserve",
        },
    )
    return _build_intent(work=work, candidate=candidate, purpose=purpose, verdict=verdict)


def _build_intent(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
    purpose: str,
    verdict: ConstrainedDismissVerdict,
) -> ActionIntent:
    from services.api.app.application.app_synthetic_touch_contracts import (
        attach_synthetic_touch_contracts,
    )

    digest = hashlib.sha256(
        json.dumps(
            [
                work.work_id,
                work.frontier_snapshot_id,
                work.source_capture_id,
                candidate.candidate_id,
                purpose,
                verdict.as_dict(),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    parameters: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "bounded_dismiss_tool": True,
        "bounded_dismiss_constrained": True,
        "bounded_dismiss_verdict": verdict.as_dict(),
        "exploration_work_id": work.work_id,
    }
    package = _work_package_name(work)
    if package:
        parameters["package_name"] = package
    phase = candidate.metadata.get("bounded_dismiss_phase")
    step = candidate.metadata.get("bounded_dismiss_step")
    if phase in {"splash", "popup"} and isinstance(step, int):
        parameters["bounded_dismiss_phase"] = phase
        parameters["bounded_dismiss_step"] = step
        if phase == "splash":
            parameters["bounded_dismiss_splash_steps"] = step
        else:
            parameters["bounded_dismiss_popup_steps"] = step
    # ③：工具出口只允许 CLICK／OBSERVE，禁止 BACK。
    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.OBSERVE,
    }:
        raise RuntimeError("bounded dismiss constrained output forbids non-click actions")
    attach_synthetic_touch_contracts(parameters, candidate.metadata)
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
