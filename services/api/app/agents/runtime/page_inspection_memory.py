"""页面质检记忆模型：维护单次 Run 的页面采集、候选规则和页面级结果。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from services.api.app.domain.exploration.page_type_assessment import (
    PageTypeAssessment,
    PageTypeAssessmentStatus,
    build_page_type_assessment,
    build_page_type_assessment_set,
)
from services.api.app.domain.page.registry import get_default_page_type_registry


class PageInspectionStatus(StrEnum):
    """页面质检执行状态；终态不要求所有规则都成功。"""

    CAPTURED = "captured"
    DETECTING = "detecting"
    MATCHING = "matching"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"


TERMINAL_PAGE_INSPECTION_STATUSES = {
    PageInspectionStatus.COMPLETED.value,
    PageInspectionStatus.DEGRADED.value,
    PageInspectionStatus.FAILED.value,
}
REVIEW_RULE_EXECUTION_STATUSES = {"manual_review", "evidence_insufficient", "error"}
TRANSIENT_PAGE_TYPES = frozenset(get_default_page_type_registry().transient_codes)
TRANSIENT_PAGE_STATES = frozenset(
    {"loading", "line_loading", "transitioning", "needs_more_observation"}
)
TRANSIENT_RECOVERY_KINDS = TRANSIENT_PAGE_TYPES | TRANSIENT_PAGE_STATES
DEFAULT_MAX_TRANSIENT_RECOVERY_ATTEMPTS = 3
DEFAULT_MAX_OBSTRUCTION_RECOVERY_ATTEMPTS = 3
DEFAULT_MAX_PAGE_OBSTRUCTION_RECOVERY_ATTEMPTS = 20
# 生产性清障表示每次点击后都出现新的后继采集，和浏览器单次清障链
# 12 层上限保持一致；无变化或失败点击仍走更短预算，避免无限重复点击。
DEFAULT_MAX_PRODUCTIVE_OBSTRUCTION_RECOVERY_ATTEMPTS = 12
_OBSTRUCTION_PAGE_STATES = frozenset({"popup", "modal", "overlay", "dialog"})
DEFAULT_MAX_SOURCE_RESTORE_ATTEMPTS = 2
_RESTORE_SOURCE_ACTION_PREFIX = "restore_source_action:"
_RESTORE_SOURCE_TRANSIT_PREFIX = "restore_source_transit:"
_RESTORE_SOURCE_READY_KEYWORD = "restore_source_ready"
_UNSTABLE_RESTORE_PAGE_STATES = (
    TRANSIENT_PAGE_STATES
    | _OBSTRUCTION_PAGE_STATES
    | {
        "countdown",
        "login_required",
        "popup",
    }
)
_STABLE_SOURCE_PAGE_STATES = frozenset(
    {"stable", "normal", "loaded", "feed", "notice", "detail", "playing"}
)
_CAPACITY_TERMINATION_CODES = {
    "MAX_STEPS_REACHED",
    "STUCK_DETECTED",
    "CONTINUATION_LIMIT_REACHED",
    "RUN_TIMEOUT",
}
_SUPPLEMENTAL_EVIDENCE_DISABLED_CODE = "SUPPLEMENTAL_EVIDENCE_DISABLED"
_DISABLED_SUPPLEMENTAL_EVIDENCE_PURPOSES = frozenset(
    {
        "page_coverage_continue",
        "temporal_evidence_capture",
        "post_interaction_page_observation",
    }
)
_DISABLED_SUPPLEMENTAL_EVIDENCE_PURPOSE_PREFIXES = (
    "rule_evidence:",
    "transient_page_recovery:",
    "inspectable_page_transition:",
)
_BOTTOM_NAVIGATION_PAGE_TYPE_PRIORITY = {
    "profile": 0,
    "payment": 1,
    "settings": 2,
    "postpublish": 3,
    "postlist": 4,
    "videolist": 5,
    "contentlist": 6,
    "activity": 7,
    "home": 8,
}


_COVERAGE_GAP_PAGE_TYPE_PRIORITY = {
    "settings": 0,
    "payment": 1,
    "postpublish": 2,
}

_CURRENT_BROWSER_INACTIVE_REASONS = frozenset(
    {
        "obstruction_result_reuses_source_inspection",
        "page_detection_ambiguous",
        "page_type_not_rule_matchable",
        "source_restore_transit",
    }
)


@dataclass(frozen=True)
class PageDetectionMemory:
    """一次页面识别对应的规则匹配门禁结果。"""

    detection_status: str
    page_type: str | None
    page_state: str | None
    rule_matching_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定的 JSON 结构。"""
        payload = {
            "detection_status": self.detection_status,
            "page_type": self.page_type,
            "rule_matching_allowed": self.rule_matching_allowed,
        }
        if self.page_state is not None:
            payload["page_state"] = self.page_state
        return payload


