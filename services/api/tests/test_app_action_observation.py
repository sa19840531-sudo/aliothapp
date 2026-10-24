"""动作证明只来自绑定的前后截图，分别结算到达、状态变化和观察。"""

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.app_action_observation import (
    APP_ACTION_OBSERVATION_KEY,
    action_observation_for_successor,
    build_app_action_observation,
    confirmed_app_identity,
)
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    FrontierSnapshot,
    NormalizedBounds,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    InteractionVerificationOutcome,
    SurfaceKind,
)
from services.api.tests.test_app_screenshot_exploration import _action_turn
from services.api.tests.test_app_visual_page_identity import _analysis, _anchor, _observation
from services.api.tests.test_llm_app_vision import _request


def _frame(
    role, *, object_id=None, local_state=None, page_state="stable", package=None, facts=None
):
    return _analysis(
        _observation(object_id=object_id),
        local_state=local_state,
        page_state=page_state,
        surface_facts=facts,
        request=replace(
            _request(),
            capture_id=f"capture-{role}",
            screenshot_artifact_id=f"artifact-{role}",
            image_signature="sha256:" + ("a" if role == "before" else "b") * 64,
            package_name=package or "com.example.target",
        ),
    )


def _scenario(
    operation=InteractionOperation.CLICK, *, before=None, after=None, path=(), metadata=None
):
    before = before or _frame("before")
    after = after or _frame("after", object_id="作品编号 123")
    now = datetime.now(UTC)
    meta = {
        "capture_epoch": "epoch-test-1",
        "has_ui_tree": True,
        "current_window": "MainWindow",
    }
    if metadata:
        meta.update(metadata)
    candidate = CandidateAction(
        candidate_id="candidate-1",
        semantic_target="当前可见入口",
        action_type=operation,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata=meta,
    )
    frontier = FrontierSnapshot(
        snapshot_id="frontier-1",
        observation=SurfaceObservation(
            capture_id="capture-source",
            surface_kind=SurfaceKind.ANDROID,
            location="app://com.example.target/.MainActivity",
            state_version=before.image_signature,
            observed_at=now,
            metadata={"app_verified_navigation_path": list(path)},
        ),
        candidates=(candidate,),
    )
    work = build_exploration_work(
        task_id="task-1",
        session_id="session-1",
        branch_id="branch-1",
        objective="探索可见入口",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-source",
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.example.target",),
            allowed_operations=(operation,),
            max_navigation_depth=3,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=5,
            remaining_actions=5,
            max_depth=3,
            remaining_duration_ms=30_000,
        ),
        deadline=now + timedelta(minutes=1),
    )
    return dict(work=work, turn=_action_turn(work), candidate=candidate, before=before, after=after)


def _successor(scenario, proof):
    work, after = scenario["work"], scenario["after"]
    return replace(
        work,
        source_capture_id=after.capture_id,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                capture_id=after.capture_id,
                state_version=after.image_signature,
                metadata={
                    "screenshot_artifact_id": after.screenshot_artifact_id,
                    "visual_result_hash": after.result_hash,
                    APP_ACTION_OBSERVATION_KEY: proof.to_dict(),
                },
            ),
        ),
    )


@pytest.mark.parametrize("source_object", [None, "作品编号 122"])
def test_untyped_free_click_requires_confirmed_business_change_and_round_trips_proof(source_object):
    scenario = _scenario(before=_frame("before", object_id=source_object))
    proof = build_app_action_observation(**scenario)
    assert proof.outcome == "business_arrived"
    assert proof.arrival_proven and proof.completed
    assert proof.navigation_path == tuple(
        confirmed_app_identity(scenario[key]) for key in ("before", "after")
    )
    consumed = action_observation_for_successor(
        turn=scenario["turn"],
        successor=_successor(scenario, proof),
    )
    assert consumed["binding"]["action_execution_id"] == "action-login"
    assert consumed["outcome"] == "business_arrived"


