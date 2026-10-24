"""弹窗未清时有界派发不得因合成候选绑定拒收而收口。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from services.api.app.application.app_exploration_flow_contract import (
    update_app_flow_contract,
)
from services.api.app.application.app_synthetic_touch_contracts import (
    popup_clearance_observe_contract,
    resolve_popup_clearance_observe_candidate,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.exploration_dispatch_guard import (
    bounded_dispatch_terminal,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.tests.support.app_home_top_contract import (
    legacy_home_top_features as legacy_home_top_features,
)


def _work_with_popup(*, remaining: int = 10):
    flow = update_app_flow_contract(
        None,
        page_state="popup",
        overlay_signature="ad:1",
        surface_facts={"popup_visible": True, "dialog_kind": "ad_popup"},
        playbook_tabs=("主页", "短剧"),
    )
    observation = SimpleNamespace(
        page_type="home",
        page_state="popup",
        metadata={"app_flow_contract": flow},
        capture_id="cap-1",
    )
    frontier = SimpleNamespace(
        candidates=(),
        observation=observation,
        visited_state_ids=frozenset(),
    )
    budget = SimpleNamespace(remaining_actions=remaining)
    work = SimpleNamespace(
        task_id="task-1",
        work_id="work-1",
        surface_kind=SurfaceKind.ANDROID,
        frontier=frontier,
        budget=budget,
        deadline=datetime.now(UTC),
    )
    # scheduler_state_from_work 需要这些字段时返回 None → 不形成终态；
    # 这里用真实 ExplorationWork 结构会更稳，但 guard 在 state is None 时直接 return None。
    # 改为 mock scheduler_state_from_work。
    return work


def test_popup_unsatisfied_forced_observe_not_bounded(monkeypatch) -> None:
    from services.api.app.application import exploration_dispatch_guard as guard

    flow = update_app_flow_contract(
        None,
        page_state="popup",
        overlay_signature="ad:1",
        surface_facts={"popup_visible": True},
    )
    observation = SimpleNamespace(
        metadata={"app_flow_contract": flow},
        capture_id="cap-obs",
    )
    work = SimpleNamespace(
        task_id="t1",
        work_id="w1",
        surface_kind=SurfaceKind.ANDROID,
        frontier=SimpleNamespace(candidates=(), observation=observation),
        budget=SimpleNamespace(remaining_actions=20),
    )
    state = MagicMock()
    state.stop_reason.return_value = guard.ExplorationStopReason.NONE
    monkeypatch.setattr(guard, "scheduler_state_from_work", lambda w: state)
    monkeypatch.setattr(
        guard,
        "eligible_app_candidates",
        lambda work, now=None: ([], {}),
    )
    purpose = (
        "弹窗流程未满足且本帧无可靠关闭点；观察重采以续发关层候选，"
        "禁止 Frontier 排空收口。"
    )
    decision = ActionIntent(
        task_id="t1",
        session_id="s1",
        intent_id="i1",
        idempotency_key="k1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="cap-obs",
        action_type=InteractionOperation.OBSERVE,
        semantic_target="观察等待弹层关闭控件",
        parameters={
            "candidate_id": "forced-popup-clearance-observe:cap-obs",
            "app_popup_clearance_observe": popup_clearance_observe_contract(
                purpose=purpose
            ),
        },
        purpose=purpose,
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            SurfaceKind.ANDROID, InteractionOperation.OBSERVE
        ),
        deadline=datetime.now(UTC),
    )
    assert bounded_dispatch_terminal(
        work=work, decision=decision, now=datetime.now(UTC)
    ) is None


def test_resolve_forced_popup_clearance_observe() -> None:
    purpose = (
        "弹窗流程未满足且本帧无可靠关闭点；观察重采以续发关层候选，"
        "禁止 Frontier 排空收口。"
    )
    analysis = SimpleNamespace(
        capture_id="cap-obs",
        image_signature="sha256:" + ("ab" * 32),
        width=1080,
        height=1920,
        orientation="portrait",
    )
    resolved = resolve_popup_clearance_observe_candidate(
        intent_semantic_target="观察等待弹层关闭控件",
        intent_purpose=purpose,
        analysis=analysis,
        candidate_id="forced-popup-clearance-observe:cap-obs",
        payload=popup_clearance_observe_contract(purpose=purpose),
    )
    assert resolved is not None
    assert resolved.action_type is InteractionOperation.OBSERVE


def test_resolve_forced_popup_scrim_dismiss() -> None:
    from services.api.app.application.app_synthetic_touch_contracts import (
        DEFAULT_NO_CLOSE_PROMO_SCRIM_BOUNDS,
        popup_scrim_dismiss_contract,
        resolve_popup_scrim_dismiss_candidate,
    )

    purpose = (
        "当前为无关闭 X 的促销／终身卡层；点击蒙层空白处关闭，"
        "禁止点立即抢购／底飘假 X；关闭见主页后再点上方分类。"
    )
    analysis = SimpleNamespace(
        capture_id="cap-scrim",
        image_signature="sha256:" + ("cd" * 32),
        width=1080,
        height=1920,
        orientation="portrait",
    )
    resolved = resolve_popup_scrim_dismiss_candidate(
        intent_semantic_target="点击弹层蒙层空白处关闭",
        intent_purpose=purpose,
        analysis=analysis,
        candidate_id="forced-popup-scrim-dismiss:cap-scrim",
        payload=popup_scrim_dismiss_contract(
            bounds=DEFAULT_NO_CLOSE_PROMO_SCRIM_BOUNDS,
            purpose=purpose,
        ),
    )
    assert resolved is not None
    assert resolved.action_type is InteractionOperation.CLICK
    assert resolved.normalized_bounds is not None
    assert abs(resolved.normalized_bounds.top - 0.775) < 1e-6


def test_home_top_incomplete_rejected_candidate_not_bounded(
    monkeypatch, legacy_home_top_features,
) -> None:
    """上方分类未完时，绑定拒收不得 BOUNDED 收口。"""

    from services.api.app.application import exploration_dispatch_guard as guard
    from services.api.app.application.app_ui_tree_features import (
        ordered_home_top_category_tab_labels,
    )

    observation = SimpleNamespace(
        page_type="home",
        page_state="stable",
        metadata={
            "package_name": "dark.wwcskrpojs.ebjhyebjbl",
            "visited_home_top_category_tabs": [],
            "visual_summary": "至尊终身卡 立即抢购",
        },
        capture_id="cap-ht",
    )
    assert ordered_home_top_category_tab_labels(
        package_name="dark.wwcskrpojs.ebjhyebjbl"
    )
    work = SimpleNamespace(
        task_id="t1",
        work_id="w1",
        surface_kind=SurfaceKind.ANDROID,
        frontier=SimpleNamespace(candidates=(), observation=observation),
        budget=SimpleNamespace(remaining_actions=200),
        app_goal=SimpleNamespace(allowed_packages=("dark.wwcskrpojs.ebjhyebjbl",)),
    )
    state = MagicMock()
    state.stop_reason.return_value = guard.ExplorationStopReason.NONE
    monkeypatch.setattr(guard, "scheduler_state_from_work", lambda w: state)
    monkeypatch.setattr(
        guard,
        "eligible_app_candidates",
        lambda work, now=None: ([], {}),
    )
    decision = ActionIntent(
        task_id="t1",
        session_id="s1",
        intent_id="i1",
        idempotency_key="k1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="cap-ht",
        action_type=InteractionOperation.CLICK,
        semantic_target="最新",
        parameters={"candidate_id": "some-unbound-candidate"},
        purpose="点击主页上方分类「最新」",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            SurfaceKind.ANDROID, InteractionOperation.CLICK
        ),
        deadline=datetime.now(UTC),
    )
    assert bounded_dispatch_terminal(
        work=work, decision=decision, now=datetime.now(UTC)
    ) is None