@dataclass
class PageCaptureMemory:
    """当前 Run 内一次页面采集的最小可审计索引。"""

    page_capture_id: str
    page_url: str | None = None
    page_title: str | None = None
    page_seq: int | None = None
    artifact_ids: list[str] = field(default_factory=list)
    content_fingerprint: str | None = None
    visible_text_fingerprint: str | None = None
    obstruction_dismissal_count: int = 0
    page_detection: PageDetectionMemory | None = None
    exploration_action_id: str | None = None
    supersedes_page_capture_id: str | None = None
    lifecycle_status: str = "active"
    superseded_by_page_capture_id: str | None = None
    abandon_reason: str | None = None
    inspection_status: str = PageInspectionStatus.CAPTURED.value
    failure_stage: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    failure_retryable: bool | None = None

    @property
    def inspection_terminal(self) -> bool:
        """判断页面质检是否已经进入执行终态。"""
        return self.inspection_status in TERMINAL_PAGE_INSPECTION_STATUSES

    def failure_dict(self) -> dict[str, Any] | None:
        """返回稳定的失败摘要；无失败时返回 None。"""
        if not any((self.failure_stage, self.failure_code, self.failure_message)):
            return None
        return {
            "stage": self.failure_stage,
            "code": self.failure_code,
            "message": self.failure_message,
            "retryable": self.failure_retryable,
        }

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定的 JSON 结构。"""
        return {
            "page_capture_id": self.page_capture_id,
            "page_url": self.page_url,
            "page_title": self.page_title,
            "page_seq": self.page_seq,
            "artifact_ids": list(self.artifact_ids),
            "content_fingerprint": self.content_fingerprint,
            "visible_text_fingerprint": self.visible_text_fingerprint,
            "page_detection": (
                self.page_detection.to_dict() if self.page_detection is not None else None
            ),
            "exploration_action_id": self.exploration_action_id,
            "supersedes_page_capture_id": self.supersedes_page_capture_id,
            "lifecycle_status": self.lifecycle_status,
            "superseded_by_page_capture_id": self.superseded_by_page_capture_id,
            "abandon_reason": self.abandon_reason,
            "inspection_status": self.inspection_status,
            "failure": self.failure_dict(),
        }


@dataclass
class ExplorationActionMemory:
    """单次 Run 内待页面探索的候选动作。"""

    exploration_action_id: str
    source_page_capture_id: str | None
    source_page_type: str | None
    action: str
    origin: str = "discovered"
    target: str | None = None
    target_keywords: list[str] = field(default_factory=list)
    risk_level: str | None = None
    purpose: str | None = None
    expected_next_page_type: str | None = None
    navigation_depth: int | None = None
    status: str = "candidate"
    result_page_capture_id: str | None = None
    failure_reason: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    failure_retryable: bool | None = None
    attempt_count: int = 0
    mandatory: bool = False
    blocks_page_inspection: bool = False
    supersedes_source_page: bool = False
    requires_source_inspection_complete: bool = False
    evidence_source_roles: list[str] = field(default_factory=list)
    evidence_result_roles: list[str] = field(default_factory=list)
    capture_role: str | None = None
    side_evidence_only: bool = False
    resumes_page_exploration: bool = False

    def to_dict(self) -> dict[str, Any]:
        """返回可序列化的动作快照。"""
        return {
            "exploration_action_id": self.exploration_action_id,
            "source_page_capture_id": self.source_page_capture_id,
            "source_page_type": self.source_page_type,
            "action": self.action,
            "origin": self.origin,
            "target": self.target,
            "target_keywords": list(self.target_keywords),
            "risk_level": self.risk_level,
            "purpose": self.purpose,
            "expected_next_page_type": self.expected_next_page_type,
            "navigation_depth": self.navigation_depth,
            "status": self.status,
            "result_page_capture_id": self.result_page_capture_id,
            "failure_reason": self.failure_reason,
            "failure_code": self.failure_code,
            "failure_message": self.failure_message,
            "failure_retryable": self.failure_retryable,
            "attempt_count": self.attempt_count,
            "mandatory": self.mandatory,
            "blocks_page_inspection": self.blocks_page_inspection,
            "supersedes_source_page": self.supersedes_source_page,
            "requires_source_inspection_complete": self.requires_source_inspection_complete,
            "evidence_source_roles": list(self.evidence_source_roles),
            "evidence_result_roles": list(self.evidence_result_roles),
            "capture_role": self.capture_role,
            "side_evidence_only": self.side_evidence_only,
            "resumes_page_exploration": self.resumes_page_exploration,
        }

    def dedupe_key(
        self,
    ) -> tuple[str | None, str, str | None, tuple[str, ...] | str | None]:
        """生成探索动作语义去重键。"""
        normalized_keywords = tuple(
            sorted(
                {
                    keyword
                    for item in self.target_keywords
                    if (keyword := _normalized_semantic_text(item)) is not None
                }
            )
        )
        if _is_semantic_nested_content_action(
            action=self.action,
            purpose=self.purpose,
            expected_next_page_type=self.expected_next_page_type,
            target_keywords=normalized_keywords,
        ):
            # 模型生成的详情入口常在不同采集里改变描述关键词，不能让这些噪声进入
            # 去重键；稳定分支标识和目标足以识别“同一列表再次点击同一内容”。
            branch_keywords = tuple(
                keyword
                for keyword in normalized_keywords
                if keyword.startswith(("bottomtab", "pagetype", "path"))
            )
            return (
                None,
                self.action.lower(),
                None,
                "nested_content:"
                + "|".join(branch_keywords)
                + f":{_normalized_semantic_text(self.target) or ''}",
            )
        if _is_internal_navigation_action(target_keywords=normalized_keywords):
            # 由真实同源 href 推导的页面分支按 path 跨采集去重。详情返回后
            # 即使首页因重新加载产生新 capture，也不能再次进入同一链接。
            branch_keywords = tuple(
                keyword
                for keyword in normalized_keywords
                if keyword == "internalnavigation" or keyword.startswith("path")
            )
            return (
                None,
                "navigate",
                None,
                "internal_navigation:" + "|".join(branch_keywords),
            )
        if _is_semantic_entry_navigation_action(target_keywords=normalized_keywords):
            return (
                _normalized_semantic_text(self.source_page_type),
                self.action.lower(),
                _normalized_semantic_text(self.expected_next_page_type),
                "semantic_entry:"
                + (_normalized_semantic_text(self.expected_next_page_type) or "unknown"),
            )
        if _is_global_bottom_navigation_action(
            purpose=self.purpose,
            target_keywords=normalized_keywords,
        ):
            # SPA 底部导航是跨页面采集稳定存在的业务分支；同一 Tab
            # 在切换回来后不应再次入队，避免在两个 Tab 间反复跳转。
            return (
                None,
                self.action.lower(),
                _normalized_semantic_text(self.expected_next_page_type),
                f"bottom_navigation:{_normalized_semantic_text(self.target) or ''}",
            )
        return (
            self.source_page_capture_id,
            self.action.lower(),
            _normalized_semantic_text(self.expected_next_page_type),
            normalized_keywords or _normalized_semantic_text(self.target),
        )


@dataclass
class RuleInspectionResultMemory:
    """单个页面上一条规则的质检结果索引。"""

    execution_id: str | None
    rule_id: str | None
    rule_code: str | None
    execution_status: str
    result_summary: str | None = None
    problem_desc: str | None = None
    confidence: float | None = None
    severity: str | None = None
    evidence_artifact_ids: list[str] = field(default_factory=list)

    def to_dict(self, *, compact: bool = False) -> dict[str, Any]:
        """转换为模型可见摘要或完整审计结构。"""
        result: dict[str, Any] = {
            "execution_id": self.execution_id,
            "rule_id": self.rule_id,
            "rule_code": self.rule_code,
            "execution_status": self.execution_status,
        }
        if not compact:
            result.update(
                {
                    "result_summary": self.result_summary,
                    "problem_desc": self.problem_desc,
                    "confidence": self.confidence,
                    "severity": self.severity,
                    "evidence_artifact_ids": list(self.evidence_artifact_ids),
                }
            )
        return result


@dataclass
class PageInspectionMemory:
    """一个页面采集快照对应的候选规则与质检进度。"""

    capture: PageCaptureMemory
    page_type: str
    candidate_rules: list[dict[str, Any]] = field(default_factory=list)
    rule_results: dict[str, RuleInspectionResultMemory] = field(default_factory=dict)
    pending_rule_judgements: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_capture_roles: dict[str, str] = field(default_factory=dict)
    interaction_evidence_ready: bool = False
    interaction_evidence_unavailable: bool = False

    @property
    def completed(self) -> bool:
        """兼容旧调用方：任一页面执行终态都表示不再阻塞探索。"""
        return self.capture.inspection_terminal

    @property
    def candidate_rule_keys(self) -> list[str]:
        """返回候选规则的稳定主键列表。"""
        return [
            key
            for item in self.candidate_rules
            if (key := _rule_key(item.get("rule_id"), item.get("rule_code"))) is not None
        ]

    @property
    def pre_interaction_complete(self) -> bool:
        """交互动作执行前可判定的规则是否已经全部形成结果。"""
        expected = {
            key
            for item in self.candidate_rules
            if item.get("requires_interaction_evidence") is not True
            if (key := _rule_key(item.get("rule_id"), item.get("rule_code"))) is not None
        }
        return expected.issubset(self.rule_results)

    def refresh_completion(self) -> None:
        """候选规则全部有结构化结果后标记页面完成或降级完成。"""
        if self.capture.inspection_status == PageInspectionStatus.FAILED.value:
            return
        expected = set(self.candidate_rule_keys)
        if not expected or expected.issubset(self.rule_results):
            statuses = {item.execution_status for item in self.rule_results.values()}
            self.capture.inspection_status = (
                PageInspectionStatus.DEGRADED.value
                if statuses.intersection(REVIEW_RULE_EXECUTION_STATUSES)
                else PageInspectionStatus.COMPLETED.value
            )
            return
        self.capture.inspection_status = PageInspectionStatus.EVALUATING.value

    def to_dict(self, *, current: bool, compact: bool = False) -> dict[str, Any]:
        """转换为页面级工作记忆快照。"""
        if self.completed:
            status = self.capture.inspection_status
        else:
            status = "inspecting" if current else "pending"
        counts: dict[str, int] = {}
        for result in self.rule_results.values():
            counts[result.execution_status] = counts.get(result.execution_status, 0) + 1
        return {
            **self.capture.to_dict(),
            "page_type": self.page_type,
            "status": status,
            "inspection_status": self.capture.inspection_status,
            "quality_status": self._overall_status(),
            "candidate_rule_count": len(self.candidate_rule_keys),
            "completed_rule_count": len(
                set(self.candidate_rule_keys).intersection(self.rule_results)
            ),
            "candidate_rules": [dict(item) for item in self.candidate_rules],
            "evidence_capture_roles": dict(self.evidence_capture_roles),
            "interaction_evidence_ready": self.interaction_evidence_ready,
            "interaction_evidence_unavailable": self.interaction_evidence_unavailable,
            "pending_rule_judgements": [
                dict(item) for item in self.pending_rule_judgements.values()
            ],
            "result": {
                "overall_status": self._overall_status(),
                "status_counts": {key: counts[key] for key in sorted(counts)},
                "rules": [item.to_dict(compact=compact) for item in self.rule_results.values()],
            },
        }

    def _overall_status(self) -> str:
        """聚合页面结果；不确定状态优先转人工，避免被确定性失败掩盖。"""
        if not self.completed:
            return "in_progress"
        if self.capture.inspection_status in {
            PageInspectionStatus.DEGRADED.value,
            PageInspectionStatus.FAILED.value,
        }:
            return "needs_review"
        if not self.candidate_rule_keys:
            return "no_applicable_rules"
        statuses = {item.execution_status for item in self.rule_results.values()}
        if statuses.intersection(REVIEW_RULE_EXECUTION_STATUSES):
            return "needs_review"
        if "fail" in statuses:
            return "fail"
        if "warning" in statuses:
            return "warning"
        if statuses == {"skipped"}:
            return "skipped"
        return "pass"


@dataclass
class PageInspectionState:
    """单次 Run 的页面质检状态容器。"""

    _page_captures: dict[str, PageCaptureMemory] = field(default_factory=dict, repr=False)
    _page_inspections: dict[str, PageInspectionMemory] = field(default_factory=dict, repr=False)
    _exploration_actions: dict[str, ExplorationActionMemory] = field(
        default_factory=dict, repr=False
    )
    _rule_write_rejection_counts: dict[str, int] = field(default_factory=dict, repr=False)
    _visited_bottom_navigation_targets: set[str] = field(default_factory=set, repr=False)
    _visited_page_tab_targets: set[tuple[str, str]] = field(default_factory=set, repr=False)
    _rule_target_page_types: set[str] = field(default_factory=set, repr=False)
    _background_snapshot_page_capture_ids: set[str] = field(default_factory=set, repr=False)
    _exploration_sequence: int = field(default=0, repr=False)
    _current_page_capture_id: str | None = field(default=None, repr=False)
    _page_type_assessments: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _page_type_assessment_scope_complete: bool = field(default=False, repr=False)
    _page_type_assessment_scope_page_types: tuple[str, ...] = field(
        default_factory=tuple,
        repr=False,
    )

    def set_rule_target_page_types(self, page_types: set[str]) -> None:
        """记录当前规则集需要覆盖的页面类型，供探索候选排序使用。"""
        self._rule_target_page_types = {
            normalized
            for page_type in page_types
            if (normalized := _normalized_semantic_text(page_type)) is not None
        }

    def remember_page_capture(
        self,
        *,
        page_capture_id: str,
        page_url: str | None,
        page_title: str | None,
        page_seq: int | None,
        artifact_ids: list[str],
        content_fingerprint: str | None = None,
        visible_text_fingerprint: str | None = None,
        obstruction_dismissal_count: int = 0,
        exploration_action_id: str | None = None,
        supersedes_page_capture_id: str | None = None,
    ) -> None:
        """记录最新页面采集，并把它设为当前页面。"""
        exploration_action = (
            self._exploration_actions.get(exploration_action_id)
            if exploration_action_id is not None
            else None
        )
        is_auxiliary_return = _is_auxiliary_page_return_action(exploration_action)
        if is_auxiliary_return:
            # 浏览器 back 的落点尚未经过页面识别，禁止调用方提前迁移辅助页生命周期。
            supersedes_page_capture_id = None
        source_capture = (
            self._page_captures.get(supersedes_page_capture_id)
            if supersedes_page_capture_id is not None
            else None
        )
        source_inspection = (
            self._page_inspections.get(supersedes_page_capture_id)
            if supersedes_page_capture_id is not None
            else None
        )
        is_rule_evidence = bool(
            exploration_action is not None
            and str(exploration_action.purpose or "").startswith("rule_evidence:")
            # 旧版本曾把开屏关闭等生命周期动作错误提升为规则取证动作。
            # 兼容仍在运行的旧工作记忆时必须按页面过渡处理，禁止继承父页语义。
            and not exploration_action.resumes_page_exploration
        )
        is_supplemental_evidence = bool(
            exploration_action is not None
            and (exploration_action.action in {"scroll", "timed_capture"} or is_rule_evidence)
            and exploration_action.source_page_capture_id == supersedes_page_capture_id
            and source_capture is not None
            and source_inspection is not None
        )
        if exploration_action_id is not None:
            # 辅助页 back 必须等页面识别验证真实落点后才能标记成功；其他动作仍在
            # 采集成功时结算，避免来源页 supersede 把当前 selected 动作一并淘汰。
            if is_auxiliary_return and exploration_action is not None:
                exploration_action.result_page_capture_id = page_capture_id
            else:
                self.mark_exploration_action_explored(
                    exploration_action_id=exploration_action_id,
                    result_page_capture_id=page_capture_id,
                )
        if (
            exploration_action is not None
            and exploration_action.side_evidence_only
            and source_capture is not None
            and source_inspection is not None
            and supersedes_page_capture_id is not None
        ):
            self._remember_side_evidence_capture(
                page_capture_id=page_capture_id,
                page_url=page_url,
                page_title=page_title,
                page_seq=page_seq,
                artifact_ids=artifact_ids,
                content_fingerprint=content_fingerprint,
                visible_text_fingerprint=visible_text_fingerprint,
                action=exploration_action,
                source_capture=source_capture,
                source_inspection=source_inspection,
            )
            return
        if is_supplemental_evidence:
            if is_rule_evidence:
                # 交互取证可能进入落地页；仅迁移规则上下文，旧页其他局部 selector
                # 必须失效，不能像滚动补采一样重绑到新 DOM。
                self._supersede_capture(supersedes_page_capture_id, page_capture_id)
            else:
                self._supersede_for_supplemental_evidence(
                    source_page_capture_id=supersedes_page_capture_id,
                    new_page_capture_id=page_capture_id,
                )
        else:
            self._supersede_capture(supersedes_page_capture_id, page_capture_id)
        previous = self._page_captures.get(page_capture_id)
        inherited = source_capture if is_supplemental_evidence else previous
        capture = PageCaptureMemory(
            page_capture_id=page_capture_id,
            page_url=page_url,
            page_title=page_title,
            page_seq=page_seq,
            artifact_ids=list(artifact_ids),
            content_fingerprint=content_fingerprint,
            visible_text_fingerprint=visible_text_fingerprint,
            obstruction_dismissal_count=max(0, obstruction_dismissal_count),
            page_detection=inherited.page_detection if inherited is not None else None,
            exploration_action_id=exploration_action_id,
            supersedes_page_capture_id=supersedes_page_capture_id,
            lifecycle_status="active",
            superseded_by_page_capture_id=(
                previous.superseded_by_page_capture_id if previous is not None else None
            ),
            abandon_reason=previous.abandon_reason if previous is not None else None,
            inspection_status=(
                inherited.inspection_status
                if inherited is not None
                else PageInspectionStatus.CAPTURED.value
            ),
            failure_stage=inherited.failure_stage if inherited is not None else None,
            failure_code=inherited.failure_code if inherited is not None else None,
            failure_message=inherited.failure_message if inherited is not None else None,
            failure_retryable=inherited.failure_retryable if inherited is not None else None,
        )
        self._page_captures[page_capture_id] = capture
        if is_supplemental_evidence and supersedes_page_capture_id is not None:
            inspection = self._page_inspections.pop(supersedes_page_capture_id, None)
            if inspection is not None:
                inspection.capture = capture
                self._record_evidence_capture_transition(
                    inspection=inspection,
                    action=exploration_action,
                    source_page_capture_id=supersedes_page_capture_id,
                    new_page_capture_id=page_capture_id,
                )
                self._page_inspections[page_capture_id] = inspection
        else:
            inspection = self._page_inspections.get(page_capture_id)
        if inspection is not None:
            inspection.capture = capture
        self._current_page_capture_id = None if capture.inspection_terminal else page_capture_id

    def _remember_side_evidence_capture(
        self,
        *,
        page_capture_id: str,
        page_url: str | None,
        page_title: str | None,
        page_seq: int | None,
        artifact_ids: list[str],
        content_fingerprint: str | None,
        visible_text_fingerprint: str | None,
        action: ExplorationActionMemory,
        source_capture: PageCaptureMemory,
        source_inspection: PageInspectionMemory,
    ) -> None:
        """记录刷新等旁路证据，但保持规则主采集指向操作后的 primary。"""
        self._page_captures[page_capture_id] = PageCaptureMemory(
            page_capture_id=page_capture_id,
            page_url=page_url,
            page_title=page_title,
            page_seq=page_seq,
            artifact_ids=list(artifact_ids),
            content_fingerprint=content_fingerprint,
            visible_text_fingerprint=visible_text_fingerprint,
            page_detection=source_capture.page_detection,
            exploration_action_id=action.exploration_action_id,
            supersedes_page_capture_id=source_capture.page_capture_id,
            lifecycle_status="evidence_only",
            inspection_status=PageInspectionStatus.COMPLETED.value,
        )
        for role in action.evidence_result_roles:
            source_inspection.evidence_capture_roles[role] = page_capture_id
        source_inspection.evidence_capture_roles["primary"] = source_capture.page_capture_id
        self._current_page_capture_id = source_capture.page_capture_id

    @staticmethod
    def _record_evidence_capture_transition(
        *,
        inspection: PageInspectionMemory,
        action: ExplorationActionMemory | None,
        source_page_capture_id: str,
        new_page_capture_id: str,
    ) -> None:
        """把真实交互前后采集绑定到规则证据角色，并让最新补采成为 primary。"""
        roles = {
            role: capture_id
            for role, capture_id in inspection.evidence_capture_roles.items()
            if role != "primary"
        }
        if action is not None:
            for role in action.evidence_source_roles:
                roles[role] = source_page_capture_id
            for role in action.evidence_result_roles:
                roles[role] = new_page_capture_id
        roles["primary"] = new_page_capture_id
        inspection.evidence_capture_roles = roles
        inspection.interaction_evidence_ready = True

    def _supersede_for_supplemental_evidence(
        self, *, source_page_capture_id: str | None, new_page_capture_id: str
    ) -> None:
        """滚动/时序补采迁移同一页面质检上下文，不重复页面识别。"""
        source_id = _optional_text(source_page_capture_id)
        if source_id is None or source_id == new_page_capture_id:
            return
        source = self._page_captures.get(source_id)
        if source is None or source.lifecycle_status != "active":
            return
        source.lifecycle_status = "superseded"
        source.superseded_by_page_capture_id = new_page_capture_id
        for action in self._exploration_actions.values():
            if action.source_page_capture_id == source_id and action.status in {
                "candidate",
                "selected",
            }:
                action.source_page_capture_id = new_page_capture_id

    def has_page_capture(self, page_capture_id: str) -> bool:
        """判断页面采集是否由当前 Run 产生。"""
        return page_capture_id in self._page_captures

    def is_nested_return_capture(self, page_capture_id: str) -> bool:
        """判断当前采集是否由详情/播放页的受控返回动作产生。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.exploration_action_id is None:
            return False
        action = self._exploration_actions.get(capture.exploration_action_id)
        if action is None or action.action.strip().lower() != "back":
            return False
        return any(
            _normalized_semantic_text(keyword) == "nestedreturn"
            for keyword in action.target_keywords
        )

    def has_prior_obstruction_capture_in_chain(self, page_capture_id: str) -> bool:
        """判断当前弹窗采集链上是否已有同类型遮挡页被质检过。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or not self._is_obstruction_page_capture(capture):
            return False
        current_page_type = (
            capture.page_detection.page_type if capture.page_detection is not None else None
        )
        parent_id = _optional_text(capture.supersedes_page_capture_id)
        visited: set[str] = set()
        while parent_id is not None and parent_id not in visited:
            visited.add(parent_id)
            parent = self._page_captures.get(parent_id)
            if parent is None:
                return False
            parent_page_type = (
                parent.page_detection.page_type if parent.page_detection is not None else None
            )
            if self._is_obstruction_page_capture(parent) and (
                current_page_type is None or parent_page_type == current_page_type
            ):
                return True
            parent_id = _optional_text(parent.supersedes_page_capture_id)
        return False

    def pending_detection_page_capture_id(self) -> str | None:
        """返回必须先识别、禁止继续盲目补采的当前页面。"""
        capture_id = self._current_page_capture_id
        capture = self._page_captures.get(capture_id) if capture_id is not None else None
        if (
            capture is None
            or capture.lifecycle_status != "active"
            or capture.inspection_terminal
            or capture.page_detection is not None
        ):
            return None
        return capture_id

    def page_artifact_ids(self, page_capture_id: str) -> list[str]:
        """返回当前 Run 指定页面采集的真实证据 ID。"""
        capture = self._page_captures.get(page_capture_id)
        return list(capture.artifact_ids) if capture is not None else []

    def has_page_capture_history(self) -> bool:
        """判断当前 Run 是否至少形成过一次页面采集。"""
        return bool(self._page_captures)

    def remember_page_detection(
        self,
        *,
        page_capture_id: str,
        detection_status: str,
        page_type: str | None,
        rule_matching_allowed: bool,
        page_state: str | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
        retryable: bool = False,
    ) -> bool:
        """记录页面识别门禁结果；页面不存在时返回 False。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.inspection_terminal:
            return False
        capture.page_detection = PageDetectionMemory(
            detection_status=detection_status,
            page_type=page_type,
            page_state=page_state,
            rule_matching_allowed=rule_matching_allowed,
        )
        if detection_status == "recognized" and page_type is not None:
            self._fail_semantic_entry_action_if_expected_page_type_not_reached(
                page_capture_id=page_capture_id,
                current_page_type=page_type,
            )
            self._expire_redundant_page_tab_actions(
                current_page_capture_id=page_capture_id,
                current_page_type=page_type,
            )
        if detection_status == "error":
            self.fail_page_inspection(
                page_capture_id=page_capture_id,
                failure_stage="page_detection",
                failure_code=failure_code or "PAGE_DETECTION_FAILED",
                failure_message=(
                    failure_message or "页面识别失败，当前页面不再阻塞后续探索。"
                ),
                retryable=retryable,
            )
        elif detection_status == "recognized" and rule_matching_allowed:
            capture.inspection_status = PageInspectionStatus.MATCHING.value
        else:
            capture.inspection_status = PageInspectionStatus.DETECTING.value
        return True

    def _expire_redundant_page_tab_actions(
        self,
        *,
        current_page_capture_id: str,
        current_page_type: str,
    ) -> list[str]:
        """目标页面已由明确入口到达时，淘汰会把浏览器拉回旧页的同目标 Tab。

        页面类型相同不必然表示两个业务分支等价，因此这里只处理确定性生成的
        ``page_tab_navigation``，且要求本次成功动作本身不是页内 Tab。这样既能
        避免“活动页已经到达却恢复视频页再点福利 Tab”，也不会吞掉同类列表中的
        多个真实分类 Tab。
        """
        capture = self._page_captures.get(current_page_capture_id)
        reached_by = (
            self._exploration_actions.get(capture.exploration_action_id)
            if capture is not None and capture.exploration_action_id is not None
            else None
        )
        normalized_page_type = _normalized_semantic_text(current_page_type)
        if (
            reached_by is None
            or reached_by.status != "explored"
            or normalized_page_type is None
            or _normalized_semantic_text(reached_by.expected_next_page_type) != normalized_page_type
            or _is_page_tab_navigation_action(reached_by)
        ):
            return []

        expired_ids: list[str] = []
        for action in self._exploration_actions.values():
            if (
                action.exploration_action_id == reached_by.exploration_action_id
                or action.status not in {"candidate", "selected"}
                or action.mandatory
                or not _is_page_tab_navigation_action(action)
                or _normalized_semantic_text(action.expected_next_page_type) != normalized_page_type
                or _normalized_semantic_text(action.source_page_type) == normalized_page_type
            ):
                continue
            action.status = "skipped"
            action.result_page_capture_id = current_page_capture_id
            action.failure_reason = "expected_page_type_already_reached"
            action.failure_code = "EXPECTED_PAGE_TYPE_ALREADY_REACHED"
            action.failure_message = (
                "同一目标页面类型已由其他明确入口成功到达，已跳过旧页面的冗余 Tab，"
                "避免恢复来源页后重走入口生命周期。"
            )
            action.failure_retryable = False
            expired_ids.append(action.exploration_action_id)
        return expired_ids

    def _fail_semantic_entry_action_if_expected_page_type_not_reached(
        self,
        *,
        page_capture_id: str,
        current_page_type: str,
    ) -> bool:
        """语义入口完成采集后若未到达预期页，回写为失败终态。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.exploration_action_id is None:
            return False
        action = self._exploration_actions.get(capture.exploration_action_id)
        if action is None or action.status != "explored":
            return False
        normalized_keywords = tuple(
            sorted(
                keyword
                for item in action.target_keywords
                if (keyword := _normalized_semantic_text(item)) is not None
            )
        )
        if not _is_profile_local_navigation_action(target_keywords=normalized_keywords):
            return False
        expected_page_type = _normalized_semantic_text(action.expected_next_page_type)
        actual_page_type = _normalized_semantic_text(current_page_type)
        if expected_page_type not in {"activity", "settings", "payment"}:
            return False
        if actual_page_type == expected_page_type:
            return False
        action.status = "failed"
        action.result_page_capture_id = page_capture_id
        action.failure_reason = "expected_page_type_not_reached"
        action.failure_code = "EXPECTED_PAGE_TYPE_NOT_REACHED"
        action.failure_message = (
            f"个人中心专属入口执行后识别为 {current_page_type!r}，未到达预期页面类型 "
            f"{action.expected_next_page_type!r}。"
        )
        action.failure_retryable = False
        return True

    def page_detection_result(self, page_capture_id: str) -> dict[str, Any] | None:
        """返回指定采集的页面识别门禁结果副本。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.page_detection is None:
            return None
        return capture.page_detection.to_dict()

    def is_same_url_obstruction_page(self, page_capture_id: str) -> bool:
        """判断当前弹层采集是否仍位于可信来源动作的同一业务地址。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.page_url is None:
            return False
        source_id = capture.supersedes_page_capture_id
        if source_id is None and capture.exploration_action_id is not None:
            source_action = self._exploration_actions.get(capture.exploration_action_id)
            source_id = source_action.source_page_capture_id if source_action is not None else None
        source = self._page_captures.get(source_id) if source_id is not None else None
        return bool(
            source is not None
            and source.page_url is not None
            and _normalized_page_location(capture.page_url)
            == _normalized_page_location(source.page_url)
        )

    def is_obstruction_chain_continuation(self, page_capture_id: str) -> bool:
        """判断当前采集是否由已质检弹窗的关闭动作产生，避免逐层重复跑规则。"""
        capture = self._page_captures.get(page_capture_id)
        if (
            capture is None
            or capture.exploration_action_id is None
            or capture.supersedes_page_capture_id is None
        ):
            return False
        source = self._page_captures.get(capture.supersedes_page_capture_id)
        action = self._exploration_actions.get(capture.exploration_action_id)
        source_state = (
            _normalized_semantic_text(source.page_detection.page_state)
            if source is not None and source.page_detection is not None
            else None
        )
        return bool(
            source_state in {"popup", "modal", "overlay", "dialog"}
            and action is not None
            and _is_obstruction_dismissal_action(action)
            and self.is_same_url_obstruction_page(page_capture_id)
        )

    def can_reuse_completed_obstruction_source(
        self,
        *,
        page_capture_id: str,
        page_type: str | None,
        page_state: str | None,
    ) -> bool:
        """判断稳定页关闭局部遮挡后的结果采集能否复用来源页质检。"""
        capture = self._page_captures.get(page_capture_id)
        if (
            capture is None
            or capture.exploration_action_id is None
            or capture.supersedes_page_capture_id is None
        ):
            return False
        source = self._page_captures.get(capture.supersedes_page_capture_id)
        action = self._exploration_actions.get(capture.exploration_action_id)
        source_detection = source.page_detection if source is not None else None
        return bool(
            source is not None
            and source.inspection_terminal
            and source_detection is not None
            and _normalized_semantic_text(source_detection.page_state) in _STABLE_SOURCE_PAGE_STATES
            and _normalized_semantic_text(page_state) in _STABLE_SOURCE_PAGE_STATES
            and _normalized_semantic_text(source_detection.page_type)
            == _normalized_semantic_text(page_type)
            and action is not None
            and action.mandatory
            and action.requires_source_inspection_complete
            and _is_obstruction_dismissal_action(action)
            and self.is_same_url_obstruction_page(page_capture_id)
        )

    def _should_clear_obstruction_chain(self, action: ExplorationActionMemory) -> bool:
        """仅对已识别为弹窗的来源页启用连关，开屏广告仍单独留证。"""
        source = self._page_captures.get(action.source_page_capture_id or "")
        source_state = (
            _normalized_semantic_text(source.page_detection.page_state)
            if source is not None and source.page_detection is not None
            else None
        )
        return bool(
            action.mandatory
            and source_state in {"popup", "modal", "overlay", "dialog"}
            and _is_obstruction_dismissal_action(action)
        )

    def is_source_restore_transit(self, page_capture_id: str) -> bool:
        """判断当前采集是否处于来源恢复的开屏/弹窗过渡链。"""
        if page_capture_id not in self._page_captures:
            return False
        return self._pending_source_restore() is not None

    def rebind_restored_source_action_if_ready(
        self,
        *,
        page_capture_id: str,
        detection_status: str,
        page_type: str | None,
        page_state: str | None,
    ) -> str | None:
        """仅在恢复页稳定命中原页面后重绑旧动作。

        返回 ``ready_reused`` 表示原来源页已经完成过质检，本次恢复采集只用于
        重新取得可交互 DOM，可直接执行旧 Frontier；返回
        ``ready_requires_inspection`` 表示仍需正常执行该页规则。
        """
        pending = self._pending_source_restore()
        if pending is None or detection_status != "recognized":
            return None
        restore_action, original = pending
        capture = self._page_captures.get(page_capture_id)
        original_source = self._page_captures.get(original.source_page_capture_id)
        if capture is None or original_source is None:
            return None
        expected_page_type = restore_action.expected_next_page_type or original.source_page_type
        normalized_expected_page_type = _normalized_semantic_text(expected_page_type)
        normalized_page_state = _normalized_semantic_text(page_state)
        restored_location = _normalized_page_location(capture.page_url)
        original_location = _normalized_page_location(original_source.page_url)
        if (
            normalized_expected_page_type is None
            or normalized_expected_page_type != _normalized_semantic_text(page_type)
            or normalized_page_state not in _STABLE_SOURCE_PAGE_STATES
            or restored_location is None
            or restored_location != original_location
        ):
            return None

        original.status = "candidate"
        original.source_page_capture_id = page_capture_id
        original.source_page_type = page_type
        for candidate in self._exploration_actions.values():
            if (
                _restore_source_original_action_id(candidate) == original.exploration_action_id
                and _RESTORE_SOURCE_READY_KEYWORD not in candidate.target_keywords
            ):
                candidate.target_keywords.append(_RESTORE_SOURCE_READY_KEYWORD)

        if original_source.inspection_terminal:
            capture.lifecycle_status = "abandoned"
            capture.abandon_reason = "source_restore_transit"
            capture.inspection_status = PageInspectionStatus.COMPLETED.value
            if self._current_page_capture_id == page_capture_id:
                self._current_page_capture_id = None
            return "ready_reused"
        return "ready_requires_inspection"

    def resolve_auxiliary_page_return_if_ready(
        self,
        *,
        page_capture_id: str,
        detection_status: str,
        page_type: str | None,
        page_state: str | None,
    ) -> str | None:
        """验证辅助页历史返回的真实落点后，才迁移页面与动作生命周期。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None:
            return None
        lineage = self._page_capture_lineage_ids(page_capture_id)
        action = next(
            (
                candidate
                for candidate in reversed(list(self._exploration_actions.values()))
                if candidate.status == "selected"
                and _is_auxiliary_page_return_action(candidate)
                and candidate.result_page_capture_id in lineage
            ),
            None,
        )
        if action is None or action.source_page_capture_id is None:
            return None
        auxiliary = self._page_captures.get(action.source_page_capture_id)
        entry_action = (
            self._exploration_actions.get(auxiliary.exploration_action_id)
            if auxiliary is not None and auxiliary.exploration_action_id is not None
            else None
        )
        original_source = (
            self._page_captures.get(entry_action.source_page_capture_id)
            if entry_action is not None and entry_action.source_page_capture_id is not None
            else None
        )
        if auxiliary is None or entry_action is None or original_source is None:
            return None

        normalized_detection_status = _normalized_semantic_text(detection_status)
        normalized_page_state = _normalized_semantic_text(page_state)
        if normalized_detection_status != "recognized":
            if normalized_detection_status != "error":
                return None
            target_matches = False
        elif normalized_page_state in {
            state
            for raw_state in _UNSTABLE_RESTORE_PAGE_STATES
            if (state := _normalized_semantic_text(raw_state)) is not None
        }:
            # 返回来源页后仍可能先遇到弹窗或加载态，保留 pending，等待清障后的后继采集。
            return None
        elif normalized_page_state not in _STABLE_SOURCE_PAGE_STATES:
            target_matches = False
        else:
            target_matches = bool(
                _normalized_semantic_text(action.expected_next_page_type)
                == _normalized_semantic_text(page_type)
                and _normalized_page_identity(capture.page_url) is not None
                and _normalized_page_identity(capture.page_url)
                == _normalized_page_identity(original_source.page_url)
            )
        if not target_matches:
            action.result_page_capture_id = page_capture_id
            self.fail_exploration_action(
                exploration_action_id=action.exploration_action_id,
                failure_code="AUXILIARY_RETURN_TARGET_NOT_REACHED",
                failure_message=(
                    "辅助页浏览器返回未命中真实来源页的稳定类型与地址，"
                    "已保留原辅助页生命周期并停止提前迁移。"
                ),
                retryable=False,
                failure_reason="auxiliary_return_target_not_reached",
            )
            return "target_not_reached"

        self.mark_exploration_action_explored(
            exploration_action_id=action.exploration_action_id,
            result_page_capture_id=page_capture_id,
        )
        self._supersede_capture(auxiliary.page_capture_id, page_capture_id)
        return (
            "ready_reused" if original_source.inspection_terminal else "ready_requires_inspection"
        )

    def has_started_page_inspection(self, page_capture_id: str) -> bool:
        """判断页面是否已经完成候选规则匹配。"""
        return page_capture_id in self._page_inspections

    def has_incomplete_page_inspection(self) -> bool:
        """判断当前 Run 是否仍有已采集但未完成规则写回的活跃页面。"""
        return any(
            self._is_active_capture(page_capture_id) and not capture.inspection_terminal
            for page_capture_id, capture in self._page_captures.items()
        )

    def _is_active_capture(self, page_capture_id: str) -> bool:
        capture = self._page_captures.get(page_capture_id)
        return capture is not None and capture.lifecycle_status == "active"

    def _supersede_capture(
        self, supersedes_page_capture_id: str | None, new_page_capture_id: str
    ) -> None:
        superseded_id = _optional_text(supersedes_page_capture_id)
        if superseded_id is None or superseded_id == new_page_capture_id:
            return
        superseded = self._page_captures.get(superseded_id)
        if superseded is None or superseded.lifecycle_status != "active":
            return
        superseded.lifecycle_status = "superseded"
        superseded.superseded_by_page_capture_id = new_page_capture_id
        for action in self._exploration_actions.values():
            if action.source_page_capture_id != superseded_id or action.status not in {
                "candidate",
                "selected",
            }:
                continue
            is_global_navigation = _is_global_navigation_action(
                purpose=action.purpose,
                target_keywords=tuple(
                    keyword
                    for raw_keyword in action.target_keywords
                    if (keyword := _normalized_semantic_text(raw_keyword)) is not None
                ),
            )
            if is_global_navigation:
                # 首页生成的底部导航属于跨页面全局 Frontier；进入一个业务分支后
                # 仍需保留其余 Tab，不能随局部来源快照一起淘汰。
                continue
            # 遮挡关闭、详情章节等局部动作只对产生它们的页面快照有效。新采集已
            # 替代旧页后全部失效，避免详情返回后又在其他页面执行陈旧 selector。
            action.status = "skipped"
            action.failure_reason = "source_page_superseded"

    def remember_exploration_candidates(
        self,
        *,
        page_capture_id: str,
        page_type: str | None,
        candidate_actions: list[Mapping[str, Any]],
        next_page_hypotheses: list[Mapping[str, Any]],
    ) -> list[str]:
        """把页面识别输出的候选动作写入探索 frontier。"""
        if not self._is_active_capture(page_capture_id):
            return []
        normalized_payloads = _normalized_exploration_payloads(
            page_capture_id=page_capture_id,
            page_type=page_type,
            candidate_actions=candidate_actions,
            next_page_hypotheses=next_page_hypotheses,
        )
        self._remember_active_bottom_navigation_target(normalized_payloads)
        self._remember_active_page_tab_target(
            page_capture_id=page_capture_id,
            payloads=normalized_payloads,
        )
        existing_by_key = {item.dedupe_key(): item for item in self._exploration_actions.values()}
        added_ids: list[str] = []
        for raw_payload in normalized_payloads:
            payload = dict(raw_payload)
            payload_keywords = tuple(
                keyword
                for raw_keyword in _string_list(payload.get("target_keywords"))
                if (keyword := _normalized_semantic_text(raw_keyword)) is not None
            )
            if _is_browser_history_return_intent(
                action=_optional_text(payload.get("action")),
                target=_optional_text(payload.get("target")),
                target_keywords=payload_keywords,
            ):
                if not self._capture_has_nested_navigation_origin(page_capture_id):
                    # 模型可能把入口详情页描述成“返回上一层”。没有真实来源导航时，
                    # 该动作不是业务按钮而是浏览器历史猜测，执行后会进入 about:blank。
                    continue
                payload.update(
                    {
                        "action": "back",
                        "target": "browser_history",
                        "target_keywords": list(
                            dict.fromkeys(
                                [
                                    *_string_list(payload.get("target_keywords")),
                                    "nested_return",
                                ]
                            )
                        ),
                        "purpose": "详情或播放页质检完成后返回来源列表继续探索",
                        "navigation_depth": payload.get("navigation_depth") or 1,
                    }
                )
            normalized_purpose = _normalized_semantic_text(payload.get("purpose")) or ""
            if (
                _normalized_semantic_text(payload.get("action")) in {"click", "goto"}
                and payload.get("expected_next_page_type") is not None
                and payload.get("navigation_depth") is None
                and not any(
                    marker in normalized_purpose
                    for marker in ("关闭", "弹窗", "遮挡", "倒计时", "dismiss", "popup")
                )
            ):
                source_capture = self._page_captures.get(page_capture_id)
                source_action = (
                    self._exploration_actions.get(source_capture.exploration_action_id)
                    if source_capture is not None
                    and source_capture.exploration_action_id is not None
                    else None
                )
                source_depth = (
                    source_action.navigation_depth
                    if source_action is not None and source_action.navigation_depth is not None
                    else 0
                )
                payload["navigation_depth"] = source_depth + 1
            probe = ExplorationActionMemory(
                exploration_action_id="",
                source_page_capture_id=page_capture_id,
                source_page_type=page_type,
                **payload,
            )
            normalized_target = _normalized_semantic_text(probe.target)
            normalized_keywords = tuple(
                keyword
                for item in probe.target_keywords
                if (keyword := _normalized_semantic_text(item)) is not None
            )
            if _is_nested_return_action(
                action=probe.action,
                target=probe.target,
                target_keywords=normalized_keywords,
                purpose=probe.purpose,
            ) and not self._capture_has_nested_navigation_origin(page_capture_id):
                # 入口本身就是详情页，或仅因关闭弹窗而被重新识别成详情页时，
                # 浏览器并不存在可返回的业务来源页。此时执行 back 会落到
                # about:blank，必须只保留由真实列表/同源链接进入详情后的返回动作。
                continue
            if (
                _is_global_bottom_navigation_action(
                    purpose=probe.purpose,
                    target_keywords=normalized_keywords,
                )
                and normalized_target in self._visited_bottom_navigation_targets
            ):
                continue
            page_tab_target_key = self._page_tab_target_key(
                page_capture_id=page_capture_id,
                target=probe.target,
            )
            if (
                _is_page_tab_navigation_action(probe)
                and page_tab_target_key is not None
                and page_tab_target_key in self._visited_page_tab_targets
            ):
                continue
            probe_key = probe.dedupe_key()
            existing = existing_by_key.get(probe_key)
            if (
                existing is not None
                and existing.status == "skipped"
                and existing.failure_reason == "source_page_superseded"
                and _is_semantic_entry_navigation_action(target_keywords=normalized_keywords)
            ):
                existing = None
            if (
                existing is not None
                and existing.status not in {"candidate", "selected"}
                and _is_obstruction_dismissal_action(probe)
            ):
                # 多层弹窗可能在新快照中继续复用完全相同的关闭 selector。
                # 已执行的上一层动作不能吞掉当前层，否则底层业务页会一直停在
                # popup，只执行弹窗规则而漏掉稳定页面规则。
                existing = None
            if existing is not None:
                if existing.status in {"candidate", "selected"}:
                    existing.risk_level = _higher_risk_level(
                        existing.risk_level,
                        probe.risk_level,
                    )
                    if _is_high_risk(existing.risk_level):
                        _block_high_risk_action(existing)
                if existing.status == "candidate" and _is_semantic_nested_content_action(
                    action=probe.action,
                    purpose=probe.purpose,
                    expected_next_page_type=probe.expected_next_page_type,
                    target_keywords=normalized_keywords,
                ):
                    # 同分支上的无转场补采可能改变滚动位置或重排 DOM。详情候选尚未
                    # 执行时，把它重绑到最新列表快照；否则旧来源不再是当前浏览器页，
                    # 会被全局导航抢占并最终在错误页面执行旧 selector。
                    existing.source_page_capture_id = page_capture_id
                    existing.source_page_type = page_type
                    existing.target = probe.target
                    existing.purpose = probe.purpose
                elif existing.status == "candidate" and _is_semantic_entry_navigation_action(
                    target_keywords=normalized_keywords,
                ):
                    existing.source_page_capture_id = page_capture_id
                    existing.source_page_type = page_type
                    existing.target = probe.target
                    existing.target_keywords = probe.target_keywords
                    existing.risk_level = _higher_risk_level(
                        existing.risk_level,
                        probe.risk_level,
                    )
                    existing.purpose = probe.purpose
                    existing.expected_next_page_type = probe.expected_next_page_type
                    existing.navigation_depth = probe.navigation_depth
                    if _is_high_risk(existing.risk_level):
                        _block_high_risk_action(existing)
                elif existing.status == "candidate" and _is_global_bottom_navigation_action(
                    purpose=probe.purpose,
                    target_keywords=normalized_keywords,
                ):
                    # 底部 Tabbar 在 SPA 多个页面复用，同一 Tab 候选应跟随最新可见
                    # Tabbar 重新绑定，避免长队列后拿旧快照 selector 去当前页面点击。
                    existing.source_page_capture_id = page_capture_id
                    existing.source_page_type = page_type
                    existing.target = probe.target
                    existing.target_keywords = probe.target_keywords
                    existing.risk_level = _higher_risk_level(
                        existing.risk_level,
                        probe.risk_level,
                    )
                    existing.purpose = probe.purpose
                    existing.expected_next_page_type = probe.expected_next_page_type
                    existing.navigation_depth = probe.navigation_depth
                continue
            self._exploration_sequence += 1
            exploration_action_id = f"exploration-{self._exploration_sequence}"
            action = ExplorationActionMemory(
                exploration_action_id=exploration_action_id,
                source_page_capture_id=page_capture_id,
                source_page_type=page_type,
                **payload,
            )
            if _is_high_risk(action.risk_level):
                _block_high_risk_action(action)
            self._exploration_actions[exploration_action_id] = action
            existing_by_key[action.dedupe_key()] = action
            if _is_page_tab_navigation_action(action) and page_tab_target_key is not None:
                self._visited_page_tab_targets.add(page_tab_target_key)
            added_ids.append(exploration_action_id)
        auxiliary_return_id = self._ensure_auxiliary_page_return_action(
            page_capture_id=page_capture_id,
            page_type=page_type,
            payloads=normalized_payloads,
        )
        if auxiliary_return_id is not None:
            added_ids.append(auxiliary_return_id)
        return added_ids

    def _ensure_auxiliary_page_return_action(
        self,
        *,
        page_capture_id: str,
        page_type: str | None,
        payloads: list[dict[str, Any]],
    ) -> str | None:
        """辅助页缺少底栏时，只沿真实语义入口建立一次受控历史返回。"""
        normalized_page_type = _normalized_semantic_text(page_type)
        if normalized_page_type != "settings":
            return None
        if any(
            _is_global_bottom_navigation_action(
                purpose=_optional_text(payload.get("purpose")),
                target_keywords=tuple(
                    keyword
                    for raw_keyword in _string_list(payload.get("target_keywords"))
                    if (keyword := _normalized_semantic_text(raw_keyword)) is not None
                ),
            )
            for payload in payloads
        ):
            return None

        capture = self._page_captures.get(page_capture_id)
        source_action = (
            self._exploration_actions.get(capture.exploration_action_id)
            if capture is not None and capture.exploration_action_id is not None
            else None
        )
        if (
            capture is None
            or source_action is None
            or source_action.status != "explored"
            or source_action.origin == "entry"
            or source_action.source_page_capture_id is None
            or _normalized_semantic_text(source_action.expected_next_page_type)
            != normalized_page_type
            or not isinstance(source_action.navigation_depth, int)
            or isinstance(source_action.navigation_depth, bool)
            or source_action.navigation_depth <= 0
        ):
            return None
        source_keywords = tuple(
            keyword
            for raw_keyword in source_action.target_keywords
            if (keyword := _normalized_semantic_text(raw_keyword)) is not None
        )
        if not _is_semantic_entry_navigation_action(target_keywords=source_keywords):
            return None
        source_capture = self._page_captures.get(source_action.source_page_capture_id)
        source_location = (
            _normalized_page_identity(source_capture.page_url)
            if source_capture is not None
            else None
        )
        current_location = _normalized_page_identity(capture.page_url)
        if (
            source_location is None
            or current_location is None
            or source_location == current_location
        ):
            return None
        existing = next(
            (
                action
                for action in self._exploration_actions.values()
                if action.source_page_capture_id == page_capture_id
                and "auxiliary_page_return" in action.target_keywords
            ),
            None,
        )
        if existing is not None:
            return existing.exploration_action_id

        self._exploration_sequence += 1
        action_id = f"exploration-{self._exploration_sequence}"
        self._exploration_actions[action_id] = ExplorationActionMemory(
            exploration_action_id=action_id,
            source_page_capture_id=page_capture_id,
            source_page_type=page_type,
            action="back",
            origin="recovery",
            target="browser_history",
            target_keywords=["auxiliary_page_return"],
            risk_level="low",
            purpose="辅助页不存在底部导航，返回真实来源业务页继续探索",
            expected_next_page_type=source_action.source_page_type,
            navigation_depth=source_action.navigation_depth,
            mandatory=True,
            requires_source_inspection_complete=True,
        )
        return action_id

    def _capture_has_nested_navigation_origin(self, page_capture_id: str) -> bool:
        """判断当前采集是否由真实业务分支进入，而不是入口或弹窗重采集。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.exploration_action_id is None:
            return False
        source_action = self._exploration_actions.get(capture.exploration_action_id)
        if source_action is None or source_action.origin == "entry":
            return False
        normalized_keywords = tuple(
            keyword
            for raw_keyword in source_action.target_keywords
            if (keyword := _normalized_semantic_text(raw_keyword)) is not None
        )
        return _is_nested_content_action(
            purpose=source_action.purpose,
            target_keywords=normalized_keywords,
        ) or _is_internal_navigation_action(target_keywords=normalized_keywords)

    def expire_stale_obstruction_actions(self, *, current_page_capture_id: str) -> list[str]:
        """稳定页出现后终结其他快照遗留的遮挡动作与来源页。"""
        expired_action_ids: list[str] = []
        for action in self._exploration_actions.values():
            source_id = action.source_page_capture_id
            if (
                source_id is None
                or source_id == current_page_capture_id
                or action.status not in {"candidate", "selected"}
                or not action.mandatory
                or not action.blocks_page_inspection
            ):
                continue
            action.status = "skipped"
            action.failure_reason = "stable_page_replaced_obstruction_source"
            expired_action_ids.append(action.exploration_action_id)
            source = self._page_captures.get(source_id)
            if source is not None and source.lifecycle_status == "active":
                source.lifecycle_status = "superseded"
                source.superseded_by_page_capture_id = current_page_capture_id
        return expired_action_ids

    def _remember_active_bottom_navigation_target(self, payloads: list[dict[str, Any]]) -> None:
        """从确定性非激活 Tab 集合推导当前已访问 Tab，避免回跳首页。

        页面识别层会为同一可见 Tabbar 的所有非激活项生成连续
        ``nth-of-type`` 动作，因此 ``1..max(index)`` 中唯一缺失项就是当前激活项。
        该项已经对应当前页面，不应在切换到其他 Tab 后重新进入 Frontier。
        """
        indexed_targets: dict[int, str] = {}
        for payload in payloads:
            target = _optional_text(payload.get("target"))
            keywords = tuple(
                keyword
                for item in _string_list(payload.get("target_keywords"))
                if (keyword := _normalized_semantic_text(item)) is not None
            )
            if target is None or not _is_global_bottom_navigation_action(
                purpose=_optional_text(payload.get("purpose")),
                target_keywords=keywords,
            ):
                continue
            match = re.search(r"nth-of-type\((\d+)\)", target)
            if match is not None:
                indexed_targets[int(match.group(1))] = target
        if len(indexed_targets) < 1:
            return
        missing = set(range(1, max(indexed_targets) + 1)).difference(indexed_targets)
        if len(missing) != 1:
            return
        sample_target = next(iter(indexed_targets.values()))
        active_target = re.sub(
            r"nth-of-type\(\d+\)",
            f"nth-of-type({missing.pop()})",
            sample_target,
            count=1,
        )
        normalized = _normalized_semantic_text(active_target)
        if normalized is not None:
            self._visited_bottom_navigation_targets.add(normalized)

    def _remember_active_page_tab_target(
        self,
        *,
        page_capture_id: str,
        payloads: list[dict[str, Any]],
    ) -> None:
        """从非激活页内 Tab 集合推断当前 Tab，避免切换后反向重复点击。"""
        indexed_targets: dict[int, str] = {}
        for payload in payloads:
            probe = ExplorationActionMemory(
                exploration_action_id="",
                source_page_capture_id=page_capture_id,
                source_page_type=None,
                **payload,
            )
            if not _is_page_tab_navigation_action(probe):
                continue
            target = _optional_text(probe.target)
            match = re.search(r"nth-of-type\((\d+)\)", target or "")
            if match is not None and target is not None:
                indexed_targets[int(match.group(1))] = target
        if not indexed_targets:
            return
        missing = set(range(1, max(indexed_targets) + 1)).difference(indexed_targets)
        if len(missing) != 1:
            return
        sample_target = next(iter(indexed_targets.values()))
        active_target = re.sub(
            r"nth-of-type\(\d+\)",
            f"nth-of-type({missing.pop()})",
            sample_target,
            count=1,
        )
        key = self._page_tab_target_key(
            page_capture_id=page_capture_id,
            target=active_target,
        )
        if key is not None:
            self._visited_page_tab_targets.add(key)

    def _page_tab_target_key(
        self,
        *,
        page_capture_id: str,
        target: str | None,
    ) -> tuple[str, str] | None:
        """按业务路由和结构 selector 标识同一组页内 Tab。"""
        capture = self._page_captures.get(page_capture_id)
        location = _normalized_page_location(capture.page_url if capture is not None else None)
        normalized_target = _normalized_semantic_text(target)
        if location is None or normalized_target is None:
            return None
        return location, normalized_target

    def ensure_transient_page_recovery(
        self,
        *,
        page_capture_id: str,
        page_type: str | None,
    ) -> str | None:
        """临时不执行瞬态页补采，直接按证据不足收口。"""
        capture = self._page_captures.get(page_capture_id)
        if (
            capture is None
            or capture.lifecycle_status != "active"
            or capture.inspection_terminal
            or page_type not in TRANSIENT_RECOVERY_KINDS
        ):
            return None
        self.mark_supplemental_evidence_insufficient(
            page_capture_id=page_capture_id,
            failure_stage="page_detection",
            failure_message=(
                f"{page_type} 需要同会话补采后才能继续识别；"
                "当前策略暂不执行补采，已按证据不足收口。"
            ),
        )
        return None

    def ensure_source_restore_transit_observation(
        self,
        *,
        page_capture_id: str,
        page_type: str | None,
        max_attempts: int = DEFAULT_MAX_TRANSIENT_RECOVERY_ATTEMPTS,
    ) -> str | None:
        """来源恢复落在开屏倒计时时，只观测过渡态，不重复执行整页规则。"""
        capture = self._page_captures.get(page_capture_id)
        pending = self._pending_source_restore()
        if (
            capture is None
            or capture.lifecycle_status != "active"
            or capture.inspection_terminal
            or pending is None
        ):
            return None
        _, original = pending
        marker = f"{_RESTORE_SOURCE_TRANSIT_PREFIX}{original.exploration_action_id}"
        attempt_count = sum(
            marker in action.target_keywords for action in self._exploration_actions.values()
        )
        if attempt_count >= max_attempts:
            self.fail_page_inspection(
                page_capture_id=page_capture_id,
                failure_stage="source_restore",
                failure_code="SOURCE_RESTORE_TRANSIT_EXHAUSTED",
                failure_message=(
                    f"来源恢复连续观测 {max_attempts} 次后仍停留在开屏过渡态，"
                    "已停止本轮观测并重新评估来源恢复。"
                ),
                retryable=False,
            )
            return None
        self._exploration_sequence += 1
        action_id = f"exploration-{self._exploration_sequence}"
        self._exploration_actions[action_id] = ExplorationActionMemory(
            exploration_action_id=action_id,
            source_page_capture_id=page_capture_id,
            source_page_type=page_type,
            action="observe",
            target="same_session",
            target_keywords=[marker],
            risk_level="low",
            purpose=f"{marker}:attempt-{attempt_count + 1}",
            mandatory=True,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )
        return action_id

    def ensure_scroll_coverage_action(self, *, page_capture_id: str) -> str | None:
        """临时不执行滚动补采，直接按证据不足收口。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.lifecycle_status != "active" or capture.inspection_terminal:
            return None
        self.mark_supplemental_evidence_insufficient(
            page_capture_id=page_capture_id,
            failure_stage="page_coverage",
            failure_message="页面覆盖需要滚动补采；当前策略暂不执行补采，已按证据不足收口。",
        )
        return None

    def ensure_temporal_capture_action(self, *, page_capture_id: str) -> str | None:
        """临时不执行时序补采，直接按证据不足收口。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.lifecycle_status != "active" or capture.inspection_terminal:
            return None
        self.mark_supplemental_evidence_insufficient(
            page_capture_id=page_capture_id,
            failure_stage="temporal_capture",
            failure_message="规则需要多帧或时序补采；当前策略暂不执行补采，已按证据不足收口。",
        )
        return None

    def ensure_interaction_evidence_action(
        self,
        *,
        page_capture_id: str,
        match_terms: list[str],
        required_capture_roles: list[str] | None = None,
    ) -> str | None:
        """临时不执行交互补采，保留普通探索候选并按证据不足收口。"""
        del match_terms, required_capture_roles
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.lifecycle_status != "active" or capture.inspection_terminal:
            return None
        self.mark_supplemental_evidence_insufficient(
            page_capture_id=page_capture_id,
            failure_stage="interaction_evidence",
            failure_message=(
                "规则需要 click/hover 交互补采；当前策略暂不执行补采，"
                "已按证据不足收口。"
            ),
        )
        return None

    def defer_snapshot_page_inspection(self, *, page_capture_id: str) -> bool:
        """把只依赖静态快照的剩余规则移入后台，允许浏览器推进下一页。"""
        inspection = self._page_inspections.get(page_capture_id)
        if inspection is None or inspection.completed:
            return False
        if any(
            action.source_page_capture_id == page_capture_id
            and action.mandatory
            and action.requires_source_inspection_complete
            and action.status in {"candidate", "selected"}
            for action in self._exploration_actions.values()
        ):
            # 开屏广告、弹窗等过渡页的后续动作依赖当前页规则完整收敛。
            # 若此时把静态规则放到后台，页面可能先跳转，迟到的 Judge 结果
            # 就无法再写回原页面，最终会被误判为执行预算耗尽。
            return False
        completed_keys = set(inspection.rule_results)
        unresolved = [
            item
            for item in inspection.candidate_rules
            if _rule_key(
                _optional_text(item.get("rule_id")),
                _optional_text(item.get("rule_code")),
            )
            not in completed_keys
        ]
        if not unresolved or any(
            _optional_text(item.get("session_dependency")) != "snapshot" for item in unresolved
        ):
            return False
        self._background_snapshot_page_capture_ids.add(page_capture_id)
        if self._current_page_capture_id == page_capture_id:
            self._current_page_capture_id = None
        return True

    def ensure_inspectable_page_transition(
        self, *, page_capture_id: str, page_type: str
    ) -> str | None:
        """临时不执行可质检过渡页重采，直接按证据不足收口。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.lifecycle_status != "active" or capture.inspection_terminal:
            return None
        self.mark_supplemental_evidence_insufficient(
            page_capture_id=page_capture_id,
            failure_stage="page_transition",
            failure_message=(
                f"{page_type} 需要同会话重采后才能继续页面探索；"
                "当前策略暂不执行补采，已按证据不足收口。"
            ),
        )
        return None

    def require_exploration_action(
        self,
        *,
        exploration_action_id: str,
        blocks_page_inspection: bool,
        supersedes_source_page: bool,
        requires_source_inspection_complete: bool = False,
    ) -> bool:
        """把页面恢复动作升级为不可被普通收口跳过的必执行动作。"""
        action = self._exploration_actions.get(exploration_action_id)
        if action is None or action.status not in {"candidate", "selected"}:
            return False
        if action.action == "click" and (
            blocks_page_inspection or requires_source_inspection_complete
        ):
            is_page_obstruction_recovery = self._is_page_obstruction_recovery_context(action)
            source_capture = self._page_captures.get(action.source_page_capture_id or "")
            source_is_obstruction = self._is_obstruction_page_capture(source_capture)
            is_obstruction_dismissal = _is_obstruction_dismissal_action(action)
            uses_page_obstruction_budget = source_is_obstruction or is_obstruction_dismissal
            page_attempt_count = (
                self._page_obstruction_recovery_attempt_count(action)
                if uses_page_obstruction_budget
                else 0
            )
            same_target_attempt_count = self._same_target_obstruction_attempt_count(action)
            productive_attempt_count = (
                self._productive_obstruction_recovery_attempt_count(action)
                if uses_page_obstruction_budget
                else 0
            )
            if is_page_obstruction_recovery and productive_attempt_count > 0:
                obstruction_attempt_count = page_attempt_count
                max_obstruction_attempts = (
                    DEFAULT_MAX_PRODUCTIVE_OBSTRUCTION_RECOVERY_ATTEMPTS
                )
            elif is_page_obstruction_recovery:
                uses_short_visible_text_budget = (
                    source_capture is not None
                    and source_capture.visible_text_fingerprint is not None
                    and same_target_attempt_count >= DEFAULT_MAX_OBSTRUCTION_RECOVERY_ATTEMPTS
                )
                if uses_short_visible_text_budget:
                    obstruction_attempt_count = same_target_attempt_count
                    max_obstruction_attempts = DEFAULT_MAX_OBSTRUCTION_RECOVERY_ATTEMPTS
                else:
                    obstruction_attempt_count = page_attempt_count
                    max_obstruction_attempts = DEFAULT_MAX_PAGE_OBSTRUCTION_RECOVERY_ATTEMPTS
            elif (
                is_obstruction_dismissal
                and page_attempt_count >= DEFAULT_MAX_PAGE_OBSTRUCTION_RECOVERY_ATTEMPTS
            ):
                obstruction_attempt_count = page_attempt_count
                max_obstruction_attempts = DEFAULT_MAX_PAGE_OBSTRUCTION_RECOVERY_ATTEMPTS
            elif productive_attempt_count > 0:
                obstruction_attempt_count = page_attempt_count
                max_obstruction_attempts = (
                    DEFAULT_MAX_PRODUCTIVE_OBSTRUCTION_RECOVERY_ATTEMPTS
                )
            else:
                obstruction_attempt_count = same_target_attempt_count
                max_obstruction_attempts = DEFAULT_MAX_OBSTRUCTION_RECOVERY_ATTEMPTS
            if obstruction_attempt_count >= max_obstruction_attempts:
                action.status = "failed"
                action.failure_reason = "page_obstruction_recovery_exhausted"
                action.failure_code = "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED"
                action.failure_message = (
                    f"同一页面遮挡恢复动作已达到 {max_obstruction_attempts} 次上限，"
                    "已停止重复恢复。"
                )
                action.failure_retryable = False
                if action.source_page_capture_id is not None:
                    self._fail_obstruction_recovery_chain(
                        page_capture_id=action.source_page_capture_id,
                        failure_stage="page_capture",
                        failure_code="PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED",
                        failure_message=action.failure_message,
                        retryable=False,
                    )
                return False
        action.mandatory = True
        action.blocks_page_inspection = blocks_page_inspection
        action.supersedes_source_page = supersedes_source_page
        action.requires_source_inspection_complete = requires_source_inspection_complete
        return True

    def _disable_supplemental_evidence_action(
        self, action: ExplorationActionMemory
    ) -> bool:
        """阻断旧工作记忆中的补采动作，并把来源页收口为证据不足。"""
        if not _is_disabled_supplemental_evidence_action(action):
            return False
        if action.status in {"candidate", "selected"}:
            action.status = "failed"
            action.failure_reason = "supplemental_evidence_disabled"
            action.failure_code = _SUPPLEMENTAL_EVIDENCE_DISABLED_CODE
            action.failure_message = "当前策略暂不执行补采，遗留补采动作已停止。"
            action.failure_retryable = False
            if action.source_page_capture_id is not None:
                self.mark_supplemental_evidence_insufficient(
                    page_capture_id=action.source_page_capture_id,
                    failure_stage="supplemental_evidence",
                    failure_message=action.failure_message,
                )
        return True

    def _disable_pending_supplemental_evidence_actions(self) -> None:
        """集中清理恢复或续跑时遗留的补采 Frontier。"""
        for action in self._exploration_actions.values():
            self._disable_supplemental_evidence_action(action)

    def mandatory_action_block_reason(self, exploration_action_id: str | None) -> str | None:
        """必执行动作依赖的页面质检尚未完成时返回阻断原因。"""
        action_id = _optional_text(exploration_action_id)
        action = self._exploration_actions.get(action_id) if action_id is not None else None
        if action is not None and self._disable_supplemental_evidence_action(action):
            return None
        if (
            action is None
            or not action.mandatory
            or not action.requires_source_inspection_complete
            or action.source_page_capture_id is None
        ):
            return None
        source = self._page_captures.get(action.source_page_capture_id)
        inspection = self._page_inspections.get(action.source_page_capture_id)
        if (
            action.resumes_page_exploration
            and inspection is not None
            and not inspection.pre_interaction_complete
        ):
            return "当前页面必须先完成交互前规则判定，之后才能执行关闭或跳过动作。"
        if (
            not action.resumes_page_exploration
            and source is not None
            and not source.inspection_terminal
        ):
            return "当前遮挡页面必须先完成候选规则执行，之后才能执行关闭或跳过动作。"
        return None

    def mandatory_page_action_capture_defaults(
        self, exploration_action_id: str | None
    ) -> dict[str, str | int | bool] | None:
        """返回必执行页面恢复动作需要绑定的旧采集与动作信息。"""
        action_id = _optional_text(exploration_action_id)
        action = self._exploration_actions.get(action_id) if action_id is not None else None
        if action is not None and self._disable_supplemental_evidence_action(action):
            return None
        if (
            action is None
            or not action.mandatory
            or action.status not in {"candidate", "selected"}
            or action.source_page_capture_id is None
        ):
            return None
        defaults: dict[str, str | int | bool] = {
            "action": action.action,
            "target": action.target or "same_session",
        }
        if action.navigation_depth is not None:
            defaults["navigation_depth"] = action.navigation_depth
        if action.supersedes_source_page:
            defaults["supersedes_page_capture_id"] = action.source_page_capture_id
        if self._should_clear_obstruction_chain(action):
            defaults["clear_obstructions"] = True
        return defaults

    def frontier_action_capture_defaults(
        self, exploration_action_id: str | None
    ) -> dict[str, str | int | bool] | None:
        """返回已绑定 Frontier 动作的浏览器边界参数。"""
        action_id = _optional_text(exploration_action_id)
        action = self._exploration_actions.get(action_id) if action_id is not None else None
        if action is not None and self._disable_supplemental_evidence_action(action):
            return None
        if action is None or action.status not in {"candidate", "selected"}:
            return None
        defaults: dict[str, str | int | bool] = {
            "action": action.action,
            "target": action.target or "same_session",
        }
        if action.navigation_depth is not None:
            defaults["navigation_depth"] = action.navigation_depth
        if action.supersedes_source_page and action.source_page_capture_id is not None:
            defaults["supersedes_page_capture_id"] = action.source_page_capture_id
        if self._should_clear_obstruction_chain(action):
            defaults["clear_obstructions"] = True
        return defaults

    def is_exploration_action_runnable(self, exploration_action_id: str | None) -> bool:
        """确认动作属于当前 Run、尚未终态且满足来源页面执行约束。"""
        action_id = _optional_text(exploration_action_id)
        action = self._exploration_actions.get(action_id) if action_id is not None else None
        if action is not None and self._disable_supplemental_evidence_action(action):
            return False
        return bool(
            action is not None
            and action.status in {"candidate", "selected"}
            and not _is_high_risk(action.risk_level)
            and self._is_exploration_action_ready(action)
        )

    def degrade_mandatory_action_source_pages(
        self, *, exploration_action_ids: list[str], failure_message: str
    ) -> list[str]:
        """隔离必经过渡页的规则工作，继续执行其关闭或观察动作。"""
        degraded_page_ids: list[str] = []
        for action_id in dict.fromkeys(exploration_action_ids):
            action = self._exploration_actions.get(action_id)
            if (
                action is None
                or not action.mandatory
                or not action.requires_source_inspection_complete
                or action.source_page_capture_id is None
            ):
                continue
            page_capture_id = action.source_page_capture_id
            terminalized = self.degrade_page_inspection(
                page_capture_id=page_capture_id,
                failure_stage="rule_execution",
                failure_code="RULE_EXECUTION_ABORTED_FOR_PAGE_CONTINUATION",
                failure_message=failure_message,
                retryable=False,
                missing_rule_status="evidence_insufficient",
            )
            if not terminalized:
                # 模型可能在候选规则查询前反复提前返回。此时没有已开始的
                # PageInspectionMemory 可降级，但必须释放开屏广告等过渡动作；
                # Worker 收尾会为该已识别业务页补齐页面绑定的规则 error 结果。
                terminalized = self.fail_page_inspection(
                    page_capture_id=page_capture_id,
                    failure_stage="rule_matching",
                    failure_code="RULE_MATCHING_ABORTED_FOR_PAGE_CONTINUATION",
                    failure_message=failure_message,
                    retryable=False,
                    missing_rule_status="evidence_insufficient",
                )
            if terminalized:
                degraded_page_ids.append(action.source_page_capture_id)
        return degraded_page_ids

    def blocked_mandatory_transition_action_ids(self) -> list[str]:
        """返回因来源页规则未收敛而阻塞的必执行过渡动作。"""
        self._disable_pending_supplemental_evidence_actions()
        action_ids: list[str] = []
        for action in self._exploration_actions.values():
            if (
                action.status not in {"candidate", "selected"}
                or not action.mandatory
                or not action.requires_source_inspection_complete
                or action.source_page_capture_id is None
            ):
                continue
            source = self._page_captures.get(action.source_page_capture_id)
            if source is not None and not source.inspection_terminal:
                action_ids.append(action.exploration_action_id)
        return action_ids

    def _same_target_obstruction_attempt_count(self, candidate: ExplorationActionMemory) -> int:
        """统计替代链上选择器与用户可见遮挡均未变化的恢复次数。"""
        lineage = self._page_capture_lineage_ids(candidate.source_page_capture_id)
        candidate_capture = self._page_captures.get(candidate.source_page_capture_id or "")
        return sum(
            1
            for action in self._exploration_actions.values()
            if action is not candidate
            and action.source_page_capture_id in lineage
            and action.target == candidate.target
            and self._obstruction_capture_unchanged(
                candidate_capture,
                self._page_captures.get(action.source_page_capture_id or ""),
            )
            and not self._obstruction_action_had_productive_result(action)
            and action.status in {"explored", "failed"}
            and action.mandatory
            and (action.blocks_page_inspection or action.requires_source_inspection_complete)
        )

    @staticmethod
    def _obstruction_capture_unchanged(
        candidate: PageCaptureMemory | None,
        previous: PageCaptureMemory | None,
    ) -> bool:
        if candidate is None or previous is None:
            return False
        if candidate.visible_text_fingerprint is not None:
            return candidate.visible_text_fingerprint == previous.visible_text_fingerprint
        if candidate.content_fingerprint is None:
            return True
        return candidate.content_fingerprint == previous.content_fingerprint

    def _obstruction_action_had_productive_result(self, action: ExplorationActionMemory) -> bool:
        result = self._page_captures.get(action.result_page_capture_id or "")
        return bool(result is not None and result.obstruction_dismissal_count > 0)

    def _productive_obstruction_recovery_attempt_count(
        self,
        candidate: ExplorationActionMemory,
    ) -> int:
        lineage = self._page_capture_lineage_ids(candidate.source_page_capture_id)
        return sum(
            1
            for action in self._exploration_actions.values()
            if action is not candidate
            and action.source_page_capture_id in lineage
            and action.action == "click"
            and action.status == "explored"
            and action.mandatory
            and (action.blocks_page_inspection or action.requires_source_inspection_complete)
            and self._obstruction_action_had_productive_result(action)
        )

    def _page_obstruction_recovery_attempt_count(
        self,
        candidate: ExplorationActionMemory,
        *,
        include_candidate: bool = False,
    ) -> int:
        """按同一页面替代链统计弹窗/遮挡关闭动作总次数。"""
        lineage = self._page_capture_lineage_ids(candidate.source_page_capture_id)
        return sum(
            1
            for action in self._exploration_actions.values()
            if (include_candidate or action is not candidate)
            and action.source_page_capture_id in lineage
            and action.action == "click"
            and action.status in {"explored", "failed"}
            and action.mandatory
            and (action.blocks_page_inspection or action.requires_source_inspection_complete)
        )

    def _is_page_obstruction_recovery_context(self, candidate: ExplorationActionMemory) -> bool:
        """识别需要使用页面级预算的普通业务页弹窗/遮挡恢复上下文。"""
        capture = self._page_captures.get(candidate.source_page_capture_id or "")
        if not self._is_obstruction_page_capture(capture):
            return False
        page_type = (
            _normalized_semantic_text(capture.page_detection.page_type)
            if capture is not None and capture.page_detection is not None
            else None
        )
        return page_type not in {"lineloading", "startupad"}

    def _is_obstruction_page_capture(self, capture: PageCaptureMemory | None) -> bool:
        if capture is None or capture.page_detection is None:
            return False
        page_state = _normalized_semantic_text(capture.page_detection.page_state)
        return page_state in _OBSTRUCTION_PAGE_STATES

    def _page_capture_lineage_ids(self, page_capture_id: str | None) -> set[str]:
        lineage: set[str] = set()
        capture = self._page_captures.get(page_capture_id or "")
        while capture is not None and capture.page_capture_id not in lineage:
            lineage.add(capture.page_capture_id)
            previous_id = capture.supersedes_page_capture_id
            capture = self._page_captures.get(previous_id) if previous_id else None
        return lineage

    def degrade_stalled_rule_pages(self, *, failure_message: str) -> list[str]:
        """隔离已开始规则匹配但连续无进展的活跃页面，不影响其他 Frontier。"""
        degraded_page_ids: list[str] = []
        for page_capture_id, capture in self._page_captures.items():
            if (
                capture.lifecycle_status != "active"
                or capture.inspection_terminal
                or page_capture_id not in self._page_inspections
            ):
                continue
            if self.degrade_page_inspection(
                page_capture_id=page_capture_id,
                failure_stage="rule_execution",
                failure_code="RULE_EXECUTION_BUDGET_EXHAUSTED",
                failure_message=failure_message,
                retryable=False,
                missing_rule_status="evidence_insufficient",
            ):
                degraded_page_ids.append(page_capture_id)
        return degraded_page_ids

    def fail_unstarted_rule_matching_pages(self, *, failure_message: str) -> list[str]:
        """终结已识别但模型未启动候选规则查询的普通业务页。

        页面已被识别为可质检业务页时，模型在候选规则查询前空转同样是单页
        规则异常。必须先将该页转为可审计终态，再继续消费与它无依赖关系的
        动态 Frontier，不能把整条探索链直接升级为全局不完整。
        """
        failed_page_ids: list[str] = []
        for page_capture_id, capture in self._page_captures.items():
            detection = capture.page_detection
            if (
                capture.lifecycle_status != "active"
                or capture.inspection_terminal
                or detection is None
                or detection.detection_status != "recognized"
                or not detection.rule_matching_allowed
                or page_capture_id in self._page_inspections
            ):
                continue
            if self.fail_page_inspection(
                page_capture_id=page_capture_id,
                failure_stage="rule_matching",
                failure_code="RULE_MATCHING_ABORTED_FOR_PAGE_CONTINUATION",
                failure_message=failure_message,
                retryable=False,
                missing_rule_status="evidence_insufficient",
            ):
                failed_page_ids.append(page_capture_id)
        return failed_page_ids

    def register_rule_write_rejection(self, *, page_capture_id: str) -> int:
        """累计当前页面的规则写回拒绝次数，用于及时结束模型内层重复提交。"""
        if page_capture_id not in self._page_captures:
            return 0
        count = self._rule_write_rejection_counts.get(page_capture_id, 0) + 1
        self._rule_write_rejection_counts[page_capture_id] = count
        return count

    def resolve_mandatory_page_action(self, *, action: str | None) -> str | None:
        """在唯一匹配时补齐模型遗漏的必执行动作 ID。"""
        self._disable_pending_supplemental_evidence_actions()
        action_name = _optional_text(action)
        if action_name is None:
            return None
        matches = [
            item.exploration_action_id
            for item in self._exploration_actions.values()
            if item.mandatory
            and item.status in {"candidate", "selected"}
            and item.action == action_name
        ]
        return matches[0] if len(matches) == 1 else None

    def resolve_unique_frontier_page_action(self, *, action: str | None) -> str | None:
        """在唯一可执行 Frontier 时补齐模型遗漏的动作 ID。"""
        self._disable_pending_supplemental_evidence_actions()
        action_name = _optional_text(action)
        selected = [
            item.exploration_action_id
            for item in self._exploration_actions.values()
            if item.status == "selected"
        ]
        if len(selected) == 1:
            # 外层监督器一次只派发一个动作。即使模型漏传动作 ID，甚至把
            # mode/trigger_action 写错，后续中间件也会按已选 Frontier 的结构化
            # 参数覆盖，避免重新把队列推进权交回自然语言推断。
            return selected[0]
        if action_name is None:
            return None
        matches = [
            item.exploration_action_id
            for item in self._exploration_actions.values()
            if item.status in {"candidate", "selected"} and item.action == action_name
        ]
        return matches[0] if len(matches) == 1 else None

    def prepare_next_exploration_action(
        self, *, blocking_page_capture_id: str | None = None
    ) -> dict[str, Any] | None:
        """选择一个可执行 Frontier，供外层监督器确定性派发。

        ``blocking_page_capture_id`` 用于遮挡恢复：当前页尚未质检完成时，仅允许
        执行该页的必执行遮挡动作。普通业务导航必须等待来源页规则进入终态，确保
        “先完成当前页质检，再探索下一页”的页面级顺序。页面数量仍由运行中动态
        发现决定，本方法不预建静态页面清单。
        """
        self._disable_pending_supplemental_evidence_actions()
        latest_capture_id = self._latest_browser_page_capture_id()
        latest_capture = (
            self._page_captures.get(latest_capture_id) if latest_capture_id is not None else None
        )
        candidates: list[ExplorationActionMemory] = []
        for action in self._exploration_actions.values():
            if action.status not in {"candidate", "selected"}:
                continue
            if _is_high_risk(action.risk_level):
                _block_high_risk_action(action)
                continue
            if blocking_page_capture_id is not None:
                if not (
                    action.source_page_capture_id == blocking_page_capture_id
                    and action.mandatory
                    and action.blocks_page_inspection
                    and self._is_exploration_action_ready(action)
                ):
                    continue
            elif not self._is_exploration_action_ready(action):
                continue
            if blocking_page_capture_id is None and _is_stale_local_page_action(
                action,
                latest_capture_id=latest_capture_id,
            ):
                if (
                    latest_capture is not None
                    and latest_capture.page_detection is not None
                    and _is_same_page_type_tab_navigation_action(action)
                ):
                    action.status = "skipped"
                    action.result_page_capture_id = latest_capture_id
                    action.failure_reason = "stale_local_page_action"
                    continue
            candidates.append(action)
        if not candidates:
            return None

        selected = next(
            (
                item
                for item in candidates
                if item.status == "selected"
                and item.source_page_capture_id in {None, latest_capture_id}
            ),
            None,
        )
        mandatory = next(
            (item for item in candidates if item.mandatory),
            None,
        )
        coverage_gap_navigation = min(
            (
                item
                for item in candidates
                if item.source_page_capture_id == latest_capture_id
                and item.action.strip().casefold() in {"click", "hover", "goto"}
                and _coverage_gap_page_type_priority(item) is not None
                and not self._has_observed_page_type(item.expected_next_page_type or "")
                and not _is_high_risk(item.risk_level)
                and not _is_global_navigation_action(
                    purpose=item.purpose,
                    target_keywords=tuple(
                        keyword
                        for raw_keyword in item.target_keywords
                        if (keyword := _normalized_semantic_text(raw_keyword)) is not None
                    ),
                )
            ),
            key=_coverage_gap_action_priority,
            default=None,
        )
        home_coverage_navigation = None
        if not self._has_explored_expected_page_type("home"):
            home_coverage_navigation = next(
                (
                    item
                    for item in candidates
                    if item.source_page_capture_id == latest_capture_id
                    and _is_missing_home_navigation_action(item)
                ),
                None,
            )
        bottom_navigation = min(
            (
                item
                for item in candidates
                if item.source_page_capture_id == latest_capture_id
                and _is_global_bottom_navigation_action(
                    purpose=item.purpose,
                    target_keywords=tuple(
                        keyword
                        for raw_keyword in item.target_keywords
                        if (keyword := _normalized_semantic_text(raw_keyword)) is not None
                    ),
                )
            ),
            key=lambda item: (
                0 if self._is_rule_target_action(item) else 1,
                *_bottom_navigation_priority(item),
            ),
            default=None,
        )
        structured_local_navigation = next(
            (
                item
                for item in candidates
                if item.source_page_capture_id == latest_capture_id
                and (
                    _is_nested_content_action(
                        purpose=item.purpose,
                        target_keywords=tuple(
                            keyword
                            for raw_keyword in item.target_keywords
                            if (keyword := _normalized_semantic_text(raw_keyword)) is not None
                        ),
                    )
                    or any(
                        _normalized_semantic_text(keyword) == "nestedreturn"
                        for keyword in item.target_keywords
                    )
                )
            ),
            None,
        )
        page_local_navigation = next(
            (
                item
                for item in candidates
                if item.source_page_capture_id == latest_capture_id
                and _is_page_local_navigation_action(item)
            ),
            None,
        )
        global_navigation = next(
            (
                item
                for item in candidates
                if self._is_rule_target_action(item)
                and _is_global_navigation_action(
                    purpose=item.purpose,
                    target_keywords=tuple(
                        keyword
                        for raw_keyword in item.target_keywords
                        if (keyword := _normalized_semantic_text(raw_keyword)) is not None
                    ),
                )
            ),
            None,
        )
        if global_navigation is None:
            global_navigation = next(
                (
                    item
                    for item in candidates
                    if _is_global_navigation_action(
                        purpose=item.purpose,
                        target_keywords=tuple(
                            keyword
                            for raw_keyword in item.target_keywords
                            if (keyword := _normalized_semantic_text(raw_keyword)) is not None
                        ),
                    )
                ),
                None,
            )
        current_page_action = next(
            (
                item
                for item in candidates
                if item.source_page_capture_id in {None, latest_capture_id}
                or item.action.strip().casefold() == "goto"
            ),
            None,
        )
        targeted_local_page_action = next(
            (
                item
                for item in candidates
                if item.source_page_capture_id == latest_capture_id
                and not self._is_repeated_page_tab_attempt(item)
                and not _is_global_navigation_action(
                    purpose=item.purpose,
                    target_keywords=tuple(
                        keyword
                        for raw_keyword in item.target_keywords
                        if (keyword := _normalized_semantic_text(raw_keyword)) is not None
                    ),
                )
                and self._is_rule_target_action(item)
            ),
            None,
        )
        local_page_action = next(
            (
                item
                for item in candidates
                if item.source_page_capture_id == latest_capture_id
                and not self._is_repeated_page_tab_attempt(item)
                and not _is_global_navigation_action(
                    purpose=item.purpose,
                    target_keywords=tuple(
                        keyword
                        for raw_keyword in item.target_keywords
                        if (keyword := _normalized_semantic_text(raw_keyword)) is not None
                    ),
                )
            ),
            None,
        )
        targeted_global_navigation = next(
            (
                item
                for item in (bottom_navigation, global_navigation)
                if item is not None and self._is_rule_target_action(item)
            ),
            None,
        )
        # 结构化详情/返回先闭环；若页内 Tab 因此成为旧 selector，下次调度会
        # 将它收口为覆盖缺口，禁止再通过整页 goto 重放开屏与弹窗流程。
        chosen = (
            selected
            or mandatory
            or coverage_gap_navigation
            or structured_local_navigation
            or page_local_navigation
            or targeted_local_page_action
            or targeted_global_navigation
            or local_page_action
            or home_coverage_navigation
            or bottom_navigation
            or global_navigation
            or current_page_action
        )
        if chosen is None:
            chosen = self._ensure_source_restore_action(
                candidates=candidates,
                latest_capture_id=latest_capture_id,
            )
        if chosen is None:
            return None
        if chosen.status == "candidate":
            # selected 表示已由监督器派发；实际尝试次数仍在 page_capture_tool
            # 进入中间件时累计，避免仅构造续跑消息就虚增 attempt_count。
            chosen.status = "selected"
        payload = chosen.to_dict()
        if self._should_clear_obstruction_chain(chosen):
            payload["clear_obstructions"] = True
        return payload

    def _rebind_stale_action_to_latest_stable_source(
        self,
        *,
        action: ExplorationActionMemory,
        latest_capture_id: str | None,
    ) -> bool:
        """当前浏览器已回到同一稳定业务页时，直接把旧 selector 重绑到最新 DOM。"""
        source = self._page_captures.get(action.source_page_capture_id or "")
        latest = self._page_captures.get(latest_capture_id or "")
        detection = latest.page_detection if latest is not None else None
        if source is None or latest is None or detection is None:
            return False
        expected_page_type = _normalized_semantic_text(action.source_page_type)

        if expected_page_type is None and source.page_detection is not None:
            expected_page_type = _normalized_semantic_text(source.page_detection.page_type)
        if (
            _normalized_semantic_text(detection.detection_status) != "recognized"
            or expected_page_type is None
            or _normalized_semantic_text(detection.page_type) != expected_page_type
            or _normalized_semantic_text(detection.page_state) not in _STABLE_SOURCE_PAGE_STATES
            or _normalized_page_identity(source.page_url) is None
            or _normalized_page_identity(source.page_url)
            != _normalized_page_identity(latest.page_url)
        ):
            return False
        action.status = "candidate"
        action.source_page_capture_id = latest.page_capture_id
        action.source_page_type = detection.page_type
        action.failure_reason = None
        action.failure_code = None
        action.failure_message = None
        action.failure_retryable = None
        return True

    def _ensure_source_restore_action(
        self,
        *,
        candidates: list[ExplorationActionMemory],
        latest_capture_id: str | None,
    ) -> ExplorationActionMemory | None:
        """旧页面 selector 执行前先恢复来源 URL，再绑定到新鲜页面采集。"""
        stale_action = None
        for item in candidates:
            if item.action.strip().casefold() not in {
                "click",
                "hover",
            } or item.source_page_capture_id in {None, latest_capture_id}:
                continue
            if _is_page_tab_navigation_action(item) and not item.mandatory:
                # 页内 Tab 本应在离开当前页面前完成，即使点击后会被识别为另一
                # 页面类型。异常遗留时若再用
                # goto 恢复来源，会整页重载 SPA，并重新触发开屏广告/初始化流程。
                # 将其明确记为覆盖缺口，比把整次巡检拉回入口重新开始更可控。
                item.status = "failed"
                item.result_page_capture_id = latest_capture_id
                item.failure_reason = "local_page_action_source_left"
                item.failure_code = "LOCAL_PAGE_ACTION_SOURCE_LEFT"
                item.failure_message = (
                    "页内 Tab 未在离开来源页前执行，已停止通过整页 goto "
                    "恢复旧 selector，避免应用重新触发开屏与初始化流程。"
                )
                item.failure_retryable = False
                continue
            stale_action = item
            break
        if stale_action is None or stale_action.source_page_capture_id is None:
            return None
        source = self._page_captures.get(stale_action.source_page_capture_id)
        if source is None or not source.page_url:
            stale_action.status = "skipped"
            stale_action.failure_reason = "source_page_state_unrestorable"
            stale_action.failure_code = "SOURCE_PAGE_STATE_UNRESTORABLE"
            return None
        if self._rebind_stale_action_to_latest_stable_source(
            action=stale_action,
            latest_capture_id=latest_capture_id,
        ):
            return stale_action
        marker = f"{_RESTORE_SOURCE_ACTION_PREFIX}{stale_action.exploration_action_id}"
        existing = next(
            (
                item
                for item in self._exploration_actions.values()
                if marker in item.target_keywords and item.status in {"candidate", "selected"}
            ),
            None,
        )
        if existing is not None:
            return existing
        restore_attempt_count = sum(
            marker in item.target_keywords for item in self._exploration_actions.values()
        )
        if restore_attempt_count >= DEFAULT_MAX_SOURCE_RESTORE_ATTEMPTS:
            stale_action.status = "failed"
            stale_action.failure_reason = "source_page_state_unrestorable"
            stale_action.failure_code = "SOURCE_ACTION_NOT_RESTORABLE"
            stale_action.failure_message = (
                f"来源页面连续恢复 {DEFAULT_MAX_SOURCE_RESTORE_ATTEMPTS} 次后"
                "仍未进入原页面稳定态，已停止重复回到入口。"
            )
            stale_action.failure_retryable = False
            return None
        self._exploration_sequence += 1
        action_id = f"exploration-{self._exploration_sequence}"
        history_depth = self._browser_history_navigation_depth_to_source(
            latest_capture_id=latest_capture_id,
            source_page_capture_id=stale_action.source_page_capture_id,
        )
        restore_via_history = history_depth is not None
        restore = ExplorationActionMemory(
            exploration_action_id=action_id,
            source_page_capture_id=latest_capture_id,
            source_page_type=None,
            action="back" if restore_via_history else "goto",
            origin="recovery",
            target="browser_history" if restore_via_history else source.page_url,
            target_keywords=[
                marker,
                *(["source_restore_history"] if restore_via_history else []),
            ],
            risk_level="low",
            purpose=marker,
            expected_next_page_type=stale_action.source_page_type,
            navigation_depth=(
                history_depth if restore_via_history else stale_action.navigation_depth
            ),
            mandatory=True,
        )
        self._exploration_actions[action_id] = restore
        return restore

    def _browser_history_navigation_depth_to_source(
        self,
        *,
        latest_capture_id: str | None,
        source_page_capture_id: str,
    ) -> int | None:
        """只信任当前页面到目标来源页的最近一条真实跨 URL 导航边。"""
        source = self._page_captures.get(source_page_capture_id)
        current = self._page_captures.get(latest_capture_id or "")
        if source is None or current is None or current.exploration_action_id is None:
            return None
        action = self._exploration_actions.get(current.exploration_action_id)
        if (
            action is None
            or action.status != "explored"
            or action.source_page_capture_id is None
            or not isinstance(action.navigation_depth, int)
            or isinstance(action.navigation_depth, bool)
            or action.navigation_depth <= 0
        ):
            return None
        ancestor = self._page_captures.get(action.source_page_capture_id)
        if ancestor is None:
            return None
        current_location = _normalized_page_location(current.page_url)
        ancestor_location = _normalized_page_location(ancestor.page_url)
        target_location = _normalized_page_location(source.page_url)
        target_page_type = (
            _normalized_semantic_text(source.page_detection.page_type)
            if source.page_detection is not None
            else None
        )
        ancestor_page_type = (
            _normalized_semantic_text(ancestor.page_detection.page_type)
            if ancestor.page_detection is not None
            else None
        )
        if (
            current_location is None
            or ancestor_location is None
            or current_location == ancestor_location
            or ancestor_location != target_location
            or (target_page_type is not None and ancestor_page_type != target_page_type)
        ):
            return None
        return action.navigation_depth

    def _pending_source_restore(
        self,
    ) -> tuple[ExplorationActionMemory, ExplorationActionMemory] | None:
        """返回最近一次尚未确认稳定落地的来源恢复及其原动作。"""
        for restore_action in reversed(tuple(self._exploration_actions.values())):
            original_id = _restore_source_original_action_id(restore_action)
            if (
                original_id is None
                or _RESTORE_SOURCE_READY_KEYWORD in restore_action.target_keywords
                or restore_action.status != "explored"
            ):
                continue
            original = self._exploration_actions.get(original_id)
            if original is not None and original.status in {"candidate", "selected"}:
                return restore_action, original
        return None

    def _has_observed_page_type(self, page_type: str) -> bool:
        """只把真实识别成功的页面计为已覆盖，不采信动作的预期落页。"""
        normalized_page_type = _normalized_semantic_text(page_type)
        if normalized_page_type is None:
            return False
        return any(
            capture.page_detection is not None
            and capture.page_detection.detection_status == "recognized"
            and _normalized_semantic_text(capture.page_detection.page_type) == normalized_page_type
            for capture in self._page_captures.values()
        )

    def _has_explored_expected_page_type(self, page_type: str) -> bool:
        """判断目标类型入口是否已尝试，用于避免同一首页入口循环点击。"""
        normalized_page_type = _normalized_semantic_text(page_type)
        if normalized_page_type is None:
            return False
        return any(
            action.status == "explored"
            and _normalized_semantic_text(action.expected_next_page_type) == normalized_page_type
            for action in self._exploration_actions.values()
        )

    def _is_repeated_page_tab_attempt(self, action: ExplorationActionMemory) -> bool:
        """识别已执行过、但没有成功进入目标类型的页内 Tab 重试。"""
        expected_page_type = _normalized_semantic_text(action.expected_next_page_type)
        if expected_page_type is None or not _is_page_tab_navigation_action(action):
            return False
        return any(
            other.exploration_action_id != action.exploration_action_id
            and other.status in {"explored", "failed", "skipped"}
            and _is_page_tab_navigation_action(other)
            and _normalized_semantic_text(other.expected_next_page_type) == expected_page_type
            for other in self._exploration_actions.values()
        )

    def _is_rule_target_action(self, action: ExplorationActionMemory) -> bool:
        """判断动作声明的目标页面是否由当前规则集覆盖。"""
        expected_page_type = _normalized_semantic_text(action.expected_next_page_type)
        return bool(
            expected_page_type is not None and expected_page_type in self._rule_target_page_types
        )

    def _latest_browser_page_capture_id(self) -> str | None:
        """返回当前浏览器会话最后一次成功采集。"""
        captures = list(self._page_captures.values())
        if not captures:
            return None
        return max(
            captures,
            key=lambda item: (
                item.page_seq if item.page_seq is not None else -1,
                item.page_capture_id,
            ),
        ).page_capture_id

    def fail_next_stalled_exploration_action(
        self, *, failure_code: str, failure_message: str
    ) -> str | None:
        """只隔离一个连续无进展动作，保留其余动态 Frontier。"""
        action = self.prepare_next_exploration_action()
        if action is None:
            return None
        action_id = str(action["exploration_action_id"])
        if not self.fail_exploration_action(
            exploration_action_id=action_id,
            failure_code=failure_code,
            failure_message=failure_message,
            retryable=False,
            failure_reason=failure_code.lower(),
        ):
            return None
        self.ensure_failed_obstruction_reobservation(exploration_action_id=action_id)
        return action_id

    def _is_exploration_action_ready(self, action: ExplorationActionMemory) -> bool:
        """判断动作是否满足来源页面终态约束。"""
        if _is_auxiliary_page_return_action(action) and action.result_page_capture_id is not None:
            return False
        source_id = action.source_page_capture_id
        if source_id is None:
            return True
        source = self._page_captures.get(source_id)
        if source is None or source.lifecycle_status != "active":
            return True
        if action.resumes_page_exploration:
            inspection = self._page_inspections.get(source_id)
            return inspection is None or inspection.pre_interaction_complete
        if action.mandatory and action.blocks_page_inspection:
            return True
        if source_id in self._background_snapshot_page_capture_ids:
            return True
        return source.inspection_terminal

    def has_runnable_frontier_for_closure(
        self,
        *,
        exploration_action_ids: list[str],
        close_all_pending_actions: bool,
        max_depth: int | None,
    ) -> bool:
        """判断收口目标中是否仍有未触及实际预算的低风险 Frontier。"""
        self._disable_pending_supplemental_evidence_actions()
        pending_statuses = {"candidate", "selected"}
        requested_action_ids = (
            [
                action_id
                for action_id, action in self._exploration_actions.items()
                if action.status in pending_statuses
            ]
            if close_all_pending_actions
            else exploration_action_ids
        )
        for action_id in dict.fromkeys(requested_action_ids):
            action = self._exploration_actions.get(action_id)
            if action is None or action.status not in pending_statuses:
                continue
            if action.risk_level in {"high", "critical"}:
                continue
            if max_depth is not None and (
                action.navigation_depth is not None and action.navigation_depth > max_depth
            ):
                continue
            return True
        return False

    def ensure_entry_exploration_action(
        self,
        *,
        action: str,
        target: str | None,
        navigation_depth: int | None,
    ) -> str:
        """为尚无来源页面的入口导航建立可审计动作。"""
        action_id = self.seed_entry_exploration_action(
            action=action,
            target=target,
            navigation_depth=navigation_depth,
        )
        self.mark_exploration_action_selected(action_id)
        return action_id

    def seed_entry_exploration_action(
        self,
        *,
        action: str,
        target: str | None,
        navigation_depth: int | None,
    ) -> str:
        """预建入口 Frontier，但在真正调用页面采集前不累计尝试次数。"""
        existing = next(
            (
                item
                for item in self._exploration_actions.values()
                if item.origin == "entry" and item.status in {"candidate", "selected"}
            ),
            None,
        )
        if existing is None:
            self._exploration_sequence += 1
            action_id = f"exploration-{self._exploration_sequence}"
            existing = ExplorationActionMemory(
                exploration_action_id=action_id,
                source_page_capture_id=None,
                source_page_type=None,
                action=action,
                origin="entry",
                target=target,
                navigation_depth=navigation_depth,
            )
            self._exploration_actions[action_id] = existing
        return existing.exploration_action_id

    def begin_page_capture_attempt(
        self,
        *,
        exploration_action_id: str | None,
        action: str,
        target: str | None,
        navigation_depth: int | None,
    ) -> str | None:
        """关联一次页面采集尝试；首个入口失败时也能保留动作终态。"""
        if exploration_action_id is not None:
            return (
                exploration_action_id
                if self.mark_exploration_action_selected(exploration_action_id)
                else None
            )
        if self._page_captures:
            return None
        return self.ensure_entry_exploration_action(
            action=action,
            target=target,
            navigation_depth=navigation_depth,
        )

    def mark_exploration_action_selected(self, exploration_action_id: str) -> bool:
        """把候选动作标记为已选中，并累计本次尝试。"""
        action = self._exploration_actions.get(exploration_action_id)
        if action is not None and self._disable_supplemental_evidence_action(action):
            return False
        if action is None or action.status not in {"candidate", "selected"}:
            return False
        action.status = "selected"
        action.attempt_count += 1
        return True

    def fail_exploration_action(
        self,
        *,
        exploration_action_id: str,
        failure_code: str,
        failure_message: str,
        retryable: bool,
        failure_reason: str | None = None,
    ) -> bool:
        """把一次无法继续的页面探索动作写入失败终态。"""
        action = self._exploration_actions.get(exploration_action_id)
        if action is None or action.status not in {"candidate", "selected"}:
            return False
        action.status = "failed"
        action.failure_reason = failure_reason or failure_code.lower()
        action.failure_code = failure_code
        action.failure_message = failure_message
        action.failure_retryable = retryable
        return True

    def ensure_failed_obstruction_reobservation(self, *, exploration_action_id: str) -> str | None:
        """关闭遮挡失败后强制重采复核，禁止直接在旧遮挡页面执行规则。"""
        failed_action = self._exploration_actions.get(exploration_action_id)
        if (
            failed_action is None
            or failed_action.status != "failed"
            or not failed_action.mandatory
            or not (
                failed_action.blocks_page_inspection
                or failed_action.requires_source_inspection_complete
            )
            or failed_action.source_page_capture_id is None
        ):
            return None
        if self._is_page_obstruction_recovery_context(failed_action):
            recovery_exhausted = (
                self._page_obstruction_recovery_attempt_count(
                    failed_action,
                    include_candidate=True,
                )
                >= DEFAULT_MAX_PAGE_OBSTRUCTION_RECOVERY_ATTEMPTS
            )
        else:
            recovery_exhausted = (
                self._obstruction_failure_attempt_count(failed_action.source_page_capture_id)
                >= DEFAULT_MAX_OBSTRUCTION_RECOVERY_ATTEMPTS
            )
        if recovery_exhausted:
            self.fail_page_inspection(
                page_capture_id=failed_action.source_page_capture_id,
                failure_stage="page_capture",
                failure_code="PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED",
                failure_message=("同一遮挡恢复链连续关闭失败，已停止重复点击并转人工复核。"),
                retryable=False,
            )
            return None
        source_capture = self._page_captures.get(failed_action.source_page_capture_id)
        session_crashed = (
            failed_action.failure_code == "PAGE_CAPTURE_SESSION_CRASHED"
            and source_capture is not None
            and bool(source_capture.page_url)
        )
        self._exploration_sequence += 1
        action_id = f"exploration-{self._exploration_sequence}"
        self._exploration_actions[action_id] = ExplorationActionMemory(
            exploration_action_id=action_id,
            source_page_capture_id=failed_action.source_page_capture_id,
            source_page_type=failed_action.source_page_type,
            action="goto" if session_crashed else "observe",
            target=source_capture.page_url if session_crashed else "same_session",
            risk_level="low",
            purpose=(
                "browser_session_recovery_after_crash"
                if session_crashed
                else "obstruction_reobservation_after_failed_dismissal"
            ),
            expected_next_page_type=failed_action.source_page_type,
            navigation_depth=failed_action.navigation_depth,
            mandatory=True,
            blocks_page_inspection=True,
            supersedes_source_page=True,
        )
        return action_id

    def _obstruction_failure_attempt_count(self, page_capture_id: str) -> int:
        """沿显式替代链统计遮挡关闭失败次数，避免失败重采形成无限循环。"""
        lineage: set[str] = set()
        capture = self._page_captures.get(page_capture_id)
        while capture is not None and capture.page_capture_id not in lineage:
            lineage.add(capture.page_capture_id)
            previous_id = capture.supersedes_page_capture_id
            capture = self._page_captures.get(previous_id) if previous_id else None
        return sum(
            1
            for action in self._exploration_actions.values()
            if action.source_page_capture_id in lineage
            and action.status == "failed"
            and action.mandatory
            and (action.blocks_page_inspection or action.requires_source_inspection_complete)
        )

    def mark_exploration_action_explored(
        self, *, exploration_action_id: str, result_page_capture_id: str
    ) -> bool:
        """把已绑定采集结果的探索动作标记为已探索。"""
        action = self._exploration_actions.get(exploration_action_id)
        if action is None or action.status not in {"candidate", "selected"}:
            return False
        action.status = "explored"
        action.result_page_capture_id = result_page_capture_id
        action.failure_reason = None
        action.failure_code = None
        action.failure_message = None
        action.failure_retryable = None
        return True

    def remember_page_type_assessments(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        assessments: list[Mapping[str, Any]],
        scope_complete: bool,
        scope_page_types: list[str] | tuple[str, ...] = (),
    ) -> int:
        """保存当前 Run 的产品业务页面类型适用性评估。"""
        built_assessments: list[PageTypeAssessment] = []
        for item in assessments:
            page_type = _optional_text(item.get("page_type"))
            status = _optional_text(item.get("status"))
            reason_code = _optional_text(item.get("reason_code"))
            if page_type is None or status is None or reason_code is None:
                continue
            page_capture_ids = _string_list(item.get("page_capture_ids"))
            page_states = _string_list(item.get("page_states")) or self._page_states_for_captures(
                page_capture_ids
            )
            built_assessments.append(
                build_page_type_assessment(
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    page_type=page_type,
                    status=PageTypeAssessmentStatus(status),
                    reason_code=reason_code,
                    page_capture_ids=page_capture_ids,
                    evidence_artifact_ids=_string_list(item.get("evidence_artifact_ids")),
                    exploration_action_ids=_string_list(item.get("exploration_action_ids")),
                    page_states=page_states,
                    assessed_at=datetime.now(UTC),
                )
            )
        assessment_set = build_page_type_assessment_set(
            task_id=task_id,
            agent_run_id=agent_run_id,
            assessments=built_assessments,
            assessment_scope_complete=scope_complete,
            required_page_types=tuple(scope_page_types),
        )
        self._page_type_assessments = {
            assessment.page_type: assessment.to_dict()
            for assessment in assessment_set.assessments
        }
        self._page_type_assessment_scope_complete = bool(
            assessment_set.assessment_scope_complete and self._page_type_assessments
        )
        self._page_type_assessment_scope_page_types = assessment_set.required_page_types
        return len(self._page_type_assessments)

    def observed_business_page_types(self) -> tuple[str, ...]:
        """返回当前 Run 已实际识别的业务页面类型，作为覆盖分母下限。"""

        registry = get_default_page_type_registry()
        observed = {
            detection.page_type
            for capture in self._page_captures.values()
            if (detection := capture.page_detection) is not None
            and detection.detection_status == "recognized"
            and detection.page_type in registry.coverage_required_codes
        }
        return tuple(
            page_type
            for page_type in registry.coverage_required_codes
            if page_type in observed
        )

    def _page_states_for_captures(self, page_capture_ids: list[str]) -> list[str]:
        """从当前 Run 已识别页面补齐页面状态，供页面类型评估合同留痕。"""
        page_states: list[str] = []
        for capture_id in page_capture_ids:
            capture = self._page_captures.get(capture_id)
            page_state = (
                capture.page_detection.page_state
                if capture is not None and capture.page_detection is not None
                else None
            )
            if page_state is not None and page_state not in page_states:
                page_states.append(page_state)
        return page_states

    def close_exploration_items(
        self,
        *,
        action_status: str,
        reason: str,
        exploration_action_ids: list[str],
        close_all_pending_actions: bool,
        page_capture_ids: list[str],
        abandon_all_pending_pages: bool,
    ) -> dict[str, list[str]]:
        """显式关闭不再执行的探索动作和未开始规则匹配的页面。"""
        pending_statuses = {"candidate", "selected"}
        requested_action_ids = (
            [
                action_id
                for action_id, action in self._exploration_actions.items()
                if action.status in pending_statuses
            ]
            if close_all_pending_actions
            else exploration_action_ids
        )
        closed_action_ids: list[str] = []
        unknown_action_ids: list[str] = []
        rejected_action_ids: list[str] = []
        for action_id in dict.fromkeys(requested_action_ids):
            action = self._exploration_actions.get(action_id)
            if action is None:
                unknown_action_ids.append(action_id)
                continue
            if action.status not in pending_statuses or action.mandatory:
                rejected_action_ids.append(action_id)
                continue
            action.status = action_status
            action.failure_reason = reason
            closed_action_ids.append(action_id)

        requested_page_ids = (
            [
                page_capture_id
                for page_capture_id in self._page_captures
                if self._is_active_capture(page_capture_id)
                and (
                    (inspection := self._page_inspections.get(page_capture_id)) is None
                    or not inspection.completed
                )
            ]
            if abandon_all_pending_pages
            else page_capture_ids
        )
        abandoned_page_capture_ids: list[str] = []
        unknown_page_capture_ids: list[str] = []
        rejected_page_capture_ids: list[str] = []
        for page_capture_id in dict.fromkeys(requested_page_ids):
            capture = self._page_captures.get(page_capture_id)
            if capture is None:
                unknown_page_capture_ids.append(page_capture_id)
                continue
            inspection = self._page_inspections.get(page_capture_id)
            if capture.lifecycle_status != "active" or (
                inspection is not None and not inspection.completed
            ):
                rejected_page_capture_ids.append(page_capture_id)
                continue
            if capture.page_detection is None:
                rejected_page_capture_ids.append(page_capture_id)
                continue
            if inspection is not None and inspection.completed:
                rejected_page_capture_ids.append(page_capture_id)
                continue
            if self._has_pending_mandatory_action(page_capture_id):
                rejected_page_capture_ids.append(page_capture_id)
                continue
            capture.lifecycle_status = "abandoned"
            capture.abandon_reason = reason
            abandoned_page_capture_ids.append(page_capture_id)
            if self._current_page_capture_id == page_capture_id:
                self._current_page_capture_id = None

        return {
            "closed_action_ids": closed_action_ids,
            "unknown_action_ids": unknown_action_ids,
            "rejected_action_ids": rejected_action_ids,
            "abandoned_page_capture_ids": abandoned_page_capture_ids,
            "unknown_page_capture_ids": unknown_page_capture_ids,
            "rejected_page_capture_ids": rejected_page_capture_ids,
        }

    def abandon_unmatchable_page(self, *, page_capture_id: str, reason: str) -> bool:
        """关闭已识别但不允许规则匹配的页面采集。"""
        result = self.close_exploration_items(
            action_status="skipped",
            reason=reason,
            exploration_action_ids=[],
            close_all_pending_actions=False,
            page_capture_ids=[page_capture_id],
            abandon_all_pending_pages=False,
        )
        return page_capture_id in result["abandoned_page_capture_ids"]

    def skip_authentication_required_page(
        self,
        *,
        page_capture_id: str,
        page_type: str | None,
    ) -> str | None:
        """跳过登录态门禁页，并为动态分支建立一次受控历史返回。"""
        capture = self._page_captures.get(page_capture_id)
        if (
            capture is None
            or capture.lifecycle_status != "active"
            or capture.page_detection is None
        ):
            return None

        capture.lifecycle_status = "abandoned"
        capture.abandon_reason = "authentication_required"
        if self._current_page_capture_id == page_capture_id:
            self._current_page_capture_id = None

        source_action = (
            self._exploration_actions.get(capture.exploration_action_id)
            if capture.exploration_action_id is not None
            else None
        )
        if (
            source_action is None
            or source_action.origin == "entry"
            or source_action.action.strip().lower() == "back"
        ):
            return None

        self._exploration_sequence += 1
        action_id = f"exploration-{self._exploration_sequence}"
        source_depth = source_action.navigation_depth
        self._exploration_actions[action_id] = ExplorationActionMemory(
            exploration_action_id=action_id,
            source_page_capture_id=page_capture_id,
            source_page_type=page_type,
            action="back",
            target="browser_history",
            target_keywords=["authentication_required_return"],
            risk_level="low",
            purpose="登录态验证页已跳过，返回上一业务页继续其他页面探索",
            expected_next_page_type=source_action.source_page_type,
            # page_capture_tool 校验的是“当前页面是否确有来源层级”，不是返回后
            # 的目标深度；至少保留 1，避免合法认证分支在浏览器执行前被误阻断。
            navigation_depth=(max(source_depth, 1) if source_depth is not None else 1),
            mandatory=True,
        )
        return action_id

    def abandon_revisited_page(self, *, page_capture_id: str, reason: str) -> bool:
        """关闭详情返回后的重复列表采集，保留其新发现 Frontier。"""
        abandoned = self.abandon_unmatchable_page(
            page_capture_id=page_capture_id,
            reason=reason,
        )
        if not abandoned:
            return False
        capture = self._page_captures.get(page_capture_id)
        if capture is not None:
            # 该采集与已质检来源页属于同一逻辑页面。将重复采集置为终态，后续为
            # 执行其新 Frontier 恢复来源时即可复用既有质检，禁止再跑整套规则。
            capture.inspection_status = PageInspectionStatus.COMPLETED.value
        return True

    def has_pending_exploration(self) -> bool:
        """判断当前 Run 是否仍有待探索动作。"""
        self._disable_pending_supplemental_evidence_actions()
        return any(
            action.status in {"candidate", "selected"}
            for action in self._exploration_actions.values()
        )

    def has_blocked_obstruction(self) -> bool:
        """判断是否有遮挡关闭动作被安全策略阻断。

        高风险业务动作本来就应当被阻断，不影响完整巡检收口；但遮挡恢复动作被
        阻断意味着底层页面从未暴露，不能把这种状态解释成正常完成。
        """
        return any(
            action.status == "blocked" and _is_obstruction_dismissal_action(action)
            for action in self._exploration_actions.values()
        )

    def has_runnable_work(self) -> bool:
        """判断是否仍有活跃页面或 Frontier 动作需要处理。"""
        return self.has_incomplete_page_inspection() or self.has_pending_exploration()

    def discovered_work_count(self) -> int:
        """返回已发现逻辑页面数；未识别首采可扩容，补采快照不得自增预算。"""
        logical_page_count = 0
        for capture in self._page_captures.values():
            action = (
                self._exploration_actions.get(capture.exploration_action_id)
                if capture.exploration_action_id is not None
                else None
            )
            is_same_logical_page_replacement = bool(
                action is not None
                and (
                    (
                        action.source_page_capture_id == capture.supersedes_page_capture_id
                        and (
                            action.action in {"scroll", "timed_capture"}
                            or _is_obstruction_dismissal_action(action)
                        )
                    )
                    or (
                        action.origin == "recovery"
                        and _restore_source_original_action_id(action) is not None
                    )
                )
            )
            logical_page_count += int(not is_same_logical_page_replacement)
        return logical_page_count

    def is_converged(self) -> bool:
        """所有已发现工作均已进入终态时返回 True。"""
        return not self.has_runnable_work()

    def terminalize_remaining_work(
        self,
        *,
        failure_stage: str,
        failure_code: str,
        failure_message: str,
    ) -> dict[str, int]:
        """由编排层把续跑后仍悬空的动作和页面统一写入终态。"""
        pending_action_ids = [
            action_id
            for action_id, action in self._exploration_actions.items()
            if action.status in {"candidate", "selected"}
        ]
        closed_action_count = sum(
            self.fail_exploration_action(
                exploration_action_id=action_id,
                failure_code=failure_code,
                failure_message=failure_message,
                retryable=False,
                failure_reason=failure_code.lower(),
            )
            for action_id in pending_action_ids
        )
        failed_pages = 0
        degraded_pages = 0
        pending_page_ids = [
            page_capture_id
            for page_capture_id, capture in self._page_captures.items()
            if capture.lifecycle_status == "active" and not capture.inspection_terminal
        ]
        for page_capture_id in pending_page_ids:
            if page_capture_id in self._page_inspections:
                changed = self.degrade_page_inspection(
                    page_capture_id=page_capture_id,
                    failure_stage=failure_stage,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    retryable=False,
                    missing_rule_status=(
                        "skipped" if failure_code in _CAPACITY_TERMINATION_CODES else "error"
                    ),
                )
                degraded_pages += int(changed)
            else:
                changed = self.fail_page_inspection(
                    page_capture_id=page_capture_id,
                    failure_stage=failure_stage,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    retryable=False,
                )
                failed_pages += int(changed)
        return {
            "closed_action_count": closed_action_count,
            "failed_page_count": failed_pages,
            "degraded_page_count": degraded_pages,
        }

    def requires_review(self) -> bool:
        """执行缺口或探索动作失败需要人工复核；业务质检 fail 不在此列。"""
        has_execution_gap = (
            any(
                capture.inspection_status
                in {PageInspectionStatus.DEGRADED.value, PageInspectionStatus.FAILED.value}
                for capture in self._page_captures.values()
            )
            or any(action.status == "failed" for action in self._exploration_actions.values())
            or self.has_blocked_obstruction()
        )
        has_effective_page_result = any(
            capture.lifecycle_status == "active" and capture.inspection_terminal
            for capture in self._page_captures.values()
        )
        zero_effective_coverage = bool(self._page_captures) and not has_effective_page_result
        return has_execution_gap or zero_effective_coverage

    def _has_pending_mandatory_action(self, page_capture_id: str) -> bool:
        return any(
            action.source_page_capture_id == page_capture_id
            and action.mandatory
            and action.status in {"candidate", "selected"}
            for action in self._exploration_actions.values()
        )

    def has_blocking_mandatory_page_action(self, page_capture_id: str) -> bool:
        """判断页面是否仍被必须先执行的遮挡恢复动作阻塞。"""
        return any(
            action.source_page_capture_id == page_capture_id
            and action.mandatory
            and action.blocks_page_inspection
            and action.status in {"candidate", "selected"}
            for action in self._exploration_actions.values()
        )

    def start_page_inspection(
        self,
        *,
        page_capture_id: str,
        page_type: str,
        candidate_rules: list[Mapping[str, Any]],
    ) -> bool:
        """为指定页面建立候选规则清单；页面不存在时返回 False。"""
        self._background_snapshot_page_capture_ids.discard(page_capture_id)
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.lifecycle_status != "active" or capture.inspection_terminal:
            return False
        normalized_rules: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidate_rules:
            rule_id = _optional_text(item.get("rule_id"))
            rule_code = _optional_text(item.get("rule_code"))
            key = _rule_key(rule_id, rule_code)
            if key is None or key in seen:
                continue
            seen.add(key)
            route = item.get("execution_route")
            route = route if isinstance(route, Mapping) else {}
            input_contract = (
                item.get("input_contract")
                if isinstance(item.get("input_contract"), Mapping)
                else {}
            )
            normalized_rules.append(
                {
                    "rule_id": rule_id,
                    "rule_code": rule_code,
                    "category": _optional_text(item.get("category")),
                    "strategy": _optional_text(route.get("strategy")),
                    "rule_engine_ready": route.get("rule_engine_ready") is True,
                    "judge_tool_name": _optional_text(
                        route.get("tool_name") or route.get("judge_tool_name")
                    ),
                    "route_blocked_reason": _optional_text(
                        route.get("blocked_reason") or route.get("route_blocked_reason")
                    ),
                    "session_dependency": (
                        _optional_text(route.get("session_dependency")) or "snapshot"
                    ),
                    "required_capture_roles": _string_list(
                        input_contract.get("required_capture_roles")
                    ),
                    "required_artifacts": _string_list(input_contract.get("required_artifacts")),
                    "requires_interaction_evidence": (_rule_requires_interaction_evidence(item)),
                }
            )
        inspection = self._page_inspections.get(page_capture_id)
        if inspection is None:
            inspection = PageInspectionMemory(
                capture=capture,
                page_type=page_type,
                candidate_rules=normalized_rules,
                evidence_capture_roles={"primary": page_capture_id},
            )
            self._page_inspections[page_capture_id] = inspection
        else:
            inspection.page_type = page_type
            inspection.candidate_rules = normalized_rules
            inspection.evidence_capture_roles = {"primary": page_capture_id}
            inspection.pending_rule_judgements.clear()
        capture.failure_stage = None
        capture.failure_code = None
        capture.failure_message = None
        capture.failure_retryable = None
        capture.inspection_status = PageInspectionStatus.EVALUATING.value
        inspection.refresh_completion()
        self._current_page_capture_id = None if inspection.completed else page_capture_id
        return True

    def record_rule_judgement(
        self,
        *,
        page_capture_id: str,
        judgement: Mapping[str, Any],
    ) -> bool:
        """登记已完成但尚未写回的确定性规则判定。"""
        inspection = self._page_inspections.get(page_capture_id)
        if inspection is None or inspection.completed:
            return False
        rule_id = _optional_text(judgement.get("rule_id"))
        rule_code = _optional_text(judgement.get("rule_code"))
        key = _matching_candidate_rule_key(
            inspection.candidate_rules,
            rule_id=rule_id,
            rule_code=rule_code,
        )
        execution_status = _optional_text(judgement.get("execution_status"))
        if key is None or execution_status is None or key in inspection.rule_results:
            return False
        inspection.pending_rule_judgements[key] = {
            "rule_id": rule_id,
            "rule_code": rule_code,
            "execution_status": execution_status,
        }
        return True

    def _fail_obstruction_recovery_chain(
        self,
        *,
        page_capture_id: str,
        failure_stage: str,
        failure_code: str,
        failure_message: str,
        retryable: bool,
    ) -> int:
        """遮挡恢复耗尽时终结同一业务地址上的活跃遮挡采集。"""
        source = self._page_captures.get(page_capture_id)
        source_url = _normalized_page_location(source.page_url) if source is not None else None
        source_page_type = (
            _normalized_semantic_text(source.page_detection.page_type)
            if source is not None and source.page_detection is not None
            else None
        )
        lineage_ids = self._page_capture_lineage_ids(page_capture_id)
        failed_count = 0
        for candidate_id, candidate in self._page_captures.items():
            if (
                candidate.lifecycle_status != "active"
                or candidate.inspection_terminal
            ):
                continue
            is_source_capture = candidate_id == page_capture_id
            if not is_source_capture and not self._is_obstruction_page_capture(candidate):
                continue
            candidate_url = (
                _normalized_page_location(candidate.page_url)
                if candidate.page_url is not None
                else None
            )
            candidate_page_type = (
                _normalized_semantic_text(candidate.page_detection.page_type)
                if candidate.page_detection is not None
                else None
            )
            same_obstruction_context = bool(
                is_source_capture
                or candidate_id in lineage_ids
                or (
                    source_url is not None
                    and candidate_url == source_url
                    and (
                        source_page_type is None
                        or candidate_page_type is None
                        or candidate_page_type == source_page_type
                    )
                )
            )
            if not same_obstruction_context:
                continue
            failed_count += int(
                self.fail_page_inspection(
                    page_capture_id=candidate_id,
                    failure_stage=failure_stage,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    retryable=retryable,
                )
            )
        return failed_count

    def fail_page_inspection(
        self,
        *,
        page_capture_id: str,
        failure_stage: str,
        failure_code: str,
        failure_message: str,
        retryable: bool,
        missing_rule_status: str = "error",
    ) -> bool:
        """终结当前页面，并为已知但未完成的候选规则补齐结构化错误。"""
        capture = self._page_captures.get(page_capture_id)
        if capture is None or capture.lifecycle_status != "active" or capture.inspection_terminal:
            return False
        inspection = self._page_inspections.get(page_capture_id)
        if inspection is not None:
            for item in inspection.candidate_rules:
                rule_id = _optional_text(item.get("rule_id"))
                rule_code = _optional_text(item.get("rule_code"))
                key = _rule_key(rule_id, rule_code)
                if key is None or key in inspection.rule_results:
                    continue
                inspection.rule_results[key] = RuleInspectionResultMemory(
                    execution_id=None,
                    rule_id=rule_id,
                    rule_code=rule_code,
                    execution_status=missing_rule_status,
                    result_summary="页面处理异常，规则未能完成判定。",
                    problem_desc=failure_message,
                )
            inspection.pending_rule_judgements.clear()
        capture.inspection_status = PageInspectionStatus.FAILED.value
        self._background_snapshot_page_capture_ids.discard(page_capture_id)
        capture.failure_stage = failure_stage
        capture.failure_code = failure_code
        capture.failure_message = failure_message
        capture.failure_retryable = retryable
        if self._current_page_capture_id == page_capture_id:
            self._current_page_capture_id = None
        return True

    def mark_supplemental_evidence_insufficient(
        self,
        *,
        page_capture_id: str,
        failure_stage: str,
        failure_message: str,
    ) -> bool:
        """将需要补采的页面按当前临时策略收口为证据不足。"""
        if page_capture_id in self._page_inspections:
            return self.degrade_page_inspection(
                page_capture_id=page_capture_id,
                failure_stage=failure_stage,
                failure_code=_SUPPLEMENTAL_EVIDENCE_DISABLED_CODE,
                failure_message=failure_message,
                retryable=False,
                missing_rule_status="evidence_insufficient",
            )
        capture = self._page_captures.get(page_capture_id)
        if (
            capture is None
            or capture.lifecycle_status != "active"
            or capture.inspection_terminal
        ):
            return False
        capture.inspection_status = PageInspectionStatus.DEGRADED.value
        capture.failure_stage = failure_stage
        capture.failure_code = _SUPPLEMENTAL_EVIDENCE_DISABLED_CODE
        capture.failure_message = failure_message
        capture.failure_retryable = False
        if self._current_page_capture_id == page_capture_id:
            self._current_page_capture_id = None
        return True

    def degrade_page_inspection(
        self,
        *,
        page_capture_id: str,
        failure_stage: str,
        failure_code: str,
        failure_message: str,
        retryable: bool,
        missing_rule_status: str = "error",
    ) -> bool:
        """以降级终态收口已开始规则匹配的页面。"""
        capture = self._page_captures.get(page_capture_id)
        inspection = self._page_inspections.get(page_capture_id)
        if (
            capture is None
            or inspection is None
            or capture.lifecycle_status != "active"
            or capture.inspection_terminal
        ):
            return False
        for item in inspection.candidate_rules:
            rule_id = _optional_text(item.get("rule_id"))
            rule_code = _optional_text(item.get("rule_code"))
            key = _rule_key(rule_id, rule_code)
            if key is None or key in inspection.rule_results:
                continue
            inspection.rule_results[key] = RuleInspectionResultMemory(
                execution_id=None,
                rule_id=rule_id,
                rule_code=rule_code,
                execution_status=missing_rule_status,
                result_summary="页面已降级完成，规则未能完成判定。",
                problem_desc=failure_message,
            )
        inspection.pending_rule_judgements.clear()
        capture.inspection_status = PageInspectionStatus.DEGRADED.value
        self._background_snapshot_page_capture_ids.discard(page_capture_id)
        capture.failure_stage = failure_stage
        capture.failure_code = failure_code
        capture.failure_message = failure_message
        capture.failure_retryable = retryable
        if self._current_page_capture_id == page_capture_id:
            self._current_page_capture_id = None
        return True

    def record_page_rule_result(
        self,
        *,
        page_capture_id: str,
        result: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> bool:
        """把一次成功规则写回同步到对应页面；页面尚未匹配规则时返回 False。"""
        inspection = self._page_inspections.get(page_capture_id)
        if inspection is None or inspection.completed:
            return False
        rule_id = _optional_text(result.get("rule_id") or arguments.get("rule_id"))
        rule_code = _optional_text(result.get("rule_code") or arguments.get("rule_code"))
        key = _matching_candidate_rule_key(
            inspection.candidate_rules,
            rule_id=rule_id,
            rule_code=rule_code,
        )
        execution_status = _optional_text(
            result.get("execution_status") or arguments.get("execution_status")
        )
        if key is None or execution_status is None:
            return False
        candidate = next(
            (
                item
                for item in inspection.candidate_rules
                if _rule_key(
                    _optional_text(item.get("rule_id")),
                    _optional_text(item.get("rule_code")),
                )
                == key
            ),
            {},
        )
        canonical_rule_id = _optional_text(candidate.get("rule_id")) or rule_id
        canonical_rule_code = _optional_text(candidate.get("rule_code")) or rule_code
        inspection.rule_results[key] = RuleInspectionResultMemory(
            execution_id=_optional_text(result.get("execution_id")),
            rule_id=canonical_rule_id,
            rule_code=canonical_rule_code,
            execution_status=execution_status,
            result_summary=_optional_text(arguments.get("result_summary")),
            problem_desc=_optional_text(arguments.get("problem_desc")),
            confidence=_optional_float(arguments.get("confidence")),
            severity=_optional_text(arguments.get("severity")),
            evidence_artifact_ids=_string_list(arguments.get("evidence_artifact_ids")),
        )
        inspection.pending_rule_judgements.pop(key, None)
        inspection.refresh_completion()
        if inspection.completed:
            self._background_snapshot_page_capture_ids.discard(page_capture_id)
            if self._current_page_capture_id == page_capture_id:
                self._current_page_capture_id = None
        elif page_capture_id not in self._background_snapshot_page_capture_ids:
            self._current_page_capture_id = page_capture_id
        return True

    def page_rule_result(
        self,
        *,
        page_capture_id: str,
        rule_id: str | None,
        rule_code: str | None,
    ) -> dict[str, Any] | None:
        """返回候选规则已形成的终态，供 Tool 执行前阻断重复判定。"""
        inspection = self._page_inspections.get(page_capture_id)
        if inspection is None:
            return None
        key = _matching_candidate_rule_key(
            inspection.candidate_rules,
            rule_id=_optional_text(rule_id),
            rule_code=_optional_text(rule_code),
        )
        result = inspection.rule_results.get(key) if key is not None else None
        return result.to_dict(compact=True) if result is not None else None

    def snapshot(self, *, compact: bool) -> dict[str, Any]:
        """返回模型可见或审计使用的页面质检快照。"""
        current_page = self._current_page(compact=compact)
        completed_pages = [
            item.to_dict(current=False, compact=compact)
            for item in self._page_inspections.values()
            if item.completed
        ]
        terminal_pages = [
            self._page_snapshot(capture_id, current=False, compact=compact)
            for capture_id, capture in self._page_captures.items()
            if self._is_active_capture(capture_id) and capture.inspection_terminal
        ]
        pending_pages = [
            self._page_snapshot(capture_id, current=False, compact=compact)
            for capture_id in self._page_captures
            if capture_id != self._current_page_capture_id
            and self._is_active_capture(capture_id)
            and not self._page_captures[capture_id].inspection_terminal
        ]
        inactive_pages = [
            {
                **capture.to_dict(),
                "page_type": (
                    capture.page_detection.page_type if capture.page_detection is not None else None
                ),
                "status": capture.lifecycle_status,
                "candidate_rule_count": 0,
                "completed_rule_count": 0,
                "candidate_rules": [],
                "result": {
                    "overall_status": capture.lifecycle_status,
                    "status_counts": {},
                    "rules": [],
                },
            }
            for capture in self._page_captures.values()
            if capture.lifecycle_status != "active"
        ]
        return {
            "schema_version": 2,
            "page_type_assessments": list(self._page_type_assessments.values()),
            "page_type_assessment_scope_complete": (self._page_type_assessment_scope_complete),
            "page_type_assessment_scope_page_types": list(
                self._page_type_assessment_scope_page_types
            ),
            "current_page": current_page,
            "inspected_pages": completed_pages,
            "terminal_pages": terminal_pages,
            "pending_pages": pending_pages,
            "inactive_pages": inactive_pages,
            "exploration_frontier": self._exploration_frontier_snapshot(),
            "inspected_page_count": len(completed_pages),
            "terminal_page_count": len(terminal_pages),
            "degraded_page_count": sum(
                page is not None
                and page.get("inspection_status") == PageInspectionStatus.DEGRADED.value
                for page in terminal_pages
            ),
            "failed_page_count": sum(
                page is not None
                and page.get("inspection_status") == PageInspectionStatus.FAILED.value
                for page in terminal_pages
            ),
            "pending_page_count": len(pending_pages),
            "inactive_page_count": len(inactive_pages),
        }

    def _exploration_frontier_snapshot(self) -> dict[str, Any]:
        pending = [
            action.to_dict()
            for action in self._exploration_actions.values()
            if action.status in {"candidate", "selected"}
        ]
        explored = [
            action.to_dict()
            for action in self._exploration_actions.values()
            if action.status == "explored"
        ]
        inactive = [
            action.to_dict()
            for action in self._exploration_actions.values()
            if action.status in {"blocked", "skipped", "failed"}
        ]
        return {
            "pending_actions": pending,
            "explored_actions": explored,
            "inactive_actions": inactive,
            "pending_action_count": len(pending),
            "explored_action_count": len(explored),
            "inactive_action_count": len(inactive),
        }

    def _current_page(self, *, compact: bool) -> dict[str, Any] | None:
        capture_id = self._current_page_capture_id
        if (
            capture_id is None
            or not self._is_active_capture(capture_id)
            or self._page_captures[capture_id].inspection_terminal
        ):
            return None
        return self._page_snapshot(capture_id, current=True, compact=compact)

    def _page_snapshot(
        self, page_capture_id: str, *, current: bool, compact: bool
    ) -> dict[str, Any] | None:
        """返回页面采集的当前或待处理快照。"""
        inspection = self._page_inspections.get(page_capture_id)
        if inspection is not None:
            return inspection.to_dict(current=current, compact=compact)
        capture = self._page_captures.get(page_capture_id)
        if capture is None:
            return None
        overall_status = "needs_review" if capture.inspection_terminal else "not_started"
        legacy_status = (
            capture.inspection_status
            if capture.inspection_terminal
            else ("captured" if current else "pending")
        )
        return {
            **capture.to_dict(),
            "page_type": (
                capture.page_detection.page_type if capture.page_detection is not None else None
            ),
            "status": legacy_status,
            "quality_status": overall_status,
            "candidate_rule_count": 0,
            "completed_rule_count": 0,
            "candidate_rules": [],
            "result": {
                "overall_status": overall_status,
                "status_counts": {},
                "rules": [],
            },
        }


def _normalized_exploration_payloads(
    *,
    page_capture_id: str,
    page_type: str | None,
    candidate_actions: list[Mapping[str, Any]],
    next_page_hypotheses: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """只把带具体目标的候选动作转换为可执行 Frontier。

    ``next_page_hypotheses`` 是页面类型与进入方式的规划提示，``how_to_enter``
    通常是“点击首个安全帖子卡片”之类自然语言，不是浏览器 selector。它仍保留在
    页面识别审计结果中，但不能直接转换为 page_capture_tool 点击动作。
    """
    payloads: list[dict[str, Any]] = []
    for item in candidate_actions:
        if not isinstance(item, Mapping):
            continue
        action = _optional_text(item.get("action"))
        target = _optional_text(item.get("target"))
        if action is None or target is None:
            continue
        if _normalized_semantic_text(action) not in {
            "goto",
            "click",
            "observe",
            "back",
            "return",
            "browserback",
            "historyback",
            "goback",
            "scroll",
            "hover",
            "networkfault",
        }:
            # 页面识别可能用 none/no_action 表示“无需继续导航”；这类说明不是可执行动作。
            continue
        target_keywords = _string_list(item.get("target_keywords"))
        purpose = _optional_text(item.get("purpose"))
        payloads.append(
            {
                "action": action,
                "target": target,
                "target_keywords": target_keywords,
                "risk_level": _optional_text(item.get("risk_level")),
                "purpose": purpose,
                "expected_next_page_type": _optional_text(item.get("expected_next_page_type")),
                "navigation_depth": _optional_int(item.get("navigation_depth")),
                "supersedes_source_page": _is_nested_return_action(
                    action=action,
                    target=target,
                    purpose=purpose,
                    target_keywords=tuple(
                        keyword
                        for raw_keyword in target_keywords
                        if (keyword := _normalized_semantic_text(raw_keyword)) is not None
                    ),
                ),
            }
        )
    return payloads


def _rule_key(rule_id: str | None, rule_code: str | None) -> str | None:
    """优先使用不可变规则 ID，缺失时回退到规则编码。"""
    return rule_id or rule_code


def _matching_candidate_rule_key(
    candidate_rules: list[dict[str, Any]],
    *,
    rule_id: str | None,
    rule_code: str | None,
) -> str | None:
    """把判定结果身份映射回候选规则使用的稳定键。"""
    if rule_id is not None:
        for candidate in candidate_rules:
            candidate_rule_id = _optional_text(candidate.get("rule_id"))
            if candidate_rule_id == rule_id:
                return _rule_key(
                    candidate_rule_id,
                    _optional_text(candidate.get("rule_code")),
                )
    if rule_code is not None:
        for candidate in candidate_rules:
            candidate_rule_code = _optional_text(candidate.get("rule_code"))
            if candidate_rule_code == rule_code:
                return _rule_key(
                    _optional_text(candidate.get("rule_id")),
                    candidate_rule_code,
                )
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _optional_text(item)) is not None]


