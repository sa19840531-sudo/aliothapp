"""task_progress 共享口径的单元测试：展示态映射、汇总优先级与旅程规范化。"""

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.task_progress import (
    RULE_DISPLAY_COVERAGE_GAP,
    RULE_DISPLAY_DETECT_ERROR,
    RULE_DISPLAY_EVIDENCE_INSUFFICIENT,
    RULE_DISPLAY_FAILED,
    RULE_DISPLAY_MANUAL_REVIEW,
    RULE_DISPLAY_NOT_APPLICABLE,
    RULE_DISPLAY_NOT_DETECTED,
    RULE_DISPLAY_NOT_EXECUTED,
    RULE_DISPLAY_PASSED,
    RULE_PREFLIGHT_DISPOSITION_REASONS,
    effective_rule_execution_reason,
    humanize_qc_text,
    normalize_journey_steps,
    page_type_display_name,
    rollup_display_status,
    rule_display_label,
    rule_display_status,
    rule_execution_counts_as_completed,
    rule_execution_counts_as_used,
    rule_final_display_status,
    rule_package_fallback,
    rule_requires_human_confirmation,
    running_at_full_progress_should_enter_review,
    severity_display,
    trigger_display_fallback,
    whole_task_progress_percent,
)


@pytest.mark.parametrize(
    "reason",
    [
        "exploration_not_started",
        "candidate_not_executed",
        "auth_requirement_not_met",
        "not_matched_by_run",
        *RULE_PREFLIGHT_DISPOSITION_REASONS,
    ],
)
def test_preflight_and_unexecuted_audits_do_not_count_as_quality_executions(reason):
    assert not rule_execution_counts_as_used("skipped", reason)
    assert not rule_execution_counts_as_completed("skipped", reason)


@pytest.mark.parametrize(
    "status,reason",
    [
        ("evidence_insufficient", None),
        ("error", "AGGREGATE_JUDGE_EXECUTION_ERROR"),
        ("skipped", "supplemental_evidence_disabled"),
        ("manual_review", "semantic_provider_http_500"),
    ],
)
def test_real_quality_attempts_keep_count_even_without_business_conclusion(status, reason):
    assert rule_execution_counts_as_used(status, reason)
    assert not rule_execution_counts_as_completed(status, reason)
    assert not rule_execution_counts_as_used(status, reason, is_bootstrap=True)


def test_aggregate_preflight_source_retains_exact_cause_without_claiming_judge_execution():
    reason = effective_rule_execution_reason(
        "evidence_insufficient",
        None,
        error_code="AGGREGATE_APPLICABILITY_UNPROVEN",
        judge_input_source="aggregate_candidate_disposition",
    )
    assert reason == "AGGREGATE_APPLICABILITY_UNPROVEN"
    assert not rule_execution_counts_as_used("evidence_insufficient", reason)
    assert (
        effective_rule_execution_reason(
            "evidence_insufficient",
            None,
            error_code="AGGREGATE_APPLICABILITY_UNPROVEN",
            judge_input_source="aggregate_rule_judge",
        )
        is None
    )
    assert (
        effective_rule_execution_reason(
            "error",
            None,
            error_code="NEW_PREFLIGHT_CODE",
            judge_input_source="aggregate_candidate_disposition",
        )
        == "aggregate_candidate_disposition"
    )


def test_verified_projected_coverage_gap_is_not_an_actual_judge_attempt():
    assert not rule_execution_counts_as_used(
        "evidence_insufficient", machine_display_status="coverage_gap",
    )
    assert rule_execution_counts_as_used(
        "evidence_insufficient", machine_display_status="evidence_insufficient",
    )


