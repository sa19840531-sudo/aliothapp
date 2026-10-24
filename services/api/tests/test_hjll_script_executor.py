"""HJLL 只用 hjll.py 的順序與落點，不採用通用候選框。"""

from services.api.app.application.app_hjll_script import (
    HJLL_SCRIPT_INDEX_KEY,
    HJLL_SCRIPT_REASON_CODE,
    apply_hjll_script_progress,
    hjll_script_override,
    hjll_script_steps,
)
from services.api.app.domain.exploration import AppExplorationGoal
from services.api.app.domain.interaction import (
    CommandPolicy,
    CommandPolicyError,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.tests.test_maf_explorer_agent import _work


def _hjll_work(*, key_elements: tuple[str, ...] | None = None, **metadata: object):
    return _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        page_type="popup",
        page_state="popup",
        key_elements=key_elements,
        observation_metadata={
            "package_name": "com.hj.ttthree",
            **metadata,
        },
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("com.hj.ttthree",),
        ),
        allowed_operations=(InteractionOperation.CLICK, InteractionOperation.OBSERVE),
    )


def test_hjll_script_matches_recorded_points() -> None:
    steps = hjll_script_steps()
    by_tag = {step.tag: step for step in steps}
    assert by_tag["01"].semantic == "开屏本体"
    assert "02" not in by_tag
    assert steps[1].tag == "close"
    assert by_tag["close"].x == 0.866 and by_tag["close"].y == 0.100
    circular = [step for step in steps if step.tag.startswith("x")]
    assert [step.tag for step in circular] == [
        "x03",
        "x05",
        "x06",
        "x07",
        "x08",
        "x09",
        "x10",
        "x11",
    ]
    assert "x04" not in by_tag
    assert {(step.x, step.y) for step in circular} == {(0.50, 0.80)}
    assert by_tag["top-最新"].x == 0.070 and by_tag["top-最新"].y == 0.131
    assert by_tag["top-推荐"].x == 0.431
    assert by_tag["top-国产"].x == 0.572
    assert by_tag["top-动漫"].x == 0.780
    assert by_tag["top-漫画"].x == 0.640
    assert by_tag["top-漫画-back"].x == 0.067 and by_tag["top-漫画-back"].y == 0.076
    assert by_tag["top-最新-back"].x == 0.067 and by_tag["top-最新-back"].y == 0.076
    assert by_tag["bottom-短剧"].x == 0.267 and by_tag["bottom-短剧"].y == 0.900
    assert by_tag["短剧2"].settle_ms == 30_000
    assert by_tag["短剧4"].action is InteractionOperation.OBSERVE
    assert by_tag["短剧-back"].x == 0.043 and by_tag["短剧-back"].y == 0.097
    assert by_tag["短剧-back"].action is InteractionOperation.CLICK
    assert by_tag["pay-unlock"].x == 0.501 and by_tag["pay-unlock"].y == 0.790
    assert by_tag["pay-now"].x == 0.499 and by_tag["pay-now"].y == 0.868
    assert by_tag["pay-now-2"].x == 0.494 and by_tag["pay-now-2"].y == 0.856
    assert by_tag["pay-known"].x == 0.490 and by_tag["pay-known"].y == 0.742
    assert by_tag["pay-cancel"].x == 0.272 and by_tag["pay-cancel"].y == 0.893
    assert by_tag["pay-browser-back"].action is InteractionOperation.BACK
    assert by_tag["pay-success"].x == 0.487 and by_tag["pay-success"].y == 0.658
    assert by_tag["pay-back"].x == 0.059 and by_tag["pay-back"].y == 0.080
    assert by_tag["bottom-暗网"].x == 0.421
    assert steps.index(by_tag["pay-unlock"]) == steps.index(by_tag["bottom-暗网"]) + 1
    assert steps.index(by_tag["bottom-AI"]) > steps.index(by_tag["pay-back"])
    assert by_tag["bottom-我的"].x == 0.890 and by_tag["bottom-我的"].y == 0.900
    assert "save-credential" not in by_tag
    assert "saved-credential" not in by_tag
    assert "我已保存" not in by_tag
    assert steps[-1].tag == "bottom-我的"


def test_hjll_recorded_script_still_runs_in_dynamic_frontier() -> None:
    work = _hjll_work(app_runtime_policy={"exploration_mode": "dynamic_frontier"})

    decision = hjll_script_override(work)
    assert decision is not None
    assert decision.parameters["hjll_script"] is True
    assert decision.parameters["candidate_id"] == "hjll-script:0:01"


def test_explicit_curriculum_keeps_existing_script() -> None:
    work = _hjll_work(app_runtime_policy={"exploration_mode": "playbook_curriculum"})

    assert hjll_script_override(work).parameters["hjll_script"] is True