@pytest.mark.parametrize("same_pixels", [True, False])
def test_unchanged_business_with_only_image_difference_is_not_arrival(same_pixels):
    after = _frame("after")
    if same_pixels:
        after = _analysis(
            _observation(),
            request=replace(
                _request(),
                capture_id="capture-after",
                screenshot_artifact_id="artifact-after",
                image_signature="sha256:" + "a" * 64,
                package_name="com.example.target",
            ),
        )
    proof = build_app_action_observation(**_scenario(after=after))
    assert proof.outcome == "unproven"
    assert not proof.arrival_proven and not proof.completed


def test_local_tab_change_completes_action_without_new_business_arrival():
    scenario = _scenario(
        before=_frame("before", local_state={"selected_tab": _anchor("推荐")}),
        after=_frame("after", local_state={"selected_tab": _anchor("最新")}),
    )
    proof = build_app_action_observation(**scenario)
    assert proof.outcome == "local_state_changed"
    assert proof.completed and not proof.arrival_proven
    assert len(proof.navigation_path) == 1


def _scroll_frame(role, offsets):
    frame = _frame(role, facts={"scroll_y": 0 if role == "before" else 400})
    candidates = tuple(
        AppVisualCandidate.build(
            source_capture_id=frame.capture_id,
            source_image_signature=frame.image_signature,
            source_width=frame.width,
            source_height=frame.height,
            source_orientation=frame.orientation,
            semantic_name=f"作品 {index}",
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(0.1, top, 0.7, top + 0.08),
            confidence=0.95,
            risk_level=InteractionRiskLevel.LOW,
            purpose="打开作品",
        )
        for index, top in enumerate(offsets)
    )
    values = {
        item.name: getattr(frame, item.name)
        for item in fields(frame)
        if item.name not in {"result_hash", "contract_version", "candidates"}
    }
    return AppVisualAnalysis.build(**values, candidates=candidates)


@pytest.mark.parametrize(
    "positions,completed",
    [
        ((0.25, 0.45), True),
        ((0.55, 0.75), False),
        ((0.25, 0.8), False),
        ((0.25,), False),
    ],
)
def test_scroll_requires_coherent_bound_anchors_not_self_reported_offset(positions, completed):
    proof = build_app_action_observation(
        **_scenario(
            InteractionOperation.SCROLL,
            before=_scroll_frame("before", (0.55, 0.75)),
            after=_scroll_frame("after", positions),
        )
    )
    assert proof.completed is completed
    assert proof.outcome == ("local_state_changed" if completed else "unproven")
    assert not proof.arrival_proven
    assert len(proof.navigation_path) == 1


@pytest.mark.parametrize("external", [False, True])
def test_back_requires_recorded_predecessor_and_in_scope_after_page(external):
    before = _frame("before", object_id="作品编号 123")
    after = _frame("after", package="com.external.ad" if external else None)
    proof = build_app_action_observation(
        **_scenario(
            InteractionOperation.BACK,
            before=before,
            after=after,
        )
    )
    assert proof.outcome == ("external" if external else "unproven")
    assert not proof.arrival_proven
    if not external:
        path = (confirmed_app_identity(after), confirmed_app_identity(before))
        proven = build_app_action_observation(
            **_scenario(
                InteractionOperation.BACK,
                before=before,
                after=after,
                path=path,
            )
        )
        assert proven.outcome == "business_arrived"
        assert proven.navigation_path == path[:1]


def test_drifted_source_does_not_join_unrelated_previous_navigation_path():
    scenario = _scenario(path=("old-home", "old-detail"))
    proof = build_app_action_observation(**scenario)
    assert proof.arrival_proven
    assert proof.navigation_path == tuple(
        confirmed_app_identity(scenario[key]) for key in ("before", "after")
    )
    assert "old-home" not in proof.navigation_path