def test_execution_status_keeps_review_and_evidence_states_distinct() -> None:
    assert rule_display_status("pass") == RULE_DISPLAY_PASSED
    assert rule_display_status("fail") == RULE_DISPLAY_FAILED
    assert rule_display_status("warning") == RULE_DISPLAY_FAILED
    assert rule_display_status("skipped") == RULE_DISPLAY_NOT_APPLICABLE
    assert rule_display_status("skipped", is_bootstrap=True) == RULE_DISPLAY_COVERAGE_GAP
    assert rule_display_status("evidence_insufficient") == RULE_DISPLAY_EVIDENCE_INSUFFICIENT
    assert rule_display_status("error") == RULE_DISPLAY_DETECT_ERROR
    assert rule_display_status("manual_review") == RULE_DISPLAY_MANUAL_REVIEW
    assert rule_display_status("skipped", "candidate_not_executed") == RULE_DISPLAY_NOT_EXECUTED
    assert (
        rule_display_status("skipped", "candidate_not_executed", is_bootstrap=True)
        == RULE_DISPLAY_COVERAGE_GAP
    )
    assert rule_display_status("skipped", "exploration_not_started") == RULE_DISPLAY_NOT_EXECUTED
    assert rule_display_status("error", "run_budget_exhausted") == RULE_DISPLAY_NOT_EXECUTED
    assert rule_display_status(None) == RULE_DISPLAY_DETECT_ERROR
    assert rule_display_status("unknown-status") == RULE_DISPLAY_DETECT_ERROR


def test_display_labels_match_prototype_wording() -> None:
    assert rule_display_label(RULE_DISPLAY_PASSED) == "通过"
    assert rule_display_label(RULE_DISPLAY_FAILED) == "未通过"
    assert rule_display_label(RULE_DISPLAY_NOT_DETECTED) == "未触发/不适用"
    assert rule_display_label(RULE_DISPLAY_NOT_APPLICABLE) == "确认不适用"
    assert rule_display_label(RULE_DISPLAY_COVERAGE_GAP) == "运行未覆盖"
    assert rule_display_label(RULE_DISPLAY_DETECT_ERROR) == "执行异常"
    assert rule_display_label(RULE_DISPLAY_EVIDENCE_INSUFFICIENT) == "证据不足"
    assert rule_display_label(RULE_DISPLAY_MANUAL_REVIEW) == "待人工复核"
    assert rule_display_label(RULE_DISPLAY_NOT_EXECUTED) == "未执行"


def test_human_verdict_overrides_machine_display_status() -> None:
    assert rule_final_display_status("manual_review", human_verdict="pass") == "passed"
    assert rule_final_display_status("pass", human_verdict="fail") == "failed"
    assert rule_final_display_status("error", human_verdict="ignored") == "detect_error"
    assert (
        rule_final_display_status("evidence_insufficient", human_verdict="pass")
        == "evidence_insufficient"
    )
    assert (
        rule_final_display_status("skipped", "candidate_not_executed", human_verdict="fail")
        == "not_executed"
    )
    assert rule_final_display_status("manual_review") == "manual_review"


def test_confirmation_scope_requires_an_executed_business_review() -> None:
    # 检测缺口不允许通过人工结论补成业务通过或未通过。
    assert rule_requires_human_confirmation("coverage_gap") is False
    assert rule_requires_human_confirmation("detect_error") is False
    assert rule_requires_human_confirmation("evidence_insufficient") is False
    assert rule_requires_human_confirmation("manual_review") is True
    assert rule_requires_human_confirmation("not_executed") is False
    assert rule_requires_human_confirmation("manual_review", "semantic_provider_http_500") is False
    assert rule_requires_human_confirmation(
        "manual_review", "semantic_provider_disconnect"
    ) is False
    assert rule_requires_human_confirmation(
        "manual_review", result_summary="模型 API 返回 HTTP 500，未生成结论。"
    ) is False
    # 通过 / 未通过 / 本产品不适用直接生效，不再卡导出。
    assert rule_requires_human_confirmation("passed") is False
    assert rule_requires_human_confirmation("failed") is False
    assert rule_requires_human_confirmation("not_detected") is False
    assert rule_requires_human_confirmation("not_applicable") is False
    # 过程态没有结论，不进确认清单。
    assert rule_requires_human_confirmation("running") is False
    assert rule_requires_human_confirmation("pending") is False