def test_hjll_script_ignores_generic_candidates_and_other_packages() -> None:
    decision = hjll_script_override(_hjll_work())
    assert decision is not None
    assert decision.parameters["hjll_script"] is True
    assert decision.parameters["candidate_id"] == "hjll-script:0:01"
    assert decision.action_type is InteractionOperation.OBSERVE
    assert decision.semantic_target == "开屏本体"
    assert decision.parameters.get("splash_body_evidence") is True
    close = hjll_script_override(_hjll_work(**{HJLL_SCRIPT_INDEX_KEY: 1}))
    assert close is not None
    assert close.parameters.get("candidate_id") == "hjll-script:1:close"
    assert close.parameters.get("splash_skip_after_only") is True

    other = _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        observation_metadata={"package_name": "top.qkjzg.zfiufm"},
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("top.qkjzg.zfiufm",),
        ),
    )
    assert hjll_script_override(other) is None


def test_hjll_script_taps_load_fail_retry_without_advancing() -> None:
    decision = hjll_script_override(
        _hjll_work(
            visual_summary="加载失败，点击重试",
            **{HJLL_SCRIPT_INDEX_KEY: 2},
        )
    )
    assert decision is not None
    assert decision.parameters["candidate_id"] == "hjll-script:load_retry:load_retry"
    assert decision.semantic_target == "点击重试"
    result: dict[str, object] = {}
    apply_hjll_script_progress(
        result,
        prior={HJLL_SCRIPT_INDEX_KEY: 2},
        completed_action=type(
            "Action", (), {"candidate_id": "hjll-script:load_retry:load_retry"}
        )(),
        action_succeeded=True,
    )
    assert result[HJLL_SCRIPT_INDEX_KEY] == 2


def test_hjll_vip_card_presses_in_app_back_once_more() -> None:
    steps = hjll_script_steps()
    back_at = next(index for index, step in enumerate(steps) if step.tag == "top-推荐-back")
    decision = hjll_script_override(
        _hjll_work(
            visual_summary="本片试看结束 国庆尊享卡",
            **{HJLL_SCRIPT_INDEX_KEY: back_at},
        )
    )
    assert decision is not None
    assert decision.action_type is InteractionOperation.CLICK
    assert decision.parameters["candidate_id"] == "hjll-script:vip_card_back:vip_card_back"
    bounds = decision.parameters["normalized_bounds"]
    assert (bounds["left"] + bounds["right"]) / 2 == 0.067
    assert (bounds["top"] + bounds["bottom"]) / 2 == 0.076

    result: dict[str, object] = {}
    apply_hjll_script_progress(
        result,
        prior={HJLL_SCRIPT_INDEX_KEY: back_at},
        completed_action=type(
            "Action", (), {"candidate_id": "hjll-script:vip_card_back:vip_card_back"}
        )(),
        action_succeeded=True,
    )
    assert result[HJLL_SCRIPT_INDEX_KEY] == back_at

    again = hjll_script_override(
        _hjll_work(
            visual_summary="本片试看结束 国庆尊享卡",
            **{
                HJLL_SCRIPT_INDEX_KEY: back_at,
                "hjll_vip_card_extra_back_index": back_at,
            },
        )
    )
    assert again is not None
    assert again.parameters["candidate_id"] == f"hjll-script:{back_at}:top-推荐-back"

    plain = hjll_script_override(_hjll_work(**{HJLL_SCRIPT_INDEX_KEY: back_at}))
    assert plain is not None
    assert plain.parameters["candidate_id"] == f"hjll-script:{back_at}:top-推荐-back"

    pay_at = next(index for index, step in enumerate(steps) if step.tag == "pay-success")
    pay = hjll_script_override(
        _hjll_work(
            visual_summary="国庆尊享卡",
            **{HJLL_SCRIPT_INDEX_KEY: pay_at},
        )
    )
    assert pay is not None
    assert pay.parameters["candidate_id"] == f"hjll-script:{pay_at}:pay-success"


def test_hjll_script_age_gate_then_advances_only_after_success() -> None:
    age = hjll_script_override(_hjll_work(visual_summary="海角影视 我已满18岁"))
    assert age is not None
    assert age.parameters["candidate_id"] == "hjll-script:age:age_gate"
    bounds = age.parameters["normalized_bounds"]
    assert round((bounds["left"] + bounds["right"]) / 2, 3) == 0.465
    assert round((bounds["top"] + bounds["bottom"]) / 2, 3) == 0.592

    from_elements = hjll_script_override(
        _hjll_work(
            visual_summary="年龄确认门禁弹窗需确认满18岁",
            key_elements=("海角影视", "未满18岁禁止进入", "我已满18岁", "内有成人内容"),
        )
    )
    assert from_elements is not None
    assert from_elements.parameters["candidate_id"] == "hjll-script:age:age_gate"

    close = hjll_script_override(_hjll_work(**{HJLL_SCRIPT_INDEX_KEY: 1}))
    assert close is not None
    assert close.semantic_target == "关闭开屏广告"
    assert close.parameters.get("splash_skip_after_only") is True
    close_bounds = close.parameters["normalized_bounds"]
    assert (close_bounds["left"] + close_bounds["right"]) / 2 == 0.866
    assert (close_bounds["top"] + close_bounds["bottom"]) / 2 == 0.100

    result: dict[str, object] = {}
    apply_hjll_script_progress(
        result,
        prior={},
        completed_action=type("Action", (), {"candidate_id": "hjll-script:0:01"})(),
        action_succeeded=True,
    )
    assert result[HJLL_SCRIPT_INDEX_KEY] == 1
    stalled: dict[str, object] = {}
    apply_hjll_script_progress(
        stalled,
        prior={HJLL_SCRIPT_INDEX_KEY: 1},
        completed_action=type("Action", (), {"candidate_id": "hjll-script:1:close"})(),
        action_succeeded=False,
    )
    assert stalled[HJLL_SCRIPT_INDEX_KEY] == 1