@pytest.mark.parametrize(
    "operation", [InteractionOperation.BACK, InteractionOperation.WARM_RESTORE]
)
def test_package_recovery_only_confirms_current_app_without_business_arrival(operation):
    scenario = _scenario(
        operation,
        before=_frame("before", package="com.external.ad"),
        metadata={"package_boundary_recovery": True},
        path=("previous-home", "previous-detail"),
    )
    intent = scenario["turn"].decision
    scenario["turn"] = replace(
        scenario["turn"],
        decision=replace(
            intent,
            parameters={**intent.parameters, "package_name": "com.example.target"},
        ),
    )
    proof = build_app_action_observation(**scenario)
    assert proof.outcome == "surface_restored"
    assert proof.completed and proof.business_visible
    assert not proof.arrival_proven and not proof.observation_proven
    assert proof.navigation_path == (confirmed_app_identity(scenario["after"]),)


@pytest.mark.parametrize("fault", ["wrong_target", "inside_source", "popup", "external_after"])
def test_package_recovery_rejects_wrong_or_unproven_target(fault):
    scenario = _scenario(
        InteractionOperation.WARM_RESTORE,
        before=_frame("before", package=None if fault == "inside_source" else "com.external.ad"),
        after=_frame(
            "after",
            package="com.external.ad" if fault == "external_after" else None,
            page_state="obstructed" if fault == "popup" else "stable",
        ),
        metadata={"package_boundary_recovery": True},
    )
    intent = scenario["turn"].decision
    scenario["turn"] = replace(
        scenario["turn"],
        decision=replace(
            intent,
            parameters={
                **intent.parameters,
                "package_name": "wrong.package"
                if fault == "wrong_target"
                else "com.example.target",
            },
        ),
    )
    proof = build_app_action_observation(**scenario)
    assert not proof.completed and not proof.arrival_proven
    if fault == "external_after":
        assert proof.outcome == "external_return_failed"
        assert proof.reason == "recovery_still_outside_target_app"
        assert proof.binding["external_activity_kind"] == "other"
        assert proof.binding["external_package_name"] == "com.external.ad"


def test_package_recovery_failed_marks_browser_kind() -> None:
    scenario = _scenario(
        InteractionOperation.BACK,
        before=_frame("before", package="com.android.chrome"),
        after=_frame("after", package="com.android.chrome"),
        metadata={"package_boundary_recovery": True},
    )
    proof = build_app_action_observation(**scenario)
    assert proof.outcome == "external_return_failed"
    assert proof.binding["external_activity_kind"] == "browser"
    before = _frame("before", object_id="作品编号 123", package="com.external.ad")
    after = _frame("after")
    proof = build_app_action_observation(
        **_scenario(
            InteractionOperation.BACK,
            before=before,
            after=after,
            path=(confirmed_app_identity(after), confirmed_app_identity(before)),
        )
    )
    assert proof.outcome == "unproven"
    assert proof.reason == "return_source_relationship_unconfirmed"
    assert not proof.arrival_proven


@pytest.mark.parametrize("popup", [False, True])
def test_observe_completes_even_unchanged_popup_but_only_visible_business_is_coverable(popup):
    state = "popup" if popup else "stable"
    proof = build_app_action_observation(
        **_scenario(
            InteractionOperation.OBSERVE,
            before=_frame("before", page_state=state),
            after=_frame("after", page_state=state),
        )
    )
    assert proof.outcome == "observation_completed"
    assert proof.completed and not proof.arrival_proven
    assert proof.observation_proven is not popup