def test_humanize_qc_text_translates_popup_wall_termination() -> None:
    assert humanize_qc_text("popup_layer_limit_exceeded:20/20") == (
        "同一页面连续处理 20 层广告弹窗后仍在弹出（上限 20 层），该页探索中止"
    )
    assert humanize_qc_text("popup_dismissal_stalled:3/3") == (
        "同一个弹窗连续 3 次尝试关闭未见效（上限 3 次），该页探索中止"
    )


@pytest.mark.parametrize("reason", ["auth_requirement_not_met", "not_matched_by_run"])
def test_unmatched_skip_placeholders_do_not_become_not_applicable(reason: str) -> None:
    """登录条件未满足和本轮未匹配的收尾占位，没有真实业务不适用结论。"""
    assert rule_display_status("skipped", reason) == "not_executed"
    assert (
        rule_final_display_status("skipped", reason, human_verdict="ignored") == "not_executed"
    )
    assert rule_execution_counts_as_completed("skipped", reason) is False


@pytest.mark.parametrize(
    "code", ["SUPPLEMENTAL_EVIDENCE_DISABLED", "SUPPLEMENTAL_EVIDENCE_NOT_READY"]
)
def test_legacy_supplemental_denial_is_evidence_gap_and_not_completed(code: str) -> None:
    assert rule_display_status("skipped", error_code=code) == "evidence_insufficient"
    assert rule_display_status("skipped", code.lower()) == "evidence_insufficient"
    assert rule_final_display_status(
        "skipped", error_code=code, human_verdict="ignored"
    ) == "evidence_insufficient"
    assert not rule_execution_counts_as_completed("skipped", error_code=code)
    # 无明确补采拒绝事实的既有不适用结论保持原口径。
    assert rule_display_status("skipped", error_code="UNRELATED_CODE") == "not_applicable"
    assert rule_display_status("pass", error_code=code) == "passed"
    assert rule_display_status(
        "skipped", "target_document_unavailable", error_code=code
    ) == "detect_error"


def test_rollup_follows_priority_order() -> None:
    assert rollup_display_status(["passed", "failed", "not_detected"]) == "failed"
    assert rollup_display_status(["passed", "detect_error", "failed"]) == "detect_error"
    assert rollup_display_status(["passed", "failed"]) == "failed"
    assert rollup_display_status(["passed", "not_executed"]) == "not_executed"
    assert rollup_display_status(["passed"]) == "passed"
    assert rollup_display_status([]) is None


@pytest.mark.parametrize("status", ["done", "review", "failed", "canceled"])
def test_stopped_task_progress_is_complete_independently_of_rule_results(status: str) -> None:
    assert whole_task_progress_percent(status) == 100
    assert whole_task_progress_percent(status, loop_guard={"max_duration_seconds": 7200}) == 100


@pytest.mark.parametrize("status", ["running", "canceling"])
@pytest.mark.parametrize("minutes, expected", [(0, 5), (30, 50), (60, 95), (120, 95), (-1, 5)])
def test_active_task_progress_tracks_elapsed_exploration_budget(
    status: str, minutes: int, expected: int,
) -> None:
    started = datetime(2026, 10, 8, tzinfo=UTC)
    assert whole_task_progress_percent(
        status, started_at=started, loop_guard={"max_duration_seconds": 3600},
        now=started + timedelta(minutes=minutes),
    ) == expected


@pytest.mark.parametrize("status", ["running", "canceling"])
@pytest.mark.parametrize("run_status", ["done", "failed", "canceled"])
def test_main_execution_end_waits_for_whole_task_to_stop(status: str, run_status: str) -> None:
    assert whole_task_progress_percent(status, run_status=run_status) == 99


