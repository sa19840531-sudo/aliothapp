"""App Capture Playbook 解析与候选生成回归。"""

from __future__ import annotations

import hashlib

from services.api.app.application.app_capture_playbook import (
    build_playbook_candidates,
    build_playbook_objective,
    resolve_app_capture_playbook,
)
from services.api.app.application.app_screenshot_understanding import (
    UnderstandAppScreenshotRequest,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.app_vision.heuristic import (
    build_first_screen_escape_analysis,
    ensure_exploration_candidates,
)


def _png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _request(**overrides):
    png = _png()
    base = dict(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        screenshot_artifact_id="art-1",
        screenshot_png=png,
        image_signature=f"sha256:{hashlib.sha256(png).hexdigest()}",
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.app",
        activity=None,
        allowed_actions=(
            InteractionOperation.CLICK,
            InteractionOperation.SCROLL,
            InteractionOperation.BACK,
            InteractionOperation.OBSERVE,
        ),
        max_candidates=12,
    )
    base.update(overrides)
    return UnderstandAppScreenshotRequest(**base)


def test_resolve_playbook_by_apk_prefix() -> None:
    assert resolve_app_capture_playbook(apk_filename="91cg_1.3.5.apk").playbook_id == "91cg"
    assert resolve_app_capture_playbook(apk_filename="hlapp_x.apk").playbook_id == "hlapp"
    # 黑料／海角开屏证据固定为本体 1 帧 + 跳过／关闭后 1 帧。
    assert resolve_app_capture_playbook(apk_filename="hlapp_x.apk").splash_burst == 1
    assert (
        resolve_app_capture_playbook(
            apk_filename="hjll3.1.0.apk",
            package_name="app.p6228f.pa3",
        ).splash_burst
        == 1
    )
    assert resolve_app_capture_playbook(apk_filename="hlwapp_x.apk").playbook_id == "hlwapp"
    assert resolve_app_capture_playbook(apk_filename="sns_91sp_x.apk").playbook_id == "sns_91sp"
    assert resolve_app_capture_playbook(apk_filename="sns_911bl_x.apk").playbook_id == "sns_911bl"
    assert resolve_app_capture_playbook(apk_filename="sns_51aw_x.apk").playbook_id == "sns_51aw"
    assert resolve_app_capture_playbook(apk_filename="sns-hjsq_x.apk").playbook_id == "sns-hjsq"
    assert resolve_app_capture_playbook(apk_filename="dx093j_x.apk").playbook_id == "dx093j"
    assert (
        resolve_app_capture_playbook(
            apk_filename="hjll3.1.0.apk",
            package_name="app.p6228f.pa3",
        ).playbook_id
        == "hjll"
    )
    assert (
        resolve_app_capture_playbook(
            apk_filename="51pc_5.8.3.apk",
            package_name="vip.dibwo.txbazn",
        ).playbook_id
        == "pc51"
    )


def test_playbook_objective_mentions_tabs_and_constraints() -> None:
    playbook = resolve_app_capture_playbook(apk_filename="sns-hjsq_demo.apk")
    text = build_playbook_objective(playbook)
    assert "sns-hjsq" in text or "untitled6" in text
    assert "主页" in text
    assert "禁止" in text
    assert "脚本动作课纲" in text
    assert "tab:主页" in text or "主页" in text


def test_script_curriculum_teaches_91cg_phases() -> None:
    from services.api.app.application.app_script_curriculum import (
        build_script_curriculum_payload,
        extract_playbook_id_from_objective,
        resolve_playbook_by_id,
    )

    playbook = resolve_app_capture_playbook(apk_filename="91cg_1.3.5.apk")
    objective = build_playbook_objective(playbook)
    assert extract_playbook_id_from_objective(objective) == "91cg"
    assert resolve_playbook_by_id("91cg") is not None
    payload = build_script_curriculum_payload(playbook)
    assert payload["script_ref"] == "untitled.py"
    phase_ids = [p["phase_id"] for p in payload["phases"]]
    # 91cg／untitled.py：等稳→外链→右上跳过(close_1/2/3图)→白圈确认→四栏
    assert "wait_stable" in phase_ids
    assert "ad_outbound" in phase_ids
    assert "tap_skip" in phase_ids
    assert phase_ids.index("ad_outbound") < phase_ids.index("tap_skip")
    assert phase_ids.index("tap_skip") < phase_ids.index("close_popup")
    assert "tab:社区" in phase_ids
    assert payload["tab_swipe_plan"][0]["tab"] == "社区"
    assert payload["current_phase_hint"]["phase_id"] in phase_ids
    assert "untitled.py" in objective
    assert payload["script_notes"]
    assert payload["forbid_hints"]


def test_script_curriculum_teaches_all_eight_packages() -> None:
    from services.api.app.application.app_script_curriculum import (
        build_script_curriculum_payload,
        build_script_curriculum_phases,
    )

    cases = [
        ("dx093j_x.apk", "untitled7.py", "splash_burst", "tab:ACG"),
        ("sns-hjsq_x.apk", "untitled6.py", "tap_skip", "tab:应用"),
        ("sns_51aw_x.apk", "untitled5.py", "tap_skip", "tab:大赛"),
        ("sns_911bl_x.apk", "untitled4.py", "system_notice", "tab:看片"),
        ("sns_91sp_x.apk", "untitled3.py", "system_notice", "tab:聊天广场"),
        ("hlwapp_x.apk", "untitled2.py", "shenqi", "tab:吃瓜"),
        ("hlapp_x.apk", "untitled1.py", "age_gate", "tab:圈子"),
        ("91cg_x.apk", "untitled.py", "tap_skip", "tab:AI科技"),
    ]
    for apk, script_ref, must_phase, must_tab in cases:
        playbook = resolve_app_capture_playbook(apk_filename=apk)
        payload = build_script_curriculum_payload(playbook)
        assert payload["script_ref"] == script_ref, apk
        phase_ids = [p["phase_id"] for p in build_script_curriculum_phases(playbook)]
        assert must_phase in phase_ids, (apk, must_phase, phase_ids)
        assert must_tab in phase_ids, (apk, must_tab, phase_ids)
        assert payload["forbid_hints"] or playbook.playbook_id == "default"
        assert "学习" in payload["learning_rule"] or "课纲" in payload["learning_rule"]


def test_curriculum_progress_is_versioned_monotonic_and_independent_from_page_depth() -> None:
    from services.api.app.application.app_script_curriculum import (
        AppCurriculumProgress,
        build_app_curriculum_progress,
        build_script_curriculum_payload,
    )

    playbook = resolve_app_capture_playbook(apk_filename="91cg_x.apk")
    progress = build_app_curriculum_progress(playbook, active_tabs=("社区", "片库"))

    assert progress.current_phase_id == "wait_stable"
    assert (
        progress.advance(
            phase_id="wait_stable",
            action_succeeded=False,
        )
        == progress
    )
    progressed = progress.advance(
        phase_id="wait_stable",
        action_succeeded=True,
    )
    restored = AppCurriculumProgress.from_dict(progressed.to_dict())
    payload = build_script_curriculum_payload(playbook, progress=restored)

    assert restored.current_phase_id == "ad_outbound"
    assert restored.completed_phase_ids == ("wait_stable",)
    assert restored.phase_cursor == 1
    assert [item["phase_id"] for item in payload["phases"]][-2:] == [
        "tab:社区",
        "tab:片库",
    ]
    assert "navigation_depth" not in payload
    assert payload["phase_progress"]["phase_cursor"] == 1


def test_hlw_tap_skip_accepts_explicit_skip_semantics_only() -> None:
    from services.api.app.application.app_script_curriculum import (
        build_app_curriculum_progress,
        curriculum_action_matches_current_phase,
    )

    playbook = resolve_app_capture_playbook(apk_filename="hlwapp_1.7.2_260819_3.apk")
    progress = build_app_curriculum_progress(playbook)
    while progress.current_phase_id != "tap_skip":
        assert progress.current_phase_id is not None
        progress = progress.advance(
            phase_id=progress.current_phase_id,
            action_succeeded=True,
        )

    assert curriculum_action_matches_current_phase(
        playbook=playbook,
        progress=progress,
        action_type="click",
        semantic_terms=("跳过", "跳过开屏广告"),
    )
    assert curriculum_action_matches_current_phase(
        playbook=playbook,
        progress=progress,
        action_type="click",
        semantic_terms=("tap_skip",),
    )
    assert not curriculum_action_matches_current_phase(
        playbook=playbook,
        progress=progress,
        action_type="click",
        semantic_terms=("关闭弹窗", "关闭当前遮挡"),
    )
    assert not curriculum_action_matches_current_phase(
        playbook=playbook,
        progress=progress,
        action_type="click",
        semantic_terms=("关闭弹窗", "tap_skip"),
    )
    assert not curriculum_action_matches_current_phase(
        playbook=playbook,
        progress=progress,
        action_type="observe",
        semantic_terms=("跳过",),
    )


def test_hlw_curriculum_includes_safe_detail_and_profile_coverage_phases() -> None:
    from services.api.app.application.app_script_curriculum import (
        build_app_curriculum_progress,
        build_script_curriculum_phases,
        curriculum_action_matches_current_phase,
    )

    playbook = resolve_app_capture_playbook(apk_filename="hlwapp_1.7.2_260819_3.apk")
    phases = build_script_curriculum_phases(playbook)
    phase_ids = [str(item["phase_id"]) for item in phases]

    assert phase_ids[-4:] == [
        "content_detail",
        "content_detail_back",
        "profile",
        "recover_back",
    ]
    assert "禁止点广告本体" in str(phases[-4]["forbid"])
    assert "禁止充值" in str(phases[-2]["forbid"])

    progress = build_app_curriculum_progress(playbook)
    assert progress.required_action_count == 28
    assert playbook.max_actions == 48
    assert playbook.max_actions - progress.required_action_count >= 20
    assert playbook.tab_swipe_plan == (
        ("吃瓜", 2),
        ("AI科技", 2),
        ("社区", 2),
        ("视频", 2),
        ("福利", 2),
    )
    while progress.current_phase_id != "content_detail":
        assert progress.current_phase_id is not None
        for _ in range(progress.current_phase_required_actions):
            progress = progress.advance(
                phase_id=progress.current_phase_id,
                action_succeeded=True,
            )

    assert curriculum_action_matches_current_phase(
        playbook=playbook,
        progress=progress,
        action_type="click",
        semantic_terms=("content_detail", "打开普通内容详情"),
    )
    progress = progress.advance(phase_id="content_detail", action_succeeded=True)
    assert curriculum_action_matches_current_phase(
        playbook=playbook,
        progress=progress,
        action_type="back",
        semantic_terms=("系统返回",),
    )
    progress = progress.advance(phase_id="content_detail_back", action_succeeded=True)
    assert curriculum_action_matches_current_phase(
        playbook=playbook,
        progress=progress,
        action_type="click",
        semantic_terms=("tab:我的", "个人中心"),
    )


def test_curriculum_progress_records_and_consumes_future_tap_skip_witness() -> None:
    from services.api.app.application.app_script_curriculum import (
        AppCurriculumProgress,
        build_app_curriculum_progress,
        candidate_explicitly_targets_tap_skip,
    )

    playbook = resolve_app_capture_playbook(apk_filename="hlwapp_1.7.2_260819_3.apk")
    progress = build_app_curriculum_progress(playbook)
    while progress.current_phase_id != "wait_line":
        assert progress.current_phase_id is not None
        progress = progress.advance(
            phase_id=progress.current_phase_id,
            action_succeeded=True,
        )

    witnessed = progress.record_phase_witness(
        phase_id="tap_skip",
        action_succeeded=True,
    )
    assert witnessed.current_phase_id == "wait_line"
    assert witnessed.phase_cursor == progress.phase_cursor
    assert witnessed.witnessed_phase_ids == ("tap_skip",)
    restored = AppCurriculumProgress.from_dict(witnessed.to_dict())
    assert restored == witnessed

    legacy_payload = witnessed.to_dict()
    legacy_payload.pop("witnessed_phase_ids")
    assert AppCurriculumProgress.from_dict(legacy_payload).witnessed_phase_ids == ()

    while restored.current_phase_id != "wait_init":
        assert restored.current_phase_id is not None
        restored = restored.advance(
            phase_id=restored.current_phase_id,
            action_succeeded=True,
        )
    assert restored.witnessed_phase_ids == ()
    assert candidate_explicitly_targets_tap_skip(
        action_type="click",
        risk_level="low",
        semantic_target="跳过广告",
        purpose="关闭启动广告并进入应用",
        expected_state_hint="广告关闭后显示主页面",
    )


def test_tap_skip_witness_rejects_popup_ad_body_and_unsafe_candidates() -> None:
    from services.api.app.application.app_script_curriculum import (
        candidate_explicitly_targets_tap_skip,
    )

    base = {
        "action_type": "click",
        "risk_level": "low",
        "semantic_target": "跳过",
        "purpose": "跳过开屏广告",
        "expected_state_hint": "tap_skip",
    }
    assert not candidate_explicitly_targets_tap_skip(
        **{**base, "semantic_target": "关闭弹窗", "purpose": "关闭当前遮挡弹层"}
    )
    assert not candidate_explicitly_targets_tap_skip(
        **{**base, "semantic_target": "关闭", "purpose": "关闭当前浮层"}
    )
    assert not candidate_explicitly_targets_tap_skip(
        **{**base, "purpose": "点击广告本体"}
    )
    assert not candidate_explicitly_targets_tap_skip(
        **{**base, "semantic_target": "skip", "purpose": "click ad body"}
    )
    assert not candidate_explicitly_targets_tap_skip(
        **{**base, "risk_level": "high"}
    )
    assert not candidate_explicitly_targets_tap_skip(
        **{**base, "action_type": "observe"}
    )


def test_tab_phase_first_clicks_named_tab_then_only_scrolls() -> None:
    from services.api.app.application.app_script_curriculum import (
        build_app_curriculum_progress,
        build_script_curriculum_payload,
    )

    playbook = resolve_app_capture_playbook(apk_filename="unknown.apk")
    progress = build_app_curriculum_progress(playbook)
    while progress.current_phase_id != "tab:首页":
        for _ in range(progress.current_phase_required_actions):
            progress = progress.advance(
                phase_id=progress.current_phase_id,
                action_succeeded=True,
            )

    first = build_script_curriculum_payload(playbook, progress=progress)
    progress = progress.advance(phase_id="tab:首页", action_succeeded=True)
    scrolling = build_script_curriculum_payload(playbook, progress=progress)

    assert first["current_phase_hint"]["prefer_actions"] == ["click"]
    assert first["current_phase_hint"]["prefer_hints"] == ["tab:首页", "首页"]
    assert scrolling["current_phase_hint"]["prefer_actions"] == ["scroll"]
    assert scrolling["current_phase_hint"]["prefer_hints"] == [
        "swipe_left_edge",
        "scroll",
    ]


def test_playbook_fallback_candidates_are_safe_non_clicks_without_fixed_targets() -> None:
    playbook = resolve_app_capture_playbook(apk_filename="hlwapp_1.apk")
    analysis = build_playbook_candidates(
        _request(package_name="com.hlwapp.demo"),
        playbook,
        reason="test",
    )
    assert analysis.candidates
    assert all(
        candidate.action_type
        in {
            InteractionOperation.OBSERVE,
            InteractionOperation.SCROLL,
            InteractionOperation.BACK,
        }
        for candidate in analysis.candidates
    )
    assert all(candidate.normalized_bounds is None for candidate in analysis.candidates)
    assert not any(
        candidate.expected_state_hint in {"tap_skip", "ad_outbound", "confirm"}
        for candidate in analysis.candidates
    )


def test_unknown_apk_does_not_inject_historical_playbook_candidates() -> None:
    request = _request(
        package_name="com.unknown.uploaded_apk",
        capture_id="capture-unknown",
    )
    playbook = resolve_app_capture_playbook(
        apk_filename="brand_new_unknown_2026.apk",
        package_name=request.package_name,
    )

    analysis = build_playbook_candidates(request, playbook, reason="unknown apk")

    assert playbook.playbook_id == "default"
    assert all(
        candidate.action_type
        in {
            InteractionOperation.OBSERVE,
            InteractionOperation.SCROLL,
            InteractionOperation.BACK,
        }
        for candidate in analysis.candidates
    )
    assert all(candidate.normalized_bounds is None for candidate in analysis.candidates)
    assert all(
        candidate.source_capture_id == request.capture_id for candidate in analysis.candidates
    )
    assert all(
        candidate.source_image_signature == request.image_signature
        for candidate in analysis.candidates
    )


def test_existing_llm_candidates_are_preserved_without_playbook_injection() -> None:
    request = _request()
    playbook = resolve_app_capture_playbook(package_name=request.package_name)
    existing = build_playbook_candidates(request, playbook, reason="seed").candidates[:1]
    analysis = build_playbook_candidates(request, playbook, reason="existing")
    analysis = analysis.__class__.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type="stable",
        visual_summary="LLM 当前屏候选",
        key_elements=("当前屏",),
        candidates=existing,
        provider_id="llm-app-vision",
        model_id="test",
        package_name=request.package_name,
        activity=request.activity,
    )

    restored = ensure_exploration_candidates(analysis, request)

    assert restored is analysis
    assert restored.candidates == existing