def test_popup_close_needs_visible_business_instead_of_replaced_overlay():
    before = _frame("before", page_state="popup")
    blocked = build_app_action_observation(
        **_scenario(before=before, after=_frame("after", page_state="popup")),
        popup_close=True,
    )
    assert not blocked.arrival_proven and not blocked.completed
    closed = build_app_action_observation(**_scenario(before=before), popup_close=True)
    assert closed.outcome == "obstruction_removed"
    assert closed.arrival_proven and len(closed.navigation_path) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "another-task"),
        ("session_id", "another-session"),
        ("parent_capture_id", "unrelated-before"),
        ("action_execution_id", "another-action"),
        ("artifact_ids", ("unrelated-image",)),
    ],
)
def test_action_proof_rejects_mixed_capture_binding(field, value):
    scenario = _scenario()
    turn = scenario["turn"]
    scenario["turn"] = replace(
        turn,
        coordination=replace(
            turn.coordination,
            after_capture=replace(turn.coordination.after_capture, **{field: value}),
        ),
    )
    proof = build_app_action_observation(**scenario)
    assert proof.reason == "action_capture_binding_mismatch"
    assert not proof.completed and not proof.arrival_proven


@pytest.mark.parametrize(
    "verification",
    [
        InteractionVerificationOutcome.NO_EFFECT,
        InteractionVerificationOutcome.EFFECT_UNEXPECTED,
    ],
)
def test_negative_verification_cannot_be_overridden_by_changed_page(verification):
    scenario = _scenario()
    turn = scenario["turn"]
    scenario["turn"] = replace(
        turn,
        coordination=replace(
            turn.coordination,
            interaction=replace(
                turn.coordination.interaction,
                verification_outcome=verification,
            ),
        ),
    )
    proof = build_app_action_observation(**scenario)
    assert proof.reason == "execution_verification_negative"
    assert not proof.completed


@pytest.mark.parametrize("changed", ["action", "image", "task", "hash"])
def test_frontier_rejects_replayed_or_tampered_action_proof(changed):
    scenario = _scenario()
    turn = scenario["turn"]
    successor = _successor(scenario, build_app_action_observation(**scenario))
    if changed == "action":
        turn = replace(
            turn,
            coordination=replace(
                turn.coordination,
                interaction=replace(
                    turn.coordination.interaction,
                    action_execution_id="another-action",
                ),
            ),
        )
    elif changed == "task":
        turn = replace(turn, task_id="another-task")
    else:
        observation = successor.frontier.observation
        metadata = dict(observation.metadata)
        if changed == "hash":
            metadata[APP_ACTION_OBSERVATION_KEY] = {
                **dict(metadata[APP_ACTION_OBSERVATION_KEY]),
                "outcome": "observation_completed",
            }
        successor = replace(
            successor,
            frontier=replace(
                successor.frontier,
                observation=replace(
                    observation,
                    metadata=metadata,
                    state_version="sha256:" + "c" * 64
                    if changed == "image"
                    else observation.state_version,
                ),
            ),
        )
    assert action_observation_for_successor(turn=turn, successor=successor) is None


def _tab_frame(
    role: str,
    *,
    page_type: str,
    page_state: str = "stable",
    confidence: float = 0.92,
    object_id: str | None = None,
    pixel_selected_labels: list[str] | None = None,
) -> AppVisualAnalysis:
    from services.api.app.domain.page.registry import get_default_page_type_registry
    from services.api.app.infrastructure.app_vision.llm import build_analysis_from_llm_payload

    analysis = build_analysis_from_llm_payload(
        request=replace(
            _request(),
            capture_id=f"capture-{role}",
            screenshot_artifact_id=f"artifact-{role}",
            image_signature="sha256:" + ("a" if role == "before" else "b") * 64,
            package_name="com.example.target",
        ),
        payload={
            "visual_page_type": page_type,
            "visual_page_type_confidence": confidence,
            "page_name": page_type,
            "page_name_source": "top_bar",
            "page_name_confidence": 0.9,
            "page_state": page_state,
            "visual_summary": page_type,
            "page_identity_observation": _observation(object_id=object_id),
            "local_state_observation": None,
            "surface_facts": {"visual_page_type_confidence": confidence},
        },
        page_types=get_default_page_type_registry(),
        model_id="tab-seek-test",
    )
    if pixel_selected_labels is None:
        return analysis
    # LLM payload 白名单会丢掉 pixel_selected_labels；用 build 回写并重算 result_hash。
    from dataclasses import fields as dataclass_fields

    values = {
        item.name: getattr(analysis, item.name)
        for item in dataclass_fields(analysis)
        if item.name not in {"result_hash", "contract_version", "surface_facts"}
    }
    return AppVisualAnalysis.build(
        **values,
        surface_facts={
            **dict(analysis.surface_facts),
            "pixel_selected_labels": list(pixel_selected_labels),
        },
    )