def test_running_at_100_percent_enters_review_only_when_rules_are_idle() -> None:
    assert running_at_full_progress_should_enter_review(
        status="running", rules_settled=True,
    )
    assert not running_at_full_progress_should_enter_review(
        status="running", rules_settled=False,
    )
    assert not running_at_full_progress_should_enter_review(
        status="canceling", rules_settled=True,
    )
    assert not running_at_full_progress_should_enter_review(
        status="review", rules_settled=True,
    )


@pytest.mark.parametrize("status", ["running", "canceling"])
@pytest.mark.parametrize("run_status", ["done", "failed", "canceled", "aborted"])
def test_settled_rules_stop_progress_at_100(status: str, run_status: str) -> None:
    assert whole_task_progress_percent(
        status, run_status=run_status, rules_settled=True,
    ) == 100
    assert whole_task_progress_percent(status, rules_settled=True) == 100


def test_progress_keeps_queued_at_zero_and_handles_legacy_time_budgets() -> None:
    started = datetime(2026, 10, 8)
    now = started + timedelta(minutes=30)
    assert whole_task_progress_percent(
        "queued", started_at=started, run_status="done", now=now,
    ) == 0
    assert whole_task_progress_percent(
        "running", started_at=started, loop_guard={"timeout_minutes": 60}, now=now,
    ) == 50
    assert whole_task_progress_percent(
        "running", started_at=started,
        loop_guard={"max_duration_seconds": 7200, "timeout_minutes": 60}, now=now,
    ) == 27


@pytest.mark.parametrize("guard", [
    None,
    {},
    {"max_duration_seconds": 0},
    {"max_duration_seconds": -1},
    {"max_duration_seconds": True},
    {"max_duration_seconds": "3600"},
    {"max_duration_seconds": float("nan")},
    {"max_duration_seconds": float("inf")},
])
def test_unknown_or_invalid_budget_does_not_guess_rule_progress(guard: dict | None) -> None:
    assert whole_task_progress_percent(
        "running", started_at=datetime(2026, 10, 8, tzinfo=UTC), loop_guard=guard,
    ) == 5
    assert whole_task_progress_percent("running", loop_guard={"max_duration_seconds": 3600}) == 5


@pytest.mark.parametrize(
    ("status", "reason", "bootstrap", "summary", "completed"),
    [
        ("pass", None, False, None, True),
        ("fail", None, False, None, True),
        ("warning", None, False, None, True),
        ("manual_review", "business_uncertain", False, None, True),
        ("skipped", "product_not_applicable", False, None, True),
        ("skipped", None, True, None, False),
        ("skipped", "exploration_not_started", False, None, False),
        ("skipped", "candidate_not_executed", False, None, False),
        ("skipped", "auth_requirement_not_met", False, None, False),
        ("skipped", "not_matched_by_run", False, None, False),
        ("manual_review", "semantic_provider_http_500", False, None, False),
        ("manual_review", None, False, "模型 API 返回 HTTP 500，未生成结论。", False),
        ("evidence_insufficient", None, False, None, False),
        ("error", None, False, None, False),
        ("pending", None, False, None, False),
    ],
)
def test_only_business_results_count_as_rule_completion(
    status, reason, bootstrap, summary, completed
) -> None:
    assert rule_execution_counts_as_completed(
        status, reason, is_bootstrap=bootstrap, result_summary=summary
    ) is completed


def test_severity_maps_to_p_levels() -> None:
    assert severity_display("critical") == "P0"
    assert severity_display("high") == "P1"
    assert severity_display("medium") == "P2"
    assert severity_display("low") == "P3"


def test_humanize_qc_text_translates_missing_contract_evidence_wording() -> None:
    assert humanize_qc_text("缺少规则输入契约要求的可信证据：action_trace") == (
        "缺少这条规则要求的证据：操作记录"
    )
    assert humanize_qc_text("缺少规则输入契约要求的可信证据：动作留痕") == (
        "缺少这条规则要求的证据：操作记录"
    )