def test_91cg_playbook_does_not_inject_historical_clicks_without_visual_facts() -> None:
    """课纲仍记录历史节奏，但 Playbook 兜底不得凭历史坐标注入点击。"""
    playbook = resolve_app_capture_playbook(apk_filename="91cg_1.3.5.apk")
    analysis = build_playbook_candidates(
        _request(package_name="me.mfanv.npymci"),
        playbook,
        reason="91cg",
    )
    assert all(
        c.action_type
        in {
            InteractionOperation.OBSERVE,
            InteractionOperation.SCROLL,
            InteractionOperation.BACK,
        }
        for c in analysis.candidates
    )
    assert all(c.normalized_bounds is None for c in analysis.candidates)
    curriculum_module = __import__(
        "services.api.app.application.app_script_curriculum",
        fromlist=["build_app_curriculum_progress", "build_script_curriculum_payload"],
    )
    progress = curriculum_module.build_app_curriculum_progress(playbook)
    progress = progress.advance(phase_id="wait_stable", action_succeeded=True)
    payload = curriculum_module.build_script_curriculum_payload(
        playbook,
        progress=progress,
    )
    assert payload["current_phase_hint"]["phase_id"] == "ad_outbound"
    progress = progress.advance(phase_id="ad_outbound", action_succeeded=True)
    assert progress.current_phase_id == "tap_skip"
    next_payload = curriculum_module.build_script_curriculum_payload(
        playbook,
        progress=progress,
    )
    assert next_payload["current_phase_hint"]["phase_id"] == "tap_skip"
    assert payload["expected_evidence_shots"]
    assert any("shequ" in s for s in payload["expected_evidence_shots"])
    assert "tap_skip" in [p["phase_id"] for p in payload["phases"]]


def test_heuristic_uses_package_independent_dynamic_safe_candidates() -> None:
    analysis = build_first_screen_escape_analysis(_request(), reason="llm down")
    assert analysis.provider_id == "heuristic-app-vision"
    assert analysis.model_id == "dynamic-safe-frontier-v2"
    assert len(analysis.candidates) == 3
    assert {c.action_type for c in analysis.candidates} == {
        InteractionOperation.BACK,
        InteractionOperation.OBSERVE,
        InteractionOperation.SCROLL,
    }
    assert any(c.action_type is InteractionOperation.BACK for c in analysis.candidates)
    assert any(c.action_type is InteractionOperation.SCROLL for c in analysis.candidates)