def _rule_requires_interaction_evidence(rule: Mapping[str, Any]) -> bool:
    """识别必须在真实交互完成后才能判定的规则。"""
    input_contract = (
        rule.get("input_contract") if isinstance(rule.get("input_contract"), Mapping) else {}
    )
    required_artifacts = {
        item.casefold() for item in _string_list(input_contract.get("required_artifacts"))
    }
    required_roles = {
        item.casefold() for item in _string_list(input_contract.get("required_capture_roles"))
    }
    if "action_trace" in required_artifacts or required_roles.difference({"primary"}):
        return True

    judge_config = rule.get("judge_config") if isinstance(rule.get("judge_config"), Mapping) else {}
    engine_config = (
        judge_config.get("engine_config")
        if isinstance(judge_config.get("engine_config"), Mapping)
        else {}
    )
    handler = _normalized_semantic_text(engine_config.get("handler"))
    if handler in {
        "overlaydismissalhandler",
        "popupclosebehaviorhandler",
        "popupsequencehandler",
        "statetransitionassertion",
    }:
        return True

    capability_config = (
        judge_config.get("capability_config")
        if isinstance(judge_config.get("capability_config"), Mapping)
        else {}
    )
    tool_refs = {
        item.casefold()
        for item in [
            *_string_list(judge_config.get("tool_refs")),
            *_string_list(capability_config.get("deterministic_tools")),
        ]
    }
    return bool(
        capability_config.get("requires_journey") is True
        and tool_refs.intersection(
            {
                "safe_click_observer_tool",
                "landing_page_probe_tool",
                "ad_modal_detector_tool",
            }
        )
    )