def _bottom_tab_seek_scenario(
    label: str,
    *,
    after_page_type: str,
    before_page_state: str = "stable",
    after_page_state: str = "stable",
    after_object_id: str | None = "目标业务",
    after_confidence: float = 0.92,
    expected: tuple[str, str] | None = None,
):
    metadata = {
        "bottom_navigation_tab_seek": True,
        "require_screenshot_evidence": True,
        "frontier_scope": "app",
    }
    if expected is not None:
        metadata[expected[0]] = expected[1]
    scenario = _scenario(
        before=_tab_frame("before", page_type="home", page_state=before_page_state),
        after=_tab_frame(
            "after",
            page_type=after_page_type,
            page_state=after_page_state,
            object_id=after_object_id,
            confidence=after_confidence,
        ),
        metadata=metadata,
    )
    return {
        **scenario,
        "candidate": replace(scenario["candidate"], semantic_target=f"底栏{label}"),
    }


def test_bottom_tab_seek_still_on_home_popup_is_unproven() -> None:
    """点精选后仍 home|popup：不得 tab_seek_screenshot_evidenced（防假 VISITED）。"""

    proof = build_app_action_observation(
        **_scenario(
            before=_tab_frame("before", page_type="home", page_state="popup"),
            after=_tab_frame("after", page_type="home", page_state="popup"),
            metadata={
                "bottom_navigation_tab_seek": True,
                "require_screenshot_evidence": True,
                "expected_next_page_type": "content_list",
                "frontier_scope": "app",
            },
        )
    )
    assert proof.outcome == "unproven"
    assert proof.outcome != "tab_seek_screenshot_evidenced"


def test_home_top_category_same_home_screenshot_change_is_evidenced() -> None:
    """上方分类同页 home 馈流：截图变化即留证，不得套用底栏切栏证明失败。"""

    proof = build_app_action_observation(
        **_scenario(
            before=_tab_frame("before", page_type="home", page_state="stable"),
            after=_tab_frame(
                "after",
                page_type="home",
                page_state="stable",
                object_id="国产流",
            ),
            metadata={
                "home_top_category_tab_seek": True,
                "require_screenshot_evidence": True,
                "expected_state_hint": "home_top_tab:国产",
                "frontier_scope": "local",
            },
        )
    )
    assert proof.outcome == "tab_seek_screenshot_evidenced"
    assert proof.reason == "home_top_category_screenshot_changed"


def test_bottom_tab_seek_evidenced_when_after_matches_expected_page() -> None:
    proof = build_app_action_observation(
        **_scenario(
            before=_tab_frame("before", page_type="home", page_state="popup"),
            after=_tab_frame(
                "after",
                page_type="content_list",
                page_state="stable",
                object_id="精选流",
            ),
            metadata={
                "bottom_navigation_tab_seek": True,
                "require_screenshot_evidence": True,
                "expected_next_page_type": "content_list",
                "frontier_scope": "app",
            },
        )
    )
    assert proof.outcome == "tab_seek_screenshot_evidenced"
    assert proof.reason == "after_page_matches_expected_tab"


