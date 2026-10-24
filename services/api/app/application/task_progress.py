"""任务详情过程可视化的共享口径：规则/页面展示状态映射与旅程步骤规范化。

新版任务详情页把规则执行态拆成互不混淆的业务结论、证据不足、人工复核和系统异常，
避免把规则未触发或自动化未收敛误写成“页面未识别”。
观察者写入侧（Worker 旁路）与聚合读取侧（工作台 API）都必须引用本模块，避免
两侧口径漂移。调整映射只改这里。
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from services.api.app.application.evidence_recovery_route import (
    REASON_EXPLANATIONS,
    registered_route_reason,
)
from services.api.app.application.report_execution_eligibility import (
    TARGET_DOCUMENT_UNAVAILABLE_REASON,
)
from services.api.app.application.rule_auth_gate import AUTH_REQUIREMENT_NOT_MET_REASON
from services.api.app.domain.page.catalog import PageTypeCatalog
from services.api.app.domain.page.registry import get_default_page_type_registry

RULE_DISPLAY_PASSED = "passed"
RULE_DISPLAY_FAILED = "failed"
RULE_DISPLAY_NOT_DETECTED = "not_detected"
RULE_DISPLAY_NOT_APPLICABLE = "not_applicable"
RULE_DISPLAY_COVERAGE_GAP = "coverage_gap"
RULE_DISPLAY_NOT_EXECUTED = "not_executed"
RULE_DISPLAY_DETECT_ERROR = "detect_error"
RULE_DISPLAY_EVIDENCE_INSUFFICIENT = "evidence_insufficient"
RULE_DISPLAY_MANUAL_REVIEW = "manual_review"
RULE_DISPLAY_RUNNING = "running"
RULE_DISPLAY_PENDING = "pending"

# 候选未执行 / 预算终止等「没跑到」的原因：展示为未执行，报告归入「其他」
RULE_NOT_EXECUTED_REASONS = frozenset(
    {
        "candidate_not_executed",
        "exploration_not_started",
        "run_budget_exhausted",
        "max_steps_reached",
        "run_timeout",
        "continuation_limit_reached",
        "not_matched_by_run",
        AUTH_REQUIREMENT_NOT_MET_REASON,
    }
)

# 旧版把补采被拒的缺证结果保存为 skipped，不能据此推断产品不适用。
RULE_EVIDENCE_GAP_REASONS = frozenset(
    {"supplemental_evidence_disabled", "supplemental_evidence_not_ready"}
)

# 聚合候选在进入判定前即可形成处置留痕；这些记录不代表已调用规则判定。
RULE_PREFLIGHT_DISPOSITION_REASONS = frozenset(
    {
        "aggregate_candidate_disposition",
        "aggregate_rule_scope_not_applicable",
        "aggregate_applicable_evidence_missing",
        "aggregate_applicability_unproven",
        "aggregate_rule_scope_conflict",
        "aggregate_presentation_evidence_rejected",
        "aggregate_journey_evidence_insufficient",
        "aggregate_environment_matrix_insufficient",
    }
)

_EXECUTION_TO_DISPLAY = {
    "pass": RULE_DISPLAY_PASSED,
    "fail": RULE_DISPLAY_FAILED,
    "warning": RULE_DISPLAY_FAILED,
    "skipped": RULE_DISPLAY_NOT_DETECTED,
    "evidence_insufficient": RULE_DISPLAY_EVIDENCE_INSUFFICIENT,
    "error": RULE_DISPLAY_DETECT_ERROR,
    "manual_review": RULE_DISPLAY_MANUAL_REVIEW,
}

RULE_DISPLAY_LABELS = {
    RULE_DISPLAY_PASSED: "通过",
    RULE_DISPLAY_FAILED: "未通过",
    RULE_DISPLAY_NOT_DETECTED: "未触发/不适用",
    RULE_DISPLAY_NOT_APPLICABLE: "确认不适用",
    RULE_DISPLAY_COVERAGE_GAP: "运行未覆盖",
    RULE_DISPLAY_NOT_EXECUTED: "未执行",
    RULE_DISPLAY_DETECT_ERROR: "执行异常",
    RULE_DISPLAY_EVIDENCE_INSUFFICIENT: "证据不足",
    RULE_DISPLAY_MANUAL_REVIEW: "待人工复核",
    RULE_DISPLAY_RUNNING: "执行中",
    RULE_DISPLAY_PENDING: "待执行",
}

# 人工判定只有三个选项（2.1 原型）；ignored 沿用库里的取值，含义是「本产品不适用」
HUMAN_VERDICT_LABELS = {
    "pass": "通过",
    "fail": "未通过",
    "ignored": "本产品不适用",
}

TASK_STATUS_LABELS = {
    "queued": "待执行",
    "running": "执行中",
    "canceling": "取消中",
    "review": "待复核",
    "done": "已完成",
    "failed": "执行失败",
    "canceled": "已取消",
}

_HUMAN_VERDICT_TO_DISPLAY = {
    "pass": RULE_DISPLAY_PASSED,
    "fail": RULE_DISPLAY_FAILED,
    "ignored": RULE_DISPLAY_NOT_APPLICABLE,
}

_ROLLUP_PRIORITY = (
    RULE_DISPLAY_DETECT_ERROR,
    RULE_DISPLAY_FAILED,
    RULE_DISPLAY_MANUAL_REVIEW,
    RULE_DISPLAY_EVIDENCE_INSUFFICIENT,
    RULE_DISPLAY_NOT_EXECUTED,
    RULE_DISPLAY_PASSED,
    RULE_DISPLAY_COVERAGE_GAP,
    RULE_DISPLAY_NOT_APPLICABLE,
    RULE_DISPLAY_NOT_DETECTED,
)

_SEVERITY_TO_DISPLAY = {
    "critical": "P0",
    "high": "P1",
    "medium": "P2",
    "low": "P3",
}

_CATEGORY_TO_PACKAGE = {
    "text": "文案质检",
    "visual": "视觉质检",
    "interaction": "交互质检",
    "ad": "广告质检",
    "payment": "支付与价格质检",
    "input_risk": "输入风险质检",
    "player": "播放器质检",
    "compliance": "合规质检",
}

PAGE_NODE_STATUS_LABELS = {
    "pending": "待执行",
    "visiting": "执行中",
    "recognized": "识别成功",
    "failed": "识别失败",
    "skipped": "已跳过",
}


def effective_rule_execution_reason(
    execution_status: str | None,
    execution_reason: str | None,
    *,
    error_code: str | None = None,
    judge_input_source: str | None = None,
) -> str | None:
    """以正式来源解释前置处置及旧补采拒绝，保留其他机器原因原值。"""
    if judge_input_source == "aggregate_candidate_disposition":
        code = str(error_code or "").strip()
        return code if code.lower() in RULE_PREFLIGHT_DISPOSITION_REASONS else judge_input_source
    if str(execution_reason or "").strip().lower() == TARGET_DOCUMENT_UNAVAILABLE_REASON:
        return execution_reason
    normalized_code = str(error_code or "").strip().lower()
    if execution_status == "skipped" and normalized_code in RULE_EVIDENCE_GAP_REASONS:
        return normalized_code
    return execution_reason


def rule_display_status(
    execution_status: str | None,
    execution_reason: str | None = None,
    *,
    is_bootstrap: bool = False,
    error_code: str | None = None,
) -> str:
    """执行态 → 展示态；未知/缺失一律按执行异常处理，避免读侧漏项。"""
    execution_reason = str(effective_rule_execution_reason(
        execution_status, execution_reason, error_code=error_code
    ) or "").strip().lower() or None
    if execution_reason == TARGET_DOCUMENT_UNAVAILABLE_REASON:
        return RULE_DISPLAY_DETECT_ERROR
    if execution_status == "skipped" and execution_reason in RULE_EVIDENCE_GAP_REASONS:
        return RULE_DISPLAY_EVIDENCE_INSUFFICIENT
    # bootstrap 预置 skipped 表示本轮覆盖缺口，优先于 candidate_not_executed 等运行期原因，
    # 避免读侧把计划外未覆盖误归类为「未执行/预算终止」并虚增待确认项。
    if execution_status == "skipped" and is_bootstrap:
        return RULE_DISPLAY_COVERAGE_GAP
    if execution_reason in RULE_NOT_EXECUTED_REASONS:
        return RULE_DISPLAY_NOT_EXECUTED
    if execution_status == "manual_review" and execution_reason:
        from services.api.app.application.platform_capability import attribute_rule_execution

        attribution = attribute_rule_execution(
            source="judge", execution_status=execution_status,
            diagnostic_codes=[execution_reason],
        )
        if attribution.outcome_class == "platform_degraded":
            return RULE_DISPLAY_DETECT_ERROR
    if execution_status is None:
        return RULE_DISPLAY_DETECT_ERROR
    if execution_status == "skipped":
        return RULE_DISPLAY_NOT_APPLICABLE
    return _EXECUTION_TO_DISPLAY.get(execution_status, RULE_DISPLAY_DETECT_ERROR)


def rule_display_label(display_status: str) -> str:
    return RULE_DISPLAY_LABELS.get(display_status, display_status)


def rule_final_display_status(
    execution_status: str | None,
    execution_reason: str | None = None,
    *,
    human_verdict: str | None = None,
    is_bootstrap: bool = False,
    error_code: str | None = None,
) -> str:
    """人工终审只覆盖已执行的业务结论，不能将检测缺口改写成业务结果。"""
    machine_display = rule_display_status(
        execution_status, execution_reason, is_bootstrap=is_bootstrap, error_code=error_code
    )
    if machine_display not in {
        RULE_DISPLAY_PASSED, RULE_DISPLAY_FAILED, RULE_DISPLAY_NOT_DETECTED,
        RULE_DISPLAY_NOT_APPLICABLE, RULE_DISPLAY_MANUAL_REVIEW,
    }:
        return machine_display
    final_status = _HUMAN_VERDICT_TO_DISPLAY.get(str(human_verdict or "").strip().lower())
    if final_status is not None:
        return final_status
    return rule_display_status(
        execution_status,
        execution_reason,
        is_bootstrap=is_bootstrap,
    )


def human_verdict_label(verdict: str | None) -> str | None:
    if verdict is None:
        return None
    return HUMAN_VERDICT_LABELS.get(verdict, verdict)


def task_status_label(status: str) -> str:
    return TASK_STATUS_LABELS.get(status, status)


TASK_PROGRESS_RUNNING_FLOOR = 5
TASK_PROGRESS_RUNNING_CEIL = 99
TASK_PROGRESS_EXPLORING_CEIL = 95


def rule_execution_counts_as_used(
    execution_status: str | None,
    execution_reason: str | None = None,
    *,
    is_bootstrap: bool = False,
    machine_display_status: str | None = None,
) -> bool:
    """实际判定计次，未执行占位和判定前处置不计；真实判定缺证仍计入次数。"""
    if is_bootstrap or execution_status is None or machine_display_status in {
        RULE_DISPLAY_NOT_EXECUTED, RULE_DISPLAY_COVERAGE_GAP,
    }:
        return False
    reason = str(execution_reason or "").strip().lower()
    if reason in RULE_PREFLIGHT_DISPOSITION_REASONS:
        return False
    return rule_display_status(execution_status, execution_reason) not in {
        RULE_DISPLAY_NOT_EXECUTED,
        RULE_DISPLAY_COVERAGE_GAP,
    }


def rule_execution_counts_as_completed(
    execution_status: str | None,
    execution_reason: str | None = None,
    *,
    is_bootstrap: bool = False,
    result_summary: str | None = None,
    error_code: str | None = None,
) -> bool:
    """只有真实业务判定和确认不适用计入完成，占位、未执行、缺证及故障不计入。"""
    if not rule_execution_counts_as_used(
        execution_status,
        execution_reason,
        is_bootstrap=is_bootstrap,
    ):
        return False
    display = rule_display_status(execution_status, execution_reason, error_code=error_code)
    if display in {RULE_DISPLAY_PASSED, RULE_DISPLAY_FAILED, RULE_DISPLAY_NOT_APPLICABLE}:
        return True
    return rule_requires_human_confirmation(
        display, execution_reason, result_summary=result_summary
    )


def whole_task_progress_percent(
    status: str,
    *,
    started_at: datetime | None = None,
    loop_guard: Mapping[str, Any] | None = None,
    run_status: str | None = None,
    rules_settled: bool = False,
    now: datetime | None = None,
) -> int:
    """任务进度表示自动探索与执行过程，不表示规则业务完成率。

    运行中按冻结时长预算估算 5% 至 95%。主执行结束后，规则尚未补完显示 99%。
    强制规则工作项全部终态后执行时长停止，进度记 100%；缺证、未执行等结果仍独立保留。
    """
    if status == "queued":
        return 0
    if status in {"done", "review", "failed", "canceled"}:
        return 100
    if status not in {"running", "canceling"}:
        return 0
    if rules_settled:
        # 强制规则工作项已全部终态。清单缺口和收口闸门不再把进度停在 99%。
        return 100
    if run_status in {"done", "failed", "canceled", "aborted"}:
        return TASK_PROGRESS_RUNNING_CEIL
    duration = _task_progress_duration_seconds(loop_guard or {})
    if not isinstance(started_at, datetime) or duration is None:
        return TASK_PROGRESS_RUNNING_FLOOR
    started = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    current = current if current.tzinfo else current.replace(tzinfo=UTC)
    fraction = max(0.0, min(1.0, (current - started).total_seconds() / duration))
    return TASK_PROGRESS_RUNNING_FLOOR + round(
        (TASK_PROGRESS_EXPLORING_CEIL - TASK_PROGRESS_RUNNING_FLOOR) * fraction
    )


def running_at_full_progress_should_enter_review(
    *,
    status: str,
    rules_settled: bool,
) -> bool:
    """执行中且进度已是 100% 时进入待复核。

    100% 只在强制规则工作项全部终态、主执行已结束且没有仍在更新的规则尝试时出现。
    取消中的任务不因这个进度改状态。
    """

    return status == "running" and rules_settled


def _task_progress_duration_seconds(loop_guard: Mapping[str, Any]) -> float | None:
    """沿用执行护栏的时长字段优先级，未知旧预算不推测成页面或规则总量。"""
    value = loop_guard.get("max_duration_seconds")
    multiplier = 1
    if value is None:
        value = loop_guard.get("timeout_minutes")
        multiplier = 60
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        duration = float(value) * multiplier
    except OverflowError:
        return None
    return duration if isfinite(duration) and duration > 0 else None


def rule_requires_human_confirmation(
    machine_display_status: str,
    execution_reason: str | None = None,
    *,
    result_summary: str | None = None,
) -> bool:
    """仅已执行且业务结论待复核的规则允许人工确认，缺口不能靠人工结论补成完成。

    未执行、缺证、检测故障保留原始状态；平台技术故障产生的转人工也不进入业务确认。
    SQL 侧同源片段见 infrastructure/db/rule_confirmation_sql.py。
    """
    from services.api.app.application.platform_capability import attribute_rule_execution

    if machine_display_status != RULE_DISPLAY_MANUAL_REVIEW:
        return False
    if str(execution_reason or "").strip().lower() in RULE_NOT_EXECUTED_REASONS:
        return False
    attribution = attribute_rule_execution(
        source="judge",
        execution_status="manual_review",
        diagnostic_codes=[execution_reason] if execution_reason else [],
        result_summary=result_summary,
    )
    return attribution.outcome_class == "human_review_required"


def rollup_display_status(display_statuses: list[str]) -> str | None:
    """按异常、真实失败、待复核、证据不足、未执行、通过、不适用汇总页面结果。"""
    present = set(display_statuses)
    for status in _ROLLUP_PRIORITY:
        if status in present:
            return status
    return None


def severity_display(severity: str | None) -> str | None:
    if severity is None:
        return None
    return _SEVERITY_TO_DISPLAY.get(severity, severity)


def rule_package_fallback(category: str | None) -> str:
    return _CATEGORY_TO_PACKAGE.get(category or "", "通用质检")


def trigger_display_fallback(
    applicable_page_types: list[str],
    *,
    catalog: PageTypeCatalog | None = None,
) -> str:
    """按适用页面范围生成触发条件展示文案的兜底值。

    引擎的真实触发语义就是"识别出的 page_type 命中规则适用范围"，因此兜底文案
    忠实描述该语义；产品后续可在 ``qc_rule.trigger_display`` 覆写为更细的说法。
    """
    resolved = catalog or get_default_page_type_registry().catalog
    by_code = {item.code: item for item in resolved.page_types if item.status == "supported"}
    names: list[str] = []
    for code in applicable_page_types:
        definition = by_code.get(code)
        names.append(definition.name if definition is not None else code)
    if not names or len(names) >= len(by_code):
        return "所有页面默认触发"
    if len(names) <= 3:
        return f"识别到{'、'.join(names)}时触发"
    return f"识别到{'、'.join(names[:3])}等 {len(names)} 类页面时触发"


def page_type_display_name(
    page_type: str | None,
    *,
    catalog: PageTypeCatalog | None = None,
) -> str | None:
    """page_type code → 目录展示名；未知 code 原样返回。"""
    if not page_type:
        return None
    resolved = catalog or get_default_page_type_registry().catalog
    for item in resolved.page_types:
        if item.code == page_type:
            return item.name
    return page_type


# ---------- 执行文案人话化 ----------
# 执行留痕（判定摘要/问题说明/复核原因等）由 Agent 与判定引擎写入，夹带工具名、
# 采集角色等英文标识。展示层统一翻译，不改库存数据，历史任务同样生效。

_QC_TERM_LABELS = {
    # 平台注册 Tool
    "page_capture_tool": "页面采集",
    "page_detection_tool": "页面识别",
    "candidate_rules_tool": "候选规则查询",
    "record_rule_execution_tool": "规则结果记录",
    "record_rule_executions_batch_tool": "批量规则结果记录",
    "rule_judge_tool": "规则判定",
    "rule_judge_batch_tool": "批量规则判定",
    "semantic_rule_judge_tool": "语义规则判定",
    "deterministic_assertion_tool": "确定性检查",
    "sensitive_word_detection_tool": "敏感词检测",
    "qr_link_validation_tool": "二维码链接校验",
    "exploration_closure_tool": "探索收尾",
    "report_generation_tool": "报告生成",
    "report_publish_tool": "报告发布",
    # 规则能力规划中的采集/判定能力名（qc_rule.execution_route）
    "text_style_extractor_tool": "文字样式提取",
    "typography_assertion_handler": "排版检查",
    "media_element_extractor_tool": "媒体元素提取",
    "multi_frame_capture_tool": "连续截图采集",
    "image_quality_analyzer_tool": "图像质量分析",
    "visual_rule_judge_tool": "视觉规则判定",
    "visible_text_extractor_tool": "可见文本提取",
    "layout_region_extractor_tool": "版面区域提取",
    "network_console_summary_tool": "网络与控制台摘要",
    "blank_module_detector_tool": "空白模块检测",
    "external_navigation_handler": "外跳导航检查",
    "link_inventory_tool": "链接清点",
    "safe_click_observer_tool": "安全点击观察",
    "state_snapshot_tool": "状态快照",
    "network_event_summary_tool": "网络事件摘要",
    "interaction_state_diff_tool": "交互状态对比",
    "data_refresh_handler": "数据刷新检查",
    "transient_message_capture_tool": "瞬时提示捕获",
    "punctuation_assertion_handler": "标点检查",
    "product_price_extractor_tool": "商品价格提取",
    "price_normalization_tool": "价格归一化",
    "ad_region_detector_tool": "广告区域检测",
    "image_geometry_tool": "图像几何检测",
    "landing_page_probe_tool": "落地页探测",
    "visual_semantic_compare_tool": "视觉语义比对",
    "countdown_accuracy_handler": "倒计时精度检查",
    "monotonic_timer_tool": "单调计时",
    "clickable_area_handler": "可点区域检查",
    "element_geometry_tool": "元素几何检测",
    "safe_hit_test_tool": "安全命中测试",
    "popup_close_behavior_handler": "弹窗关闭行为检查",
    "ad_modal_detector_tool": "广告弹窗检测",
    "overlay_dismissal_handler": "浮层关闭检查",
    "popup_sequence_handler": "弹窗序列检查",
    "network_fault_injection_tool": "模拟断网测试",
    "fallback_state_assertion_tool": "兜底状态检查",
    "ad_control_inventory_tool": "广告控件清点",
    "control_behavior_assertion_tool": "控件行为检查",
    "ad_first_frame_latency_handler": "广告首帧时延检查",
    "performance_timeline_tool": "性能时间线",
    "ad_failure_media_fallback_handler": "广告失败兜底检查",
    "player_state_probe_tool": "播放器状态探测",
    "ugc_link_extractor_tool": "用户内容链接提取",
    "safe_url_reputation_tool": "链接安全信誉检查",
    "ugc_content_extractor_tool": "用户内容提取",
    "content_safety_judge_tool": "内容安全判定",
    "spam_frequency_handler": "灌水频率检查",
    "ugc_sequence_extractor_tool": "用户内容时序提取",
    "rate_limit_observer_tool": "频控观察",
    "comment_state_sync_handler": "评论状态同步检查",
    "label_case_consistency_handler": "标签大小写一致性检查",
    "label_group_extractor_tool": "标签组提取",
    "offline_content_visibility_handler": "下架内容可见性检查",
    "content_item_state_extractor_tool": "内容条目状态提取",
    "qr_url_extractor_tool": "二维码链接提取",
    "activity_state_extractor_tool": "活动状态提取",
    "cross_page_state_assertion_tool": "跨页状态检查",
    "playback_resume_handler": "断点续播检查",
    "session_journey_runner_tool": "页面流程执行",
    "upload_rule_visibility_handler": "上传规则可见性检查",
    "upload_contract_extractor_tool": "上传约束提取",
    "safe_file_dialog_probe_tool": "文件选择探测",
    "media_fullscreen_handler": "全屏播放检查",
    "logout_entry_presence_handler": "退出登录入口检查",
    "account_control_extractor_tool": "账号控件提取",
    # 状态迁移检查项名（rule_transition_evaluators 的 checks[].name）
    "before_text_pattern": "操作前页面文字",
    "after_text_pattern": "操作后页面文字",
    "after_absent_pattern": "操作后应消失的内容",
    "refreshed_text_pattern": "刷新后页面文字",
    "action_pattern": "操作动作记录",
    "visible_text_changed": "页面文字有变化",
    "numeric_change": "数字变化",
    "fullscreen_transition": "全屏切换",
    # 高频状态词与证据类型
    "insufficient": "证据不足",
    "action_log": "操作日志",
    "ui_snapshot": "界面快照",
    "visible_text": "可见文本",
    "temporal_trace": "时序留痕",
    "action_trace": "操作记录",
    "geometry_anomalies": "几何异常",
    "evidence_insufficient": "证据不足",
    "manual_review": "转人工复核",
    "pass": "通过",
    "fail": "未通过",
    "skipped": "跳过",
    "ugc": "用户发布内容",
    "dom": "页面结构",
    "toast": "气泡提示",
    "banner": "横幅",
    "ascii": "半角字符",
    "fullwidth_ascii": "全角字符",
    "serif_elements": "衬线字体元素",
    "interval_ms": "毫秒间隔",
    "check_point": "检查点",
    "page_capture_id": "页面采集编号",
    "page_type": "页面类型",
    "requires_journey": "需要多页流程",
    "journey": "页面流程",
    "temporal": "时序",
    "geometry": "几何",
    "stable": "稳定",
    "complete": "加载完整",
    "natural": "原始尺寸",
    "multi_frame": "连续截图",
    "agent": "质检程序",
    "typography": "排版",
    "fallback": "兜底",
    "countdown": "倒计时",
    "probe": "探测",
    "landing": "落地页",
    "packet": "数据包",
    "object_fit": "图片显示方式",
}

# Agent 行文常省略 _tool/_handler 后缀（如 blank_module_detector），为词条补无后缀别名。
_QC_TERM_LABELS.update(
    {
        stem: label
        for key, label in list(_QC_TERM_LABELS.items())
        if (key.endswith("_tool") or key.endswith("_handler"))
        and (stem := key.removesuffix("_tool").removesuffix("_handler"))
        not in _QC_TERM_LABELS
    }
)


def _load_page_type_names() -> dict[str, str]:
    try:
        catalog = get_default_page_type_registry().catalog
    except Exception:  # noqa: BLE001 - 目录加载失败不影响其余翻译
        return {}
    return {item.code: item.name for item in catalog.page_types}


_PAGE_TYPE_NAMES = _load_page_type_names()

# 页面类型 code 全局替换只收带下划线的（video_list 等）；home/movie 这类普通单词
# 可能撞 URL 路径或产品文案，仅在「code」引号语境内翻译（见 humanize_qc_text）。
_QC_TERM_LABELS.update(
    {
        code: name
        for code, name in _PAGE_TYPE_NAMES.items()
        if "_" in code and code not in _QC_TERM_LABELS
    }
)

_QUOTED_PAGE_TYPE = re.compile(r"「([a-z][a-z0-9_]*)」")

_CAPTURE_ROLE_LABELS = {
    "primary": "当前页面",
    "before": "操作前页面",
    "after": "操作后页面",
    "refreshed": "刷新后页面",
    "fault": "模拟断网后页面",
    "source": "来源页面",
}

# 规则配置里的动态采集角色（防滥用探针次数、价格位置等）。
_ROLE_TOKEN_PATTERNS = (
    (re.compile(r"^attempt_(\d+)$"), "第{}次尝试"),
    (re.compile(r"^location_(\d+)$"), "价格位置{}"),
)


def _capture_role_label(token: str) -> str:
    label = _CAPTURE_ROLE_LABELS.get(token)
    if label:
        return label
    for pattern, template in _ROLE_TOKEN_PATTERNS:
        match = pattern.match(token)
        if match:
            return template.format(match.group(1))
    return token


# 中文与英文标识相邻时没有 \b 边界（中文也算 \w），改用 ASCII 词符环视。
_QC_TERM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(sorted((re.escape(term) for term in _QC_TERM_LABELS), key=len, reverse=True))
    + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# 角色名是 source/after 这类普通单词，只在「采集角色：xxx」语境内翻译，避免误伤 URL 等。
_CAPTURE_ROLE_SEGMENT = re.compile(
    r"(采集角色[^：:，。；\n]{0,16}[：:]\s*)([a-z][a-z0-9_]*(?:[、,，/\s]+[a-z][a-z0-9_]*)*)"
)


# 面向非技术用户的整句大白话改写：在词表替换之后执行；顺序敏感，长语境在前。
_CONFIDENCE_BELOW_THRESHOLD = re.compile(
    r"判定置信度\s*([01](?:\.\d+)?)\s*低于规则阈值\s*([01](?:\.\d+)?)"
)

_PLAIN_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 补采门禁的审计语（操作名、配置键是给运维看的）：验收的人只需要知道本版本不能自动补采
    (
        re.compile(
            r"补采能力(?:当前已关闭|已配置开启，但运行时闭环尚未就绪)，操作已被安全拒绝："
            r"[^\s（）]+(?:（[^）]*）)?"
        ),
        "本版本不能自动补采，先按证据不足记录",
    ),
    (re.compile(r"缺少规则输入契约要求的可信证据："), "缺少这条规则要求的证据："),
    # 收口占位：规则匹配到了页面但任务结束前没送判。原句「质检程序未完成判定与写回」
    # 读着像程序出错；实际是任务中途卡住 / 提前收尾，或规则要的多张截图没采到。
    (
        re.compile(
            r"规则已进入页面候选清单（[^）]*），但运行结束前\s*(?:质检程序|Agent)\s*未完成判定与写回"
        ),
        "规则匹配到了这个页面，但任务结束前没轮到它判定"
        "（任务中途卡住或提前收尾，或这条规则要的连续截图、操作前后截图没采到）",
    ),
    (re.compile(r"动作留痕"), "操作记录"),
    # 09-22 前写库的广告落地页证据不足：原句后面拼的工具名写回时被收成「检测工具」，
    # 已经看不出缺了哪份，改成说清楚是哪一类证据没拿到
    (
        re.compile(r"广告源素材与落地页未形成可判定的同动作成对证据(?:：[^。；\n]*)?"),
        "没拿到同一次点击广告的完整证据（点击前后的截图、落地页和两者的比对），"
        "没法比对广告和落地页是否一致",
    ),
    # 不适用的原因要让运营看得懂（2026-09-22 立川：不适用也要写明为什么不适用）
    (
        re.compile(r"本次运行未匹配该规则，收尾补记为跳过"),
        "这次没有走到这条规则要查的页面，记为不适用",
    ),
    (
        re.compile(r"本次运行匹配到的页面类型为\s*((?:「[^」]+」、?)+)，该规则未进入任何页面候选清单"),
        r"这次只走到了\1，没有出现这条规则要查的页面",
    ),
    (
        re.compile(r"启动广告仍处于当前旅程末端，等待后续首页采集后再判定"),
        "这一页还停在启动广告，要进了首页才看得出跳过是否正常，这条规则留到后面的首页再判",
    ),
    (re.compile(r"当前旅程未形成规则要求的采集角色："), "本次检查未采集到规则需要的页面："),
    (re.compile(r"缺少采集角色证据："), "缺少页面采集证据："),
    (re.compile(r"当前旅程"), "本次检查"),
    (re.compile(r"采集角色"), "采集页面"),
    (re.compile(r"旅程"), "检查流程"),
    (re.compile(r"包含可疑拦截标记："), "疑似遇到人机验证或防爬拦截，检测到标记："),
    (re.compile(r"(?<![A-Za-z0-9_])observation(?![A-Za-z0-9_])", re.IGNORECASE), "页面观察结果"),
    (re.compile(r"确定性工具"), "自动化检测工具"),
    (re.compile(r"稳定态"), "稳定状态"),
    (re.compile(r"UI\s*快照", re.IGNORECASE), "界面快照"),
    (re.compile(r"网络故障注入"), "模拟断网测试"),
    (re.compile(r"故障注入"), "模拟断网"),
    (
        re.compile(r"popup_layer_limit_exceeded:(\d+)/(\d+)"),
        r"同一页面连续处理 \1 层广告弹窗后仍在弹出（上限 \2 层），该页探索中止",
    ),
    (
        re.compile(r"popup_dismissal_stalled:(\d+)/(\d+)"),
        r"同一个弹窗连续 \1 次尝试关闭未见效（上限 \2 次），该页探索中止",
    ),
    (re.compile(r"未满足状态断言："), "操作后页面未达到预期，未通过的检查项："),
    (re.compile(r"样式签名"), "样式"),
    (re.compile(r"断言"), "检查"),
    (re.compile(r"规则阈值"), "规则要求"),
    (re.compile(r"置信度"), "AI 判断把握度"),
    (re.compile(r"退出登录控件"), "退出登录按钮"),
    (re.compile(r"多帧采集"), "连续截图采集"),
    (re.compile(r"(仅\s*)(\d+)\s*帧"), r"\g<1>\g<2> 张截图"),
    (re.compile(r"单帧"), "单张截图"),
    (re.compile(r"多帧"), "连续多张"),
    (re.compile(r"object-fit\s*[:：]\s*cover", re.IGNORECASE), "图片等比裁剪显示"),
    (re.compile(r"object-fit\s*[:：]\s*contain", re.IGNORECASE), "图片等比完整显示"),
    (re.compile(r"object-fit\s*[:：]\s*fill", re.IGNORECASE), "图片拉伸填充"),
    (re.compile(r"object-fit", re.IGNORECASE), "图片显示方式"),
    # 内部编号（UUID/长十六进制）截短，保留前 8 位可追查
    (
        re.compile(r"([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
        r"\1…",
    ),
    (re.compile(r"(?<![A-Za-z0-9_])([0-9a-f]{8})[0-9a-f]{4,}(?![A-Za-z0-9_])"), r"\1…"),
)


# 补采原因码（如 H5_TARGET_GRAPH_STATE_UNAVAILABLE）：登记过的换成那句人话，没登记的原样保留
_REASON_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,}(?![A-Za-z0-9_])"
)


def _route_reason_label(match: re.Match[str]) -> str:
    registered = registered_route_reason(match.group(0))
    return REASON_EXPLANATIONS[registered][0] if registered else match.group(0)


def _percent(value: str) -> str:
    return f"{float(value) * 100:g}%"


def humanize_qc_text(text: str | None) -> str | None:
    """执行文案翻成大白话：术语词表替换 + 整句改写；词表外的标识原样保留。"""
    if not text:
        return text

    def _translate_roles(match: re.Match[str]) -> str:
        tokens = re.split(r"([、,，/\s]+)", match.group(2))
        translated = [_capture_role_label(token) for token in tokens]
        return match.group(1) + "".join(translated)

    result = _REASON_CODE_PATTERN.sub(_route_reason_label, text)
    result = _CAPTURE_ROLE_SEGMENT.sub(_translate_roles, result)
    result = _QC_TERM_PATTERN.sub(lambda match: _QC_TERM_LABELS[match.group(0).lower()], result)
    result = _CONFIDENCE_BELOW_THRESHOLD.sub(
        lambda match: (
            f"AI 判断把握度 {_percent(match.group(1))}，"
            f"未达到规则要求的 {_percent(match.group(2))}"
        ),
        result,
    )
    for pattern, replacement in _PLAIN_REWRITES:
        result = pattern.sub(replacement, result)
    return _QUOTED_PAGE_TYPE.sub(
        lambda match: f"「{_PAGE_TYPE_NAMES.get(match.group(1), match.group(1))}」",
        result,
    )


@dataclass(frozen=True)
class JourneyStep:
    """规范化后的旅程步骤：原始名称 + 目录 page_type（无法映射时为 None）。"""

    step_name: str
    page_type: str | None


def normalize_journey_steps(
    steps: list[str],
    *,
    catalog: PageTypeCatalog | None = None,
) -> list[JourneyStep]:
    """把创建任务时的自由文本旅程步骤映射为页面目录 code。

    优先精确命中 code，其次命中目录 name/aliases；映射失败保留原始名称并置
    page_type=None（时间线仍展示该节点，但无法关联候选规则数）。
    """
    resolved = catalog or get_default_page_type_registry().catalog
    normalized: list[JourneyStep] = []
    for raw in steps:
        step = str(raw).strip()
        if not step:
            continue
        page_type: str | None = None
        try:
            page_type = resolved.require_code(step).code
        except Exception:  # noqa: BLE001 - 目录抛 InvalidPageTypeCodeError，别名再试一次
            try:
                page_type = resolved.require(step).code
            except Exception:  # noqa: BLE001 - 映射失败按未知页面处理
                page_type = None
        normalized.append(JourneyStep(step_name=step, page_type=page_type))
    return normalized