def test_humanize_qc_text_explains_candidate_not_judged_placeholder() -> None:
    raw = (
        "规则已进入页面候选清单（page_capture_id=4bdbcc73-1111-2222-3333-444455556666，"
        "page_type=home），但运行结束前 Agent 未完成判定与写回"
    )
    assert humanize_qc_text(raw) == (
        "规则匹配到了这个页面，但任务结束前没轮到它判定"
        "（任务中途卡住或提前收尾，或这条规则要的连续截图、操作前后截图没采到）"
    )


def test_humanize_qc_text_explains_why_rule_is_not_applicable() -> None:
    assert humanize_qc_text("本次运行未匹配该规则，收尾补记为跳过") == (
        "这次没有走到这条规则要查的页面，记为不适用"
    )
    assert humanize_qc_text(
        "本次运行匹配到的页面类型为 「home」、「video_list」，该规则未进入任何页面候选清单"
    ) == "这次只走到了「首页」、「视频列表页」，没有出现这条规则要查的页面"
    assert humanize_qc_text("启动广告仍处于当前旅程末端，等待后续首页采集后再判定") == (
        "这一页还停在启动广告，要进了首页才看得出跳过是否正常，这条规则留到后面的首页再判"
    )


def test_humanize_qc_text_explains_old_ad_landing_insufficient_reason() -> None:
    expected = (
        "没拿到同一次点击广告的完整证据（点击前后的截图、落地页和两者的比对），"
        "没法比对广告和落地页是否一致"
    )
    assert humanize_qc_text("广告源素材与落地页未形成可判定的同动作成对证据：检测工具") == expected
    assert (
        humanize_qc_text(
            "广告源素材与落地页未形成可判定的同动作成对证据：landing_page_probe_tool、"
            "safe_click_observer_tool"
        )
        == expected
    )


def test_humanize_qc_text_translates_new_session_recapture_code() -> None:
    assert humanize_qc_text("H5_SNAPSHOT_NEW_SESSION_RUNTIME_UNAVAILABLE") == (
        "这个端还不能关掉原会话、重开新会话来补采"
    )


def test_humanize_qc_text_hides_supplemental_gate_audit_wording() -> None:
    disabled = (
        "补采能力当前已关闭，操作已被安全拒绝：produce_request"
        "（配置键 ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED）"
    )
    not_ready = "补采能力已配置开启，但运行时闭环尚未就绪，操作已被安全拒绝：produce_request"
    assert humanize_qc_text(disabled) == "本版本不能自动补采，先按证据不足记录"
    assert humanize_qc_text(not_ready) == "本版本不能自动补采，先按证据不足记录"


def test_humanize_qc_text_translates_tool_names_and_terms() -> None:
    text = (
        "safe_click_observer_tool、landing_page_probe_tool、visual_semantic_compare_tool "
        "均返回 insufficient，action_log 为空。"
    )
    assert humanize_qc_text(text) == (
        "安全点击观察、落地页探测、视觉语义比对 均返回 证据不足，操作日志 为空。"
    )


def test_humanize_qc_text_translates_capture_roles_only_in_context() -> None:
    assert humanize_qc_text("当前旅程未形成规则要求的采集角色：source、after") == (
        "本次检查未采集到规则需要的页面：来源页面、操作后页面"
    )
    assert humanize_qc_text("缺少采集角色证据：refreshed") == "缺少页面采集证据：刷新后页面"
    # 语境外的普通英文单词（URL 参数等）不受影响
    assert humanize_qc_text("访问 https://a.com/?source=1 后 utm_source 未变化") == (
        "访问 https://a.com/?source=1 后 utm_source 未变化"
    )


def test_humanize_qc_text_loads_catalog_page_type_names() -> None:
    assert humanize_qc_text("页面类型：「video_list」") == "页面类型：「视频列表页」"