def test_hjll_browser_after_pay_known_uses_cloud_phone_back() -> None:
    steps = hjll_script_steps()
    known_at = next(index for index, step in enumerate(steps) if step.tag == "pay-known")
    decision = hjll_script_override(
        _hjll_work(
            package_name="com.sec.android.app.sbrowser",
            **{HJLL_SCRIPT_INDEX_KEY: known_at + 1},
        )
    )
    assert decision is not None
    assert decision.action_type is InteractionOperation.BACK
    assert decision.parameters["candidate_id"] == "hjll-script:browser_back:browser_back"


def test_hjll_play_store_after_pay_known_uses_cloud_phone_back() -> None:
    steps = hjll_script_steps()
    known_at = next(index for index, step in enumerate(steps) if step.tag == "pay-known")
    decision = hjll_script_override(
        _hjll_work(
            package_name="com.android.vending",
            **{HJLL_SCRIPT_INDEX_KEY: known_at + 1},
        )
    )
    assert decision is not None
    assert decision.action_type is InteractionOperation.BACK
    assert decision.parameters["candidate_id"] == "hjll-script:browser_back:browser_back"


def test_hjll_skips_download_steps_after_back_returns_to_app() -> None:
    steps = hjll_script_steps()
    cancel_at = next(index for index, step in enumerate(steps) if step.tag == "pay-cancel")
    return_at = next(index for index, step in enumerate(steps) if step.tag == "pay-return")
    decision = hjll_script_override(
        _hjll_work(**{HJLL_SCRIPT_INDEX_KEY: cancel_at})
    )
    assert decision is not None
    assert decision.action_type is InteractionOperation.OBSERVE
    assert decision.parameters["candidate_id"] == f"hjll-script:{return_at}:pay-return"


def test_hjll_script_keeps_backing_out_of_browser_before_community() -> None:
    steps = hjll_script_steps()
    cancel_at = next(index for index, step in enumerate(steps) if step.tag == "pay-cancel")
    decision = hjll_script_override(
        _hjll_work(
            package_name="com.sec.android.app.sbrowser",
            **{HJLL_SCRIPT_INDEX_KEY: cancel_at + 2},
        )
    )
    assert decision is not None
    assert decision.action_type is InteractionOperation.BACK
    assert decision.parameters["candidate_id"] == "hjll-script:browser_back:browser_back"
    result: dict[str, object] = {}
    apply_hjll_script_progress(
        result,
        prior={HJLL_SCRIPT_INDEX_KEY: cancel_at + 2},
        completed_action=type(
            "Action", (), {"candidate_id": "hjll-script:browser_back:browser_back"}
        )(),
        action_succeeded=True,
    )
    assert result[HJLL_SCRIPT_INDEX_KEY] == cancel_at + 2


def test_hjll_script_completion_is_not_frontier_exhaustion() -> None:
    done = hjll_script_override(
        _hjll_work(**{HJLL_SCRIPT_INDEX_KEY: len(hjll_script_steps())})
    )
    assert done is not None
    assert done.reason_code == HJLL_SCRIPT_REASON_CODE
    assert done.coverage_summary["hjll_script_completed"] is True
    assert done.coverage_summary["coverage_complete"] is True
    assert done.coverage_summary["visual_quality_satisfied"] is True


def test_hjll_recorded_payment_tap_passes_at_medium() -> None:
    steps = hjll_script_steps()
    policy = CommandPolicy.default()
    pay_now = None
    for tag in ("pay-now", "pay-now-2", "pay-success", "pay-back"):
        index = next(i for i, step in enumerate(steps) if step.tag == tag)
        decision = hjll_script_override(_hjll_work(**{HJLL_SCRIPT_INDEX_KEY: index}))
        assert decision is not None
        assert decision.risk_level is InteractionRiskLevel.MEDIUM
        policy.validate(decision)
        assert (
            policy.validate_resolved_target(
                decision,
                trusted_target_texts=(decision.semantic_target, decision.purpose),
                target_trust="app_visual_candidate",
            )
            is InteractionRiskLevel.MEDIUM
        )
        if tag == "pay-now":
            pay_now = decision
    assert pay_now is not None
    unmarked = pay_now.__class__(
        **{**pay_now.__dict__, "parameters": {}, "risk_level": InteractionRiskLevel.LOW}
    )
    try:
        policy.validate_resolved_target(
            unmarked,
            trusted_target_texts=("立即支付",),
            target_trust="app_visual_candidate",
        )
    except CommandPolicyError as exc:
        assert exc.error_code == "COMMAND_TARGET_RISK_UNDERRATED"
    else:
        raise AssertionError("unmarked payment tap must stay blocked")