def test_stable_ai_tab_seek_proves_arrival_and_extends_navigation_path() -> None:
    """稳定可信的 AI 切栏后态同时证明到达，并把目标身份追加到路径。"""

    before = _tab_frame("before", page_type="home", page_state="stable")
    after = _tab_frame(
        "after",
        page_type="video_detail",
        page_state="stable",
        object_id="AI 目标",
        confidence=0.92,
    )
    source_identity = confirmed_app_identity(before)
    target_identity = confirmed_app_identity(after)
    assert source_identity is not None and target_identity is not None
    scenario = _scenario(
        before=before,
        after=after,
        path=(source_identity,),
        metadata={
            "bottom_navigation_tab_seek": True,
            "require_screenshot_evidence": True,
            "frontier_scope": "app",
        },
    )
    scenario["candidate"] = replace(scenario["candidate"], semantic_target="底栏AI")

    proof = build_app_action_observation(**scenario)

    assert proof.outcome == "tab_seek_screenshot_evidenced"
    assert proof.arrival_proven
    assert proof.navigation_path == (source_identity, target_identity)


def test_bottom_tab_seek_evidenced_when_selected_label_moves() -> None:
    """页型仍判 home，但选中态从主页切到社区：须留证，避免死磕同栏。"""

    scenario = _scenario(
        before=_tab_frame(
            "before",
            page_type="home",
            page_state="stable",
            pixel_selected_labels=["主页"],
        ),
        after=_tab_frame(
            "after",
            page_type="home",
            page_state="stable",
            pixel_selected_labels=["社区"],
        ),
        metadata={
            "bottom_navigation_tab_seek": True,
            "require_screenshot_evidence": True,
            "expected_next_page_type": "post_list",
            "frontier_scope": "app",
        },
    )
    scenario = {
        **scenario,
        "candidate": replace(scenario["candidate"], semantic_target="底栏社区"),
    }
    proof = build_app_action_observation(**scenario)
    assert proof.outcome == "tab_seek_screenshot_evidenced"
    assert proof.reason == "after_selected_tab_matches_target"


def test_bottom_tab_seek_short_drama_player_is_evidenced() -> None:
    """点短剧后进 video_detail 播放器：须留证，禁止因期望 video_list 拒收。"""

    scenario = _scenario(
        before=_tab_frame(
            "before",
            page_type="home",
            page_state="stable",
            pixel_selected_labels=["主页"],
        ),
        after=_tab_frame(
            "after",
            page_type="video_detail",
            page_state="stable",
            object_id="短剧播放器",
        ),
        metadata={
            "bottom_navigation_tab_seek": True,
            "require_screenshot_evidence": True,
            "expected_next_page_type": "video_list",
            "frontier_scope": "app",
        },
    )
    scenario = {
        **scenario,
        "candidate": replace(scenario["candidate"], semantic_target="底栏短剧"),
    }
    proof = build_app_action_observation(**scenario)
    assert proof.outcome == "tab_seek_screenshot_evidenced"
    assert proof.reason == "after_page_short_drama_player_or_list"


@pytest.mark.parametrize(
    ("after_page_state", "after_confidence"),
    (("popup", 0.92), ("stable", 0.66)),
)
def test_selected_tab_seek_keeps_evidence_but_not_arrival_when_after_is_untrusted(
    after_page_state: str,
    after_confidence: float,
) -> None:
    """选中态可保留切栏截图证据，但遮挡或低可信后态不能证明到达。"""

    scenario = _scenario(
        before=_tab_frame(
            "before",
            page_type="home",
            page_state="stable",
            pixel_selected_labels=["主页"],
        ),
        after=_tab_frame(
            "after",
            page_type="home",
            page_state=after_page_state,
            object_id="社区目标",
            confidence=after_confidence,
            pixel_selected_labels=["社区"],
        ),
        metadata={
            "bottom_navigation_tab_seek": True,
            "require_screenshot_evidence": True,
            "expected_next_page_type": "post_list",
            "frontier_scope": "app",
        },
    )
    scenario["candidate"] = replace(scenario["candidate"], semantic_target="底栏社区")

    proof = build_app_action_observation(**scenario)

    assert proof.outcome == "tab_seek_screenshot_evidenced"
    assert proof.completed
    assert not proof.business_visible
    assert not proof.arrival_proven
    assert proof.navigation_path == (confirmed_app_identity(scenario["before"]),)