def test_humanize_qc_text_rewrites_jargon_to_plain_language() -> None:
    # 置信度/阈值 → 百分比大白话
    assert humanize_qc_text("判定置信度 0.8600 低于规则阈值 0.9000") == (
        "AI 判断把握度 86%，未达到规则要求的 90%"
    )
    # 拦截标记 → 人机验证说法，标记值保留
    assert humanize_qc_text("页面内容包含可疑拦截标记：robot") == (
        "页面内容疑似遇到人机验证或防爬拦截，检测到标记：robot"
    )
    # 断言/故障注入 → 检查/模拟断网
    assert humanize_qc_text("网络故障注入 与 兜底状态断言 均因缺少故障注入而不充分") == (
        "模拟断网测试 与 兜底状态检查 均因缺少模拟断网而不充分"
    )
    # 帧 → 张截图
    assert humanize_qc_text("多帧采集 状态为 证据不足（仅 1 帧）") == (
        "连续截图采集 状态为 证据不足（仅 1 张截图）"
    )
    assert humanize_qc_text("当前 Observation 为签到活动页稳定态") == (
        "当前 页面观察结果 为签到活动页稳定状态"
    )


def test_humanize_qc_text_translates_state_transition_check_names() -> None:
    assert humanize_qc_text("未满足状态断言：fullscreen_transition") == (
        "操作后页面未达到预期，未通过的检查项：全屏切换"
    )
    assert humanize_qc_text(
        "未满足状态断言：before_text_pattern, after_text_pattern, action_pattern"
    ) == ("操作后页面未达到预期，未通过的检查项：操作前页面文字, 操作后页面文字, 操作动作记录")
    assert humanize_qc_text("检测到多个不同的 Toast 样式签名") == (
        "检测到多个不同的 气泡提示 样式"
    )


def test_humanize_qc_text_translates_recapture_reason_codes() -> None:
    """补采收口写进结果的是内部原因码，任务详情里要显示成人话。"""
    assert humanize_qc_text("H5_TARGET_GRAPH_STATE_UNAVAILABLE") == (
        "要补的那个页面在页面图里没有对应状态"
    )
    assert humanize_qc_text("补采失败：EVIDENCE_REQUEST_EXPIRED") == "补采失败：补采请求已经过期"
    # 没登记的内部码、接口里的普通大写词不动
    assert humanize_qc_text("H5_SOMETHING_NEW_CODE") == "H5_SOMETHING_NEW_CODE"
    assert humanize_qc_text("页面 URL 返回 HTTP 500") == "页面 URL 返回 HTTP 500"


def test_humanize_qc_text_keeps_unknown_identifiers_and_empty_values() -> None:
    assert humanize_qc_text(None) is None
    assert humanize_qc_text("") == ""
    assert humanize_qc_text("unknown_mystery_tool 返回失败") == "unknown_mystery_tool 返回失败"
    assert severity_display(None) is None
    assert severity_display("custom") == "custom"


def test_journey_steps_normalize_with_catalog_aliases() -> None:
    steps = normalize_journey_steps(["首页", "video_detail", "支付", "银河系页面", " "])
    assert [(step.step_name, step.page_type) for step in steps] == [
        ("首页", "home"),
        ("video_detail", "video_detail"),
        ("支付", "payment"),
        ("银河系页面", None),
    ]


def test_trigger_display_fallback_wording() -> None:
    assert trigger_display_fallback([]) == "所有页面默认触发"
    assert "触发" in trigger_display_fallback(["payment"])
    many = trigger_display_fallback(["home", "payment", "settings", "post_publish", "activity"])
    assert "等 5 类页面时触发" in many


def test_rule_package_fallback_by_category() -> None:
    assert rule_package_fallback("ad") == "广告质检"
    assert rule_package_fallback("payment") == "支付与价格质检"
    assert rule_package_fallback(None) == "通用质检"


def test_page_type_display_name() -> None:
    assert page_type_display_name("home") == "首页"
    assert page_type_display_name(None) is None
    assert page_type_display_name("no_such_code") == "no_such_code"