def _normalized_semantic_text(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    return "".join(character.lower() for character in text if character.isalnum()) or None


def _coverage_gap_page_type_priority(action: ExplorationActionMemory) -> int | None:
    expected_page_type = _normalized_semantic_text(action.expected_next_page_type)
    if expected_page_type is None:
        return None
    return _COVERAGE_GAP_PAGE_TYPE_PRIORITY.get(expected_page_type)


def _coverage_gap_action_priority(action: ExplorationActionMemory) -> tuple[int, int, str]:
    page_priority = _coverage_gap_page_type_priority(action)
    navigation_depth = action.navigation_depth if action.navigation_depth is not None else 999
    return (
        page_priority if page_priority is not None else 999,
        navigation_depth,
        action.exploration_action_id,
    )


def _bottom_navigation_priority(action: ExplorationActionMemory) -> tuple[int, int, str]:
    """底部导航优先覆盖 P0/P1 页面，再按深度与动作 ID 稳定排序。"""
    expected_page_type = _normalized_semantic_text(action.expected_next_page_type)
    page_priority = _BOTTOM_NAVIGATION_PAGE_TYPE_PRIORITY.get(expected_page_type or "", 50)
    navigation_depth = action.navigation_depth if action.navigation_depth is not None else 999
    return (page_priority, navigation_depth, action.exploration_action_id)


def _is_stale_local_page_action(
    action: ExplorationActionMemory,
    *,
    latest_capture_id: str | None,
) -> bool:
    """跳过已离开来源快照的页面内局部 selector，避免在新页面执行旧 DOM 目标。"""
    source_id = action.source_page_capture_id
    if latest_capture_id is None or source_id is None or source_id == latest_capture_id:
        return False
    normalized_keywords = tuple(
        keyword
        for raw_keyword in action.target_keywords
        if (keyword := _normalized_semantic_text(raw_keyword)) is not None
    )
    return not _is_global_navigation_action(
        purpose=action.purpose,
        target_keywords=normalized_keywords,
    )


def _is_disabled_supplemental_evidence_action(action: ExplorationActionMemory) -> bool:
    """识别补采关闭前已进入工作记忆、但尚未执行的兼容动作。"""
    normalized_action = action.action.strip().casefold()
    if normalized_action == "timed_capture":
        return True
    if action.resumes_page_exploration:
        # 旧版本可能错误改写开屏关闭等生命周期动作的 purpose；这类动作仍需
        # 完成正常页面过渡，不能按规则补采误杀。
        return False
    normalized_purpose = str(action.purpose or "").strip().casefold()
    if normalized_purpose in _DISABLED_SUPPLEMENTAL_EVIDENCE_PURPOSES:
        return True
    if normalized_purpose.startswith(_DISABLED_SUPPLEMENTAL_EVIDENCE_PURPOSE_PREFIXES):
        return True
    return normalized_action == "scroll" and (
        action.target == "page_coverage" or action.blocks_page_inspection
    )


def _is_semantic_entry_navigation_action(*, target_keywords: tuple[str, ...]) -> bool:
    return "settingsentry" in target_keywords


def _is_profile_local_navigation_action(*, target_keywords: tuple[str, ...]) -> bool:
    """识别由个人中心结构能力签发的签到、设置或支付入口。"""
    return "profilelocalaction" in target_keywords and any(
        keyword in target_keywords
        for keyword in ("pagetypeactivity", "pagetypesettings", "pagetypepayment")
    )


def _is_global_bottom_navigation_action(
    *, purpose: str | None, target_keywords: tuple[str, ...]
) -> bool:
    """把受控底部导航视为跨页面采集的同一业务分支。"""
    return "bottomnavigation" in target_keywords


def _is_global_business_navigation_action(
    *, purpose: str | None, target_keywords: tuple[str, ...]
) -> bool:
    """识别由真实同源主导航 href 生成、可跨页面快照执行的业务分支。"""
    return (
        _normalized_semantic_text(purpose) == "探索真实同源主导航中的未访问业务分支"
        and "businessnavigation" in target_keywords
        and "internalnavigation" in target_keywords
        and any(keyword.startswith("path") for keyword in target_keywords)
    )


def _is_global_navigation_action(*, purpose: str | None, target_keywords: tuple[str, ...]) -> bool:
    """统一判断跨页面仍应保留的受控全局业务导航。"""
    return _is_global_bottom_navigation_action(
        purpose=purpose,
        target_keywords=target_keywords,
    ) or _is_global_business_navigation_action(
        purpose=purpose,
        target_keywords=target_keywords,
    )


def _is_page_local_navigation_action(action: ExplorationActionMemory) -> bool:
    """识别应在离开当前业务 Tab 前完成的页内“更多/展开”分支。"""
    if (
        action.action.strip().casefold() not in {"click", "hover"}
        or _is_high_risk(action.risk_level)
        or _normalized_semantic_text(action.source_page_type)
        != _normalized_semantic_text(action.expected_next_page_type)
    ):
        return False
    searchable = (
        _normalized_semantic_text(
            " ".join([str(action.target or ""), str(action.purpose or ""), *action.target_keywords])
        )
        or ""
    )
    return _is_same_page_type_tab_navigation_action(action) or any(
        marker in searchable for marker in ("更多", "查看更多", "more", "expand", "vaniconarrow")
    )


def _is_page_tab_navigation_action(action: ExplorationActionMemory) -> bool:
    """识别页面识别器确定性生成的页内业务 Tab。"""
    keywords = {
        normalized
        for item in action.target_keywords
        if (normalized := _normalized_semantic_text(item)) is not None
    }
    return (
        "pagetabnavigation" in keywords
        or _normalized_semantic_text(action.purpose) == "探索稳定页面中的页内tab业务分支"
    )


def _is_same_page_type_tab_navigation_action(action: ExplorationActionMemory) -> bool:
    """识别不会离开当前页面类型、必须原地完成的分类 Tab。"""
    source_page_type = _normalized_semantic_text(action.source_page_type)
    return bool(
        source_page_type is not None
        and source_page_type == _normalized_semantic_text(action.expected_next_page_type)
        and _is_page_tab_navigation_action(action)
    )


def _is_nested_content_action(*, purpose: str | None, target_keywords: tuple[str, ...]) -> bool:
    """识别从业务列表进入详情/播放页的受控首卡动作。"""
    return (
        _normalized_semantic_text(purpose) == "探索当前业务列表首个真实可见的低风险内容卡片"
        and "nestedcontent" in target_keywords
    )


def _is_semantic_nested_content_action(
    *,
    action: str,
    purpose: str | None,
    expected_next_page_type: str | None,
    target_keywords: tuple[str, ...],
) -> bool:
    """识别确定性首卡及模型生成的同义详情入口，用于跨采集语义去重。"""
    if _is_nested_content_action(purpose=purpose, target_keywords=target_keywords):
        return True
    if action.strip().casefold() not in {"click", "hover"}:
        return False
    normalized_expected = _normalized_semantic_text(expected_next_page_type)
    normalized_purpose = _normalized_semantic_text(purpose) or ""
    if normalized_expected in {"contentdetail", "postdetail", "videodetail"}:
        return True
    return any(
        marker in normalized_purpose
        for marker in (
            "低风险内容卡片",
            "进入内容详情",
            "进入帖子详情",
            "进入视频详情",
            "进入影片详情",
            "进入播放",
        )
    )


def _is_internal_navigation_action(
    *,
    target_keywords: tuple[str, ...],
) -> bool:
    """识别由页面真实同源 href 推导出的跨采集业务分支。"""
    return "internalnavigation" in target_keywords and any(
        keyword.startswith("path") for keyword in target_keywords
    )


def _is_missing_home_navigation_action(action: ExplorationActionMemory) -> bool:
    """识别从非首页业务页回补首页覆盖的低风险导航动作。"""
    if _normalized_semantic_text(action.expected_next_page_type) != "home":
        return False
    if _normalized_semantic_text(action.source_page_type) == "home":
        return False
    if _is_high_risk(action.risk_level):
        return False
    return action.action.strip().casefold() in {"click", "goto"}


def _is_nested_return_action(
    *,
    action: str,
    target: str,
    purpose: str | None,
    target_keywords: tuple[str, ...],
) -> bool:
    """只把平台生成的详情历史返回视为对来源快照的替代。"""
    return (
        action.casefold() == "back"
        and _normalized_semantic_text(target) == "browserhistory"
        and "nestedreturn" in target_keywords
        and _normalized_semantic_text(purpose)
        in {
            "详情质检完成后返回来源列表继续探索",
            "详情或播放页质检完成后返回来源列表继续探索",
        }
    )


def _is_browser_history_return_intent(
    *,
    action: str | None,
    target: str | None,
    target_keywords: tuple[str, ...],
) -> bool:
    """识别模型生成的浏览器历史返回同义表达，不把真实页面返回按钮混入。"""
    normalized_action = _normalized_semantic_text(action)
    normalized_target = _normalized_semantic_text(target)
    if normalized_action in {"back", "browserback", "historyback", "goback"}:
        return True
    return normalized_action == "return" and (
        normalized_target
        in {
            "browserhistory",
            "historyback",
            "浏览器后退",
            "浏览器返回",
        }
        or "nestedreturn" in target_keywords
        or ({"返回", "后退"} <= set(target_keywords))
    )


def _is_auxiliary_page_return_action(action: ExplorationActionMemory | None) -> bool:
    if action is None or action.action.strip().casefold() != "back":
        return False
    return any(
        _normalized_semantic_text(keyword) == "auxiliarypagereturn"
        for keyword in action.target_keywords
    )


def _is_high_risk(risk_level: str | None) -> bool:
    return _normalized_semantic_text(risk_level) in {"high", "critical"}


def _higher_risk_level(current: str | None, incoming: str | None) -> str | None:
    """同一语义动作重复发现时只允许风险等级上升。"""
    current_text = _optional_text(current)
    incoming_text = _optional_text(incoming)
    if current_text is None:
        return incoming_text
    if incoming_text is None:
        return current_text
    priorities = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    current_priority = priorities.get(current_text.casefold(), 0)
    incoming_priority = priorities.get(incoming_text.casefold(), 0)
    return incoming_text if incoming_priority > current_priority else current_text


def _block_high_risk_action(action: ExplorationActionMemory) -> None:
    """把任何进入 Frontier 的高风险动作收口为不可执行终态。"""
    action.status = "blocked"
    action.failure_reason = "high_risk_action"
    action.failure_code = "HIGH_RISK_ACTION"
    action.failure_message = "高风险页面动作仅记录覆盖线索，禁止自动执行。"
    action.failure_retryable = False


def _restore_source_original_action_id(
    action: ExplorationActionMemory,
) -> str | None:
    marker = next(
        (item for item in action.target_keywords if item.startswith(_RESTORE_SOURCE_ACTION_PREFIX)),
        None,
    )
    return marker.removeprefix(_RESTORE_SOURCE_ACTION_PREFIX) if marker is not None else None


def _normalized_page_identity(value: str | None) -> str | None:
    """规范化可执行 DOM 身份；查询参数变化视为不同页面状态。"""
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def _normalized_page_location(value: str | None) -> str | None:
    """规范化业务页面地址；恢复判定忽略查询参数和锚点。"""
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path or "/",
            "",
            "",
        )
    )


def _is_obstruction_dismissal_action(action: ExplorationActionMemory) -> bool:
    """识别被阻断的遮挡关闭动作，不把支付、退出等业务高风险动作算作覆盖缺口。"""
    if action.action.strip().casefold() != "click":
        return False
    normalized_keywords = {
        keyword
        for raw_keyword in action.target_keywords
        if (keyword := _normalized_semantic_text(raw_keyword)) is not None
    }
    if "closecontrol" in normalized_keywords:
        return True
    purpose = str(action.purpose or "").casefold()
    return any(marker in purpose for marker in ("关闭", "close", "dismiss")) and any(
        marker in purpose
        for marker in ("弹窗", "遮挡", "广告", "popup", "dialog", "modal", "overlay")
    )