@pytest.mark.parametrize("label", ["短剧", "暗网", "AI", "全新标签"])
@pytest.mark.parametrize("after_page_type", ["payment", "video_detail"])
def test_bottom_tab_without_expected_uses_same_page_change_semantics_for_any_label(
    label, after_page_type
):
    proof = build_app_action_observation(
        **_bottom_tab_seek_scenario(label, after_page_type=after_page_type)
    )

    assert proof.outcome == "tab_seek_screenshot_evidenced"
    # 无 expected 时：普通栏用页型变化；短剧族允许直接进播放器／列表留证。
    assert proof.reason in {
        "unmapped_tab_page_changed",
        "after_page_short_drama_player_or_list",
    }
    assert proof.completed


@pytest.mark.parametrize("label", ["短剧", "暗网", "AI", "全新标签"])
def test_bottom_tab_without_expected_same_home_popup_stays_unproven(label):
    proof = build_app_action_observation(
        **_bottom_tab_seek_scenario(
            label,
            after_page_type="home",
            before_page_state="popup",
            after_page_state="popup",
            after_object_id=None,
        )
    )

    assert proof.outcome == "unproven"
    assert not proof.completed


@pytest.mark.parametrize("expected_key", ["expected_next_page_type", "expected_state_hint"])
def test_bottom_tab_explicit_expected_still_constrains_unmapped_label(expected_key):
    proof = build_app_action_observation(
        **_bottom_tab_seek_scenario(
            "全新标签",
            after_page_type="payment",
            expected=(expected_key, "video_detail"),
        )
    )

    assert proof.outcome != "tab_seek_screenshot_evidenced"
    assert not proof.tab_seek_evidenced


def test_unmapped_ai_tab_still_on_home_is_unproven() -> None:
    scenario = _scenario(
        before=_tab_frame("before", page_type="home", page_state="popup"),
        after=_tab_frame("after", page_type="home", page_state="popup"),
        metadata={
            "bottom_navigation_tab_seek": True,
            "require_screenshot_evidence": True,
            "frontier_scope": "app",
        },
    )
    scenario = {
        **scenario,
        "candidate": replace(scenario["candidate"], semantic_target="底栏AI科技"),
    }
    proof = build_app_action_observation(**scenario)
    assert proof.outcome == "unproven"


@pytest.mark.parametrize("label", ["短剧", "暗网", "AI", "全新标签"])
def test_uia_tab_identity_hint_is_not_a_visual_page_type(label):
    """UIA 的 tab:标签是入口身份，不能变成永远无法命中的页型预期。"""
    proof = build_app_action_observation(
        **_bottom_tab_seek_scenario(
            label,
            after_page_type="payment",
            expected=("expected_state_hint", f"tab:{label}"),
        )
    )

    assert proof.tab_seek_evidenced
    assert proof.reason == "unmapped_tab_page_changed"


@pytest.mark.parametrize(
    "expected_key,hint",
    [("expected_state_hint", "tab:新栏"), ("expected_next_page_type", "payment")],
)
@pytest.mark.parametrize(
    "page_state,object_id", [("loading", "目标业务"), ("popup", "目标业务"), ("stable", None)]
)
def test_tab_page_type_change_requires_confirmed_business_frame(
    expected_key, hint, page_state, object_id
):
    """仅靠加载帧、遮挡帧或未确认身份的页型变化不得证明切栏。"""
    proof = build_app_action_observation(
        **_bottom_tab_seek_scenario(
            "新栏",
            after_page_type="payment",
            after_page_state=page_state,
            after_object_id=object_id,
            after_confidence=0.66 if object_id is None else 0.92,
            expected=(expected_key, hint),
        )
    )

    assert not proof.tab_seek_evidenced
    assert not proof.completed
