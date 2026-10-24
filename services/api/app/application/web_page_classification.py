"""基于无遮挡 Capture 与目录结构规则的 Web 页面签发服务。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Literal, Protocol

from services.api.app.application.web_article_body_structure import (
    ARTICLE_TOKENS,
    is_web_article_body_node,
)
from services.api.app.application.web_assessment_errors import (
    WebAssessmentPermissionDeniedError,
    WebAssessmentTransientServiceError,
)
from services.api.app.application.web_detail_candidate_extraction import (
    detail_candidates_from_ready_snapshot,
)
from services.api.app.application.web_detail_classification_policy import (
    deterministic_detail_result,
)
from services.api.app.application.web_home_navigation_facts import (
    has_trusted_active_home_navigation,
)
from services.api.app.application.web_interaction_facts import WebInteractionCaptureFacts
from services.api.app.application.web_logical_page import is_web_home_url
from services.api.app.application.web_navigation_recognition import (
    WebNavigationStructureFacts,
    assess_navigation_container,
)
from services.api.app.application.web_page_body_scopes import WebSnapshotBodyScopes
from services.api.app.application.web_page_recognition_rules import (
    WebPageRuleEvaluation,
    evaluate_web_page_rules,
)
from services.api.app.application.web_primary_subject_facts import (
    extract_subject_region_facts,
    primary_subject_facts,
)
from services.api.app.application.web_readiness_contracts import WebSettleAssessment
from services.api.app.application.web_subject_classification_audit import (
    subject_structure_fingerprint,
)
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.application.web_truncated_detail_media import recover_truncated_detail_media
from services.api.app.application.web_video_feed_structure import continuous_video_feed_count
from services.api.app.domain.exploration import WEB_DETAIL_PAGE_TYPES as WEB_DETAIL_PAGE_TYPES
from services.api.app.domain.exploration import (
    WEB_LIST_PAGE_TYPES,
)
from services.api.app.domain.page import PageTypeCatalog
from services.api.app.domain.page.web_page_catalog import CURRENT_WEB_CATALOG_VERSION
from services.api.app.domain.page.web_site_menu_registry import WebPageClassificationHint

WEB_FORMAL_PAGE_TYPE_MINIMUM_CONFIDENCE: Final[float] = 0.7
_STRONG_HOME_LIST_MINIMUM_CONFIDENCE = 0.75
_STRONG_HOME_LIST_MINIMUM_CARDS = 6
_DETAIL_TRANSITION_HINT_SOURCE = "trusted_list_detail_transition"
_HOME_VIDEO_FEED_MINIMUM_CARDS = 6
_HOME_VIDEO_FEED_CONFIDENCE = 0.8
_HOME_VIDEO_FEED_REASON = "WEB_HOME_VIDEO_FEED_NAV_CONFIRMED"
_SHORT_VIDEO_HOME_MINIMUM_NAV_ENTRIES = 6
_SHORT_VIDEO_HOME_REASON = "WEB_HOME_SHORT_VIDEO_FEED_NAV_CONFIRMED"
_NONROOT_HOME_VIDEO_LIST_REASON = "WEB_NONROOT_HOME_VIDEO_LIST_FALLBACK"
_SENSITIVE_DIAGNOSTIC_ASSIGNMENT = re.compile(
    r"(?i)\b(token|password|passwd|pwd|secret|cookie|authorization|api[_-]?key)"
    r"\s*[:=]\s*[^\s,;]+"
)
_BEARER_DIAGNOSTIC = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


class WebPageClassificationReason(StrEnum):
    """可进入持久化合同的 Web 分类稳定原因码。"""

    UNSUPPORTED = "WEB_PAGE_TYPE_UNSUPPORTED"
    LOW_CONFIDENCE = "WEB_PAGE_TYPE_LOW_CONFIDENCE"
    EVIDENCE_INSUFFICIENT = "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT"
    STRUCTURE_INSUFFICIENT = "WEB_PAGE_STRUCTURE_INSUFFICIENT"
    URL_HINT_DOM_CONFLICT = "URL_HINT_DOM_CONFLICT"
    MODEL_NEEDS_REVIEW = "WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW"
    MODEL_PERMISSION_DENIED = "WEB_PAGE_TYPE_MODEL_PERMISSION_DENIED"
    STRONG_NEGATIVE_CONFLICT = "WEB_PAGE_TYPE_STRONG_NEGATIVE_CONFLICT"
    PRIMARY_REGION_CONFLICT = "WEB_PAGE_TYPE_PRIMARY_REGION_CONFLICT"


@dataclass(frozen=True, kw_only=True)
class WebPageStructureFacts:
    """从可信 DOM snapshot 计算、不可由模型自述的结构事实。"""

    navigation_element_count: int
    repeated_link_count: int
    video_element_count: int
    single_subject_element_count: int
    dom_tokens: tuple[str, ...]
    audio_element_count: int = 0
    primary_detail_link_count: int = 0
    repeated_primary_card_count: int = 0
    media_card_count: int = 0
    video_specific_card_count: int = 0
    article_body_count: int = 0
    paragraph_text_length: int = 0
    player_root_count: int = 0
    advertisement_element_count: int = 0
    transaction_element_count: int = 0
    is_canonical_root_url: bool = False
    active_home_navigation: bool = False
    top_level_obstruction_count: int = 0
    top_navigation_container_count: int = 0
    visible_category_entry_count: int = 0
    homepage_section_count: int = 0
    repeated_subject_element_count: int = 0
    continuous_video_feed_count: int = 0
    ranked_video_row_count: int = 0
    primary_subject_dom_tokens: tuple[str, ...] = ()
    primary_video_element_count: int = 0
    image_gallery_root_count: int = 0
    image_viewer_control_count: int = 0
    confirmed_primary_video_resource_count: int = 0
    player_shell_without_resource_count: int = 0
    opaque_primary_media_count: int = 0
    search_input_count: int = 0
    search_trigger_count: int = 0
    query_result_group_count: int = 0
    activity_primary_region_count: int = 0
    activity_action_count: int = 0
    navigation_structure_facts: WebNavigationStructureFacts | None = None
    subject_region_facts: WebSubjectRegionFacts | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "navigation_element_count",
            "repeated_link_count",
            "video_element_count",
            "single_subject_element_count",
            "audio_element_count",
            "primary_detail_link_count",
            "repeated_primary_card_count",
            "media_card_count",
            "video_specific_card_count",
            "article_body_count",
            "paragraph_text_length",
            "player_root_count",
            "advertisement_element_count",
            "transaction_element_count",
            "top_level_obstruction_count",
            "top_navigation_container_count",
            "visible_category_entry_count",
            "homepage_section_count",
            "repeated_subject_element_count",
            "continuous_video_feed_count",
            "ranked_video_row_count",
            "primary_video_element_count",
            "image_gallery_root_count",
            "image_viewer_control_count",
            "confirmed_primary_video_resource_count",
            "player_shell_without_resource_count",
            "opaque_primary_media_count",
            "search_input_count",
            "search_trigger_count",
            "query_result_group_count",
            "activity_primary_region_count",
            "activity_action_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if type(self.is_canonical_root_url) is not bool:
            raise ValueError("is_canonical_root_url 必须为布尔值")
        if type(self.active_home_navigation) is not bool:
            raise ValueError("active_home_navigation 必须为布尔值")
        if not isinstance(self.dom_tokens, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.dom_tokens
        ):
            raise ValueError("dom_tokens 必须为非空文字元组")
        if not isinstance(self.primary_subject_dom_tokens, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.primary_subject_dom_tokens
        ):
            raise ValueError("primary_subject_dom_tokens 必须为文字元组")


@dataclass(frozen=True, kw_only=True)
class WebPageAssessment:
    """模型页面判定；最终类型仍须通过应用层证据门。"""

    page_type: str | None
    confidence: float
    evidence: tuple[str, ...]
    needs_review_reason: str | None = None
    raw_page_type: str | None = None
    proposed_page_type: str | None = None
    observed_page_type: str | None = None
    expected_page_type: str | None = None
    conflict_reason_code: str | None = None
    diagnostic_message: str | None = None
    reason_codes: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    needs_review: bool = False
    guide_version: str | None = None
    reused_capture_id: str | None = None
    reused_logical_page_id: str | None = None
    subject_region_facts: WebSubjectRegionFacts | None = None
    structure_fingerprint: str | None = None
    decision_source: str = "model"
    model_result: dict[str, object] | None = None
    deterministic_result: dict[str, object] | None = None

    def __post_init__(self) -> None:
        page_type = self.page_type.strip() if isinstance(self.page_type, str) else None
        if self.page_type is not None and not page_type:
            raise ValueError("page_type 不能为空字符串")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not 0 <= float(self.confidence) <= 1
        ):
            raise ValueError("confidence 必须在 0 到 1 之间")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.evidence
        ):
            raise ValueError("evidence 必须为非空文字元组")
        reason = self.needs_review_reason
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("needs_review_reason 不能为空字符串")
        raw_page_type = (
            self.raw_page_type.strip() if isinstance(self.raw_page_type, str) else page_type
        )
        if self.raw_page_type is not None and not raw_page_type:
            raise ValueError("raw_page_type 不能为空字符串")
        for field_name in (
            "proposed_page_type",
            "observed_page_type",
            "expected_page_type",
            "conflict_reason_code",
            "diagnostic_message",
            "guide_version",
        ):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} 不能为空字符串")
        for field_name in ("reason_codes", "conflicts"):
            value = getattr(self, field_name)
            if not isinstance(value, tuple) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ValueError(f"{field_name} 必须为非空文字元组")
        if not isinstance(self.needs_review, bool):
            raise ValueError("needs_review 必须为布尔值")
        object.__setattr__(self, "page_type", page_type)
        object.__setattr__(self, "raw_page_type", raw_page_type)
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(
            self,
            "evidence",
            tuple(item.strip()[:500] for item in self.evidence),
        )
        object.__setattr__(
            self,
            "needs_review_reason",
            reason.strip() if isinstance(reason, str) else None,
        )
        for field_name in (
            "proposed_page_type",
            "observed_page_type",
            "expected_page_type",
            "conflict_reason_code",
            "diagnostic_message",
            "guide_version",
        ):
            value = getattr(self, field_name)
            object.__setattr__(
                self,
                field_name,
                value.strip() if isinstance(value, str) else None,
            )
        normalized_reasons = tuple(dict.fromkeys(item.strip() for item in self.reason_codes))
        if reason is not None and reason.strip() not in normalized_reasons:
            normalized_reasons = (*normalized_reasons, reason.strip())
        normalized_conflicts = tuple(dict.fromkeys(item.strip() for item in self.conflicts))
        if (
            self.conflict_reason_code is not None
            and self.conflict_reason_code not in normalized_conflicts
        ):
            normalized_conflicts = (*normalized_conflicts, self.conflict_reason_code)
        object.__setattr__(self, "reason_codes", normalized_reasons)
        object.__setattr__(self, "conflicts", normalized_conflicts)
        object.__setattr__(
            self,
            "needs_review",
            self.needs_review or page_type is None or reason is not None,
        )


@dataclass(frozen=True, kw_only=True)
class WebReadyPageCapture:
    """分类器可读取的无遮挡 Web Capture 引用。"""

    capture_id: str
    web_capture_stage: str
    screenshot_artifact_id: str
    dom_artifact_id: str
    visible_text_artifact_id: str
    page_role: str
    structure_facts: WebPageStructureFacts
    captured_at: datetime | None = None
    classification_hint: WebPageClassificationHint | None = None
    url: str | None = None
    capture_purpose: str = "web_page_ready_baseline"
    menu_entry_ids: tuple[str, ...] = ()
    inspection_readiness: WebSettleAssessment | None = None
    interaction_facts: WebInteractionCaptureFacts | None = None
    supplemental_screenshot_artifact_ids: tuple[str, ...] = ()
    supplemental_evidence_status: Literal["captured", "failed", "not_attempted"] = "not_attempted"
    supplemental_evidence_reason_code: str | None = None
    requested_url: str | None = None
    redirect_chain: tuple[str, ...] = ()
    finalization_epoch: int = 0
    capture_state_fingerprint: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "capture_id",
            "web_capture_stage",
            "screenshot_artifact_id",
            "dom_artifact_id",
            "visible_text_artifact_id",
            "page_role",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
        if self.page_role not in {"home", "menu", "detail"}:
            raise ValueError("page_role 不受支持")
        if not isinstance(self.structure_facts, WebPageStructureFacts):
            raise TypeError("structure_facts 类型错误")
        if self.classification_hint is not None and not isinstance(
            self.classification_hint,
            WebPageClassificationHint,
        ):
            raise TypeError("classification_hint 类型错误")
        if self.url is not None and (not isinstance(self.url, str) or not self.url.strip()):
            raise ValueError("url 不能为空字符串")
        if self.url is not None and is_web_home_url(self.url):
            object.__setattr__(self, "page_role", "home")
            object.__setattr__(
                self, "structure_facts", replace(self.structure_facts, is_canonical_root_url=True)
            )
        if not isinstance(self.capture_purpose, str) or not self.capture_purpose.strip():
            raise ValueError("capture_purpose 不能为空")
        if not isinstance(self.menu_entry_ids, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.menu_entry_ids
        ):
            raise ValueError("menu_entry_ids 必须为非空文字元组")
        if self.inspection_readiness is not None and not isinstance(
            self.inspection_readiness,
            WebSettleAssessment,
        ):
            raise TypeError("inspection_readiness 类型错误")
        if self.captured_at is not None:
            if not isinstance(self.captured_at, datetime) or self.captured_at.tzinfo is None:
                raise ValueError("captured_at 必须为带时区的时间")
            object.__setattr__(self, "captured_at", self.captured_at.astimezone(UTC))
        if (
            not isinstance(self.supplemental_screenshot_artifact_ids, tuple)
            or any(
                not isinstance(item, str) or not item.strip()
                for item in self.supplemental_screenshot_artifact_ids
            )
            or self.supplemental_evidence_status not in {"captured", "failed", "not_attempted"}
            or (
                self.supplemental_evidence_status == "captured"
                and not self.supplemental_screenshot_artifact_ids
            )
            or (
                self.supplemental_evidence_status != "captured"
                and self.supplemental_screenshot_artifact_ids
            )
            or (
                self.supplemental_evidence_reason_code is not None
                and (
                    not isinstance(self.supplemental_evidence_reason_code, str)
                    or not self.supplemental_evidence_reason_code.strip()
                )
            )
        ):
            raise ValueError("supplemental 证据状态无效")


class WebPageAssessmentPort(Protocol):
    async def classify(
        self,
        *,
        screenshot_artifact_id: str,
        dom_artifact_id: str,
        visible_text_artifact_id: str,
        supplemental_screenshot_artifact_ids: tuple[str, ...] = (),
        allowed_page_types: tuple[str, ...],
        page_type_catalog: tuple[dict[str, object], ...],
        page_role: str,
        structure_facts: Mapping[str, object],
        classification_hint: Mapping[str, object] | None,
    ) -> WebPageAssessment: ...


class WebPageClassifier:
    """执行阶段、业务闭集、置信度和结构证据四重校验。"""

    def __init__(
        self,
        *,
        assessor: WebPageAssessmentPort,
        catalog: PageTypeCatalog,
        minimum_confidence: float = WEB_FORMAL_PAGE_TYPE_MINIMUM_CONFIDENCE,
    ) -> None:
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum_confidence 必须在 0 到 1 之间")
        if catalog.version != CURRENT_WEB_CATALOG_VERSION:
            raise ValueError("WEB_LEGACY_CATALOG_EXECUTION_FORBIDDEN")
        self._assessor = assessor
        self._catalog = catalog
        # 调用者只能收紧正式门槛，不能用显式参数降低全局硬下限。
        self._minimum_confidence = max(
            WEB_FORMAL_PAGE_TYPE_MINIMUM_CONFIDENCE, minimum_confidence
        )

    async def classify(self, capture: WebReadyPageCapture) -> WebPageAssessment:
        assessment = await self._classify(capture)
        regions = capture.structure_facts.subject_region_facts
        hint = capture.classification_hint
        hint_conflict = (
            assessment.needs_review and hint is not None
            and hint.confidence >= self._minimum_confidence
            and hint.source != _DETAIL_TRANSITION_HINT_SOURCE
            and assessment.observed_page_type is not None
            and assessment.observed_page_type != hint.expected_page_type
        )
        return replace(
            assessment,
            subject_region_facts=regions,
            structure_fingerprint=subject_structure_fingerprint(regions) if regions else None,
            # 即使安全门提前结束，可信提示与模型之间的差异仍作为审计留存。
            conflict_reason_code=(
                assessment.conflict_reason_code or WebPageClassificationReason.URL_HINT_DOM_CONFLICT
                if hint_conflict else assessment.conflict_reason_code
            ),
        )

    async def _classify(self, capture: WebReadyPageCapture) -> WebPageAssessment:
        if not isinstance(capture, WebReadyPageCapture):
            raise TypeError("capture 必须为 WebReadyPageCapture")
        accepted_evidence_stages = {
            "web_page_ready_baseline",
            "web_primary_content_evidence",
        }
        if (
            capture.web_capture_stage not in accepted_evidence_stages
            or capture.capture_purpose != capture.web_capture_stage
        ):
            raise ValueError("Web 页面分类只接受无遮挡基线或可信主体补图 Capture")
        allowed_page_types = _allowed_page_types_for_role(
            self._catalog.codes,
            page_role=capture.page_role,
        )
        public_catalog = tuple(
            item for item in _public_catalog(self._catalog) if item["code"] in allowed_page_types
        )
        machine_rules = getattr(self._catalog, "recognition_rules", {})
        if not set(allowed_page_types).issubset(machine_rules):
            raise ValueError("Web 目录缺少完整机器规则，禁止回退历史结构门")
        evaluations = {
            code: evaluate_web_page_rules(
                page_type=code, facts=capture.structure_facts, rules=machine_rules[code]
            )
            for code in allowed_page_types
            if code in machine_rules
        }
        facts_payload = _structure_facts_payload(capture.structure_facts)
        for knowledge_name in ("navigation_signals", "common_site_observations"):
            knowledge = getattr(self._catalog, knowledge_name, None)
            facts_payload[knowledge_name] = (
                _knowledge_payload(knowledge) if knowledge is not None else {}
            )
        if evaluations:
            facts_payload["rule_evaluations"] = {
                code: result.to_payload() for code, result in evaluations.items()
            }
        deterministic = deterministic_detail_result(evaluations)
        model_status = "available"
        try:
            assessment = await self._assessor.classify(
                screenshot_artifact_id=capture.screenshot_artifact_id,
                dom_artifact_id=capture.dom_artifact_id,
                visible_text_artifact_id=capture.visible_text_artifact_id,
                # 长图仅保存供质检使用，页型识别沿用 ready 首屏主证据。
                supplemental_screenshot_artifact_ids=(),
                allowed_page_types=allowed_page_types,
                page_type_catalog=public_catalog,
                page_role=capture.page_role,
                structure_facts=facts_payload,
                classification_hint=_classification_hint_payload(capture.classification_hint),
            )
        except WebAssessmentTransientServiceError:
            if deterministic["page_type"] == "unknown":
                raise
            model_status, assessment = "unavailable", None
        except WebAssessmentPermissionDeniedError:
            reason = WebPageClassificationReason.MODEL_PERMISSION_DENIED.value
            return WebPageAssessment(
                page_type="unknown",
                proposed_page_type="unknown",
                confidence=0.0,
                evidence=(reason,),
                needs_review_reason=reason,
                guide_version=self._catalog.version,
                decision_source="provider_denial",
                model_result={
                    "status": "permission_denied",
                    "page_type": None,
                    "confidence": 0.0,
                    "needs_review": True,
                    "conflicts": (),
                    "rule_evaluation": None,
                },
                deterministic_result=deterministic,
            )
        if assessment is None:
            if model_status == "available":
                model_status = "missing"
            assessment = WebPageAssessment(page_type=None, confidence=0, evidence=())
        if not isinstance(assessment, WebPageAssessment):
            raise TypeError("Web 模型分类输出格式无效")
        model_result = {
            "status": model_status,
            "page_type": safe_web_page_type_candidate(assessment.page_type),
            "confidence": assessment.confidence,
            "needs_review": assessment.needs_review if model_status == "available" else False,
            "conflicts": tuple(safe_web_diagnostic_message(item) for item in assessment.conflicts),
            "rule_evaluation": (
                evaluations[assessment.page_type].to_payload()
                if assessment.page_type in evaluations else None
            ),
        }
        observed_page_type = (
            assessment.page_type if assessment.page_type in allowed_page_types else None
        )
        proposed_page_type = (
            observed_page_type or assessment.proposed_page_type or assessment.raw_page_type
        )
        assessment = WebPageAssessment(
            page_type=assessment.page_type,
            raw_page_type=assessment.raw_page_type,
            proposed_page_type=proposed_page_type,
            observed_page_type=observed_page_type,
            expected_page_type=(
                capture.classification_hint.expected_page_type
                if capture.classification_hint is not None
                else None
            ),
            confidence=assessment.confidence,
            evidence=assessment.evidence,
            needs_review_reason=assessment.needs_review_reason,
            conflict_reason_code=assessment.conflict_reason_code,
            diagnostic_message=assessment.diagnostic_message,
            reason_codes=assessment.reason_codes,
            conflicts=assessment.conflicts,
            needs_review=assessment.needs_review,
            guide_version=self._catalog.version,
            model_result=model_result,
            deterministic_result=deterministic,
        )
        regions = capture.structure_facts.subject_region_facts
        if regions is not None and (
            regions.primary_subject_count > 1
            or regions.primary_subject_kind == "conflicting" or regions.primary_player_count > 1
        ):
            return _needs_review(assessment, WebPageClassificationReason.PRIMARY_REGION_CONFLICT)
        model_preferred = (
            assessment.page_type in WEB_DETAIL_PAGE_TYPES
            and assessment.page_type in allowed_page_types
            and assessment.confidence >= self._minimum_confidence
        )
        if model_preferred and (
            regions is None or regions.primary_subject_count == 0
        ):
            return _needs_review(
                assessment,
                WebPageClassificationReason.EVIDENCE_INSUFFICIENT,
                conflicts=evaluations[assessment.page_type].strong_conflicts,
                diagnostic_message=assessment.needs_review_reason,
            )
        if model_preferred and (
            regions.primary_subject_count != 1
            or capture.structure_facts.top_level_obstruction_count > 0
        ):
            return _needs_review(
                assessment,
                WebPageClassificationReason.PRIMARY_REGION_CONFLICT,
                conflicts=evaluations[assessment.page_type].strong_conflicts,
                diagnostic_message=assessment.needs_review_reason,
            )
        short_video_home = _trusted_short_video_home(
            capture=capture,
            assessment=assessment,
            model_status=model_status,
            evaluations=evaluations,
            minimum_confidence=self._minimum_confidence,
        )
        if short_video_home or _trusted_home_video_feed(
            capture=capture,
            assessment=assessment,
            model_status=model_status,
            evaluations=evaluations,
            minimum_confidence=self._minimum_confidence,
        ):
            home_evaluation = evaluations["home"]
            reason = (
                _SHORT_VIDEO_HOME_REASON if short_video_home else _HOME_VIDEO_FEED_REASON
            )
            return replace(
                assessment,
                page_type="home",
                confidence=_HOME_VIDEO_FEED_CONFIDENCE,
                needs_review=False,
                needs_review_reason=None,
                conflict_reason_code=None,
                conflicts=(),
                diagnostic_message=safe_web_diagnostic_message(
                    assessment.needs_review_reason or assessment.diagnostic_message
                ),
                evidence=(*assessment.evidence, reason),
                reason_codes=(*assessment.reason_codes, reason),
                decision_source="deterministic",
                deterministic_result={
                    "page_type": "home",
                    "confidence": _HOME_VIDEO_FEED_CONFIDENCE,
                    "reason_code": reason,
                    "rules": {
                        **deterministic["rules"],
                        "home": home_evaluation.to_payload(),
                    },
                },
            )
        if deterministic["page_type"] in WEB_DETAIL_PAGE_TYPES or model_preferred:
            # 主体只证明详情安全边界；合法可信模型优先决定内容与视频轴。
            if (
                capture.inspection_readiness is not None
                and not capture.inspection_readiness.can_serve_evidence
            ):
                return _needs_review(
                    assessment, WebPageClassificationReason.STRONG_NEGATIVE_CONFLICT,
                    conflicts=(capture.inspection_readiness.status.value,),
                )
            if assessment.page_type is not None and assessment.page_type not in allowed_page_types:
                return _needs_review(assessment, WebPageClassificationReason.UNSUPPORTED)
            if assessment.conflicts:
                return _needs_review(
                    assessment, WebPageClassificationReason.STRONG_NEGATIVE_CONFLICT
                )
            if assessment.needs_review_reason or (
                assessment.needs_review and model_status == "available"
            ):
                return _needs_review(
                    assessment, WebPageClassificationReason.MODEL_NEEDS_REVIEW,
                    diagnostic_message=assessment.needs_review_reason,
                )
            if not model_preferred and deterministic["confidence"] < self._minimum_confidence:
                return _needs_review(assessment, WebPageClassificationReason.LOW_CONFIDENCE)
            return replace(
                assessment,
                page_type=assessment.page_type if model_preferred else deterministic["page_type"],
                proposed_page_type=(
                    assessment.page_type if model_preferred else deterministic["page_type"]
                ),
                confidence=(
                    assessment.confidence if model_preferred else deterministic["confidence"]
                ),
                needs_review=False,
                decision_source="model" if model_preferred else "deterministic",
                conflict_reason_code=(
                    WebPageClassificationReason.URL_HINT_DOM_CONFLICT.value
                    if capture.classification_hint is not None
                    and capture.classification_hint.confidence >= self._minimum_confidence
                    and capture.classification_hint.source != _DETAIL_TRANSITION_HINT_SOURCE
                    and capture.classification_hint.expected_page_type != (
                        assessment.page_type if model_preferred else deterministic["page_type"]
                    ) else assessment.conflict_reason_code
                ),
            )
        if assessment.page_type not in allowed_page_types:
            return _needs_review(
                assessment,
                WebPageClassificationReason.UNSUPPORTED,
            )
        if assessment.page_type == "unknown":
            return unknown_web_page_assessment(
                assessment, reason=WebPageClassificationReason.EVIDENCE_INSUFFICIENT
            )
        rule_evaluation = evaluations[assessment.page_type]
        if rule_evaluation is not None:
            assessment = replace(
                assessment,
                evidence=assessment.evidence
                + tuple(
                    f"WEB_PAGE_RULE_{kind}:{item}"
                    for kind, values in (
                        ("POSITIVE", rule_evaluation.positive_hits),
                        ("SUPPORTING", rule_evaluation.supporting_hits),
                        ("MISSING", rule_evaluation.missing_facts),
                        ("CONFLICT", rule_evaluation.strong_conflicts),
                    )
                    for item in values
                ),
            )
        structure_supported = rule_evaluation.required_satisfied
        strong_conflicts = rule_evaluation.strong_conflicts
        if strong_conflicts:
            return _needs_review(
                assessment,
                WebPageClassificationReason.STRONG_NEGATIVE_CONFLICT,
                conflicts=strong_conflicts,
            )
        if not structure_supported:
            if _trusted_nonroot_home_video_list(
                capture=capture,
                assessment=assessment,
                model_status=model_status,
                evaluations=evaluations,
                minimum_confidence=self._minimum_confidence,
            ):
                video_list = evaluations["video_list"]
                generic_overlaps = tuple(
                    code for code in ("content_list", "collection_page")
                    if code in evaluations and evaluations[code].required_satisfied
                    and not evaluations[code].strong_conflicts
                )
                return replace(
                    assessment,
                    page_type="video_list",
                    proposed_page_type="video_list",
                    confidence=0.9,
                    needs_review=False,
                    evidence=(
                        *assessment.evidence,
                        _NONROOT_HOME_VIDEO_LIST_REASON,
                        *(f"WEB_PAGE_RULE_POSITIVE:{item}" for item in video_list.positive_hits),
                    ),
                    reason_codes=(*assessment.reason_codes, _NONROOT_HOME_VIDEO_LIST_REASON),
                    decision_source="deterministic",
                    deterministic_result={
                        **deterministic,
                        "page_type": "video_list",
                        "confidence": 0.9,
                        "reason_code": _NONROOT_HOME_VIDEO_LIST_REASON,
                        "rules": {
                            **deterministic["rules"],
                            "home": rule_evaluation.to_payload(),
                            "video_list": video_list.to_payload(),
                        },
                        "generic_rule_overlaps": generic_overlaps,
                    },
                )
            return _needs_review(
                assessment,
                WebPageClassificationReason.EVIDENCE_INSUFFICIENT,
                diagnostic_message=assessment.needs_review_reason,
            )
        if assessment.conflicts:
            return unknown_web_page_assessment(
                assessment, reason=WebPageClassificationReason.STRONG_NEGATIVE_CONFLICT
            )
        corroborated = (
            assessment.confidence >= self._minimum_confidence
            and structure_supported
            and (
                _matching_classification_hint(
                    capture.classification_hint,
                    page_type=assessment.page_type,
                    minimum_confidence=self._minimum_confidence,
                )
                or _strong_home_list_corroboration(
                    page_role=capture.page_role,
                    page_type=assessment.page_type,
                    confidence=assessment.confidence,
                    facts=capture.structure_facts,
                )
            )
        )
        hint_conflict = (
            capture.classification_hint is not None
            and assessment.page_type != capture.classification_hint.expected_page_type
            and capture.classification_hint.confidence >= self._minimum_confidence
            and capture.classification_hint.source != _DETAIL_TRANSITION_HINT_SOURCE
        )
        if hint_conflict:
            assessment = WebPageAssessment(
                page_type=assessment.page_type,
                raw_page_type=assessment.raw_page_type,
                proposed_page_type=assessment.proposed_page_type,
                observed_page_type=assessment.observed_page_type,
                expected_page_type=assessment.expected_page_type,
                confidence=assessment.confidence,
                evidence=assessment.evidence,
                needs_review_reason=assessment.needs_review_reason,
                conflict_reason_code=(WebPageClassificationReason.URL_HINT_DOM_CONFLICT.value),
                diagnostic_message=safe_web_diagnostic_message(
                    assessment.needs_review_reason or assessment.diagnostic_message
                ),
                reason_codes=assessment.reason_codes,
                conflicts=assessment.conflicts,
                needs_review=assessment.needs_review,
                guide_version=assessment.guide_version,
                model_result=assessment.model_result,
                deterministic_result=assessment.deterministic_result,
            )
        machine_supported_home = (
            capture.page_role == "home"
            and assessment.page_type == "home"
            and rule_evaluation is not None
            and structure_supported
            and not assessment.conflicts
            and assessment.confidence >= self._minimum_confidence
        )
        if assessment.needs_review_reason is not None or assessment.needs_review:
            if hint_conflict or (not corroborated and not machine_supported_home):
                return _needs_review(
                    assessment,
                    WebPageClassificationReason.MODEL_NEEDS_REVIEW,
                    diagnostic_message=assessment.needs_review_reason,
                )
            assessment = WebPageAssessment(
                page_type=assessment.page_type,
                raw_page_type=assessment.raw_page_type,
                proposed_page_type=assessment.proposed_page_type,
                observed_page_type=assessment.observed_page_type,
                expected_page_type=assessment.expected_page_type,
                confidence=assessment.confidence,
                evidence=assessment.evidence,
                # 首页达到正式阈值且机器门通过时，模型软复核仅留审计。
                diagnostic_message=(
                    safe_web_diagnostic_message(assessment.needs_review_reason)
                    if machine_supported_home else assessment.diagnostic_message
                ),
                reason_codes=(
                    (*assessment.reason_codes, "WEB_PAGE_TYPE_MODEL_REVIEW_CORROBORATED")
                    if machine_supported_home else assessment.reason_codes
                ),
                conflicts=assessment.conflicts,
                guide_version=assessment.guide_version,
                model_result=assessment.model_result,
                deterministic_result=assessment.deterministic_result,
            )
        if assessment.confidence < self._minimum_confidence:
            return _needs_review(
                assessment,
                WebPageClassificationReason.LOW_CONFIDENCE,
            )
        return assessment


def enforce_web_formal_confidence_floor(assessment: WebPageAssessment) -> WebPageAssessment:
    """签发与持久化出口统一防御：保留候选/原分，低于硬下限不得正式化。"""
    if (
        assessment.page_type not in {None, "unknown"}
        and assessment.confidence < WEB_FORMAL_PAGE_TYPE_MINIMUM_CONFIDENCE
    ):
        return _needs_review(
            replace(
                assessment,
                proposed_page_type=assessment.proposed_page_type or assessment.page_type,
            ),
            WebPageClassificationReason.LOW_CONFIDENCE,
            diagnostic_message=assessment.needs_review_reason,
        )
    return assessment


def _needs_review(
    assessment: WebPageAssessment,
    reason: WebPageClassificationReason,
    *,
    diagnostic_message: str | None = None,
    conflicts: tuple[str, ...] = (),
) -> WebPageAssessment:
    legacy_frozen = bool(re.search(r"\.web\.v[1-7]$", assessment.guide_version or ""))
    return WebPageAssessment(
        page_type=None if legacy_frozen else "unknown",
        raw_page_type=assessment.raw_page_type,
        proposed_page_type=assessment.proposed_page_type if legacy_frozen else "unknown",
        observed_page_type=assessment.observed_page_type,
        expected_page_type=assessment.expected_page_type,
        confidence=assessment.confidence,
        evidence=assessment.evidence,
        needs_review_reason=reason.value,
        conflict_reason_code=assessment.conflict_reason_code,
        diagnostic_message=safe_web_diagnostic_message(
            diagnostic_message or assessment.diagnostic_message
        ),
        reason_codes=tuple(dict.fromkeys((*assessment.reason_codes, reason.value))),
        conflicts=tuple(dict.fromkeys((*assessment.conflicts, *conflicts))),
        needs_review=True,
        guide_version=assessment.guide_version,
        subject_region_facts=assessment.subject_region_facts,
        structure_fingerprint=assessment.structure_fingerprint,
        decision_source=assessment.decision_source,
        model_result=assessment.model_result,
        deterministic_result=assessment.deterministic_result,
    )


def unknown_web_page_assessment(
    assessment: WebPageAssessment,
    *,
    reason: WebPageClassificationReason,
    conflicts: tuple[str, ...] = (),
) -> WebPageAssessment:
    """保留原始候选与证据，签发可持久化的未知页面终态。"""

    return _needs_review(assessment, reason, conflicts=conflicts)


def safe_web_diagnostic_message(value: str | None) -> str | None:
    """只保留脱敏、限长的模型诊断，不允许其充当原因码。"""

    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return None
    normalized = _SENSITIVE_DIAGNOSTIC_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[redacted]",
        normalized,
    )
    normalized = _BEARER_DIAGNOSTIC.sub("Bearer [redacted]", normalized)
    return normalized[:300]


def safe_web_page_type_candidate(value: str | None) -> str | None:
    """保留目录外候选用于审计，但不得携带敏感值或无界文本。"""

    diagnostic = safe_web_diagnostic_message(value)
    return diagnostic[:80] if diagnostic is not None else None


def _allowed_page_types_for_role(
    catalog_codes: tuple[str, ...],
    *,
    page_role: str,
) -> tuple[str, ...]:
    """按工作流已知页面角色收紧模型闭集，同时保留菜单直达详情。"""

    if page_role == "detail":
        # 详情动作可能被站点路由到另一张列表页；分类必须保留真实页型，
        # 再由详情导航器记录 mismatch，不能把确定的列表误升级为人工不确定。
        allowed = tuple(
            code for code in catalog_codes
            if code not in {"age_gate", "ad_page", "guide_page"}
        )
    else:
        # 页面角色只限制探索上下文，正式页型由对应冻结目录定义。
        allowed = tuple(
            code for code in catalog_codes
            if code not in {"age_gate", "ad_page", "guide_page"}
        )
    if not allowed:
        raise ValueError("Web 页面角色没有可用的 Catalog 类型")
    return allowed


def _matching_classification_hint(
    hint: WebPageClassificationHint | None,
    *,
    page_type: str,
    minimum_confidence: float,
) -> bool:
    """只让高置信且非混合提示补充 DOM 已证明的页面轴。"""

    return bool(
        hint is not None
        and hint.expected_page_type == page_type
        and hint.confidence >= minimum_confidence
        and not hint.mixed_content
    )


def _trusted_nonroot_home_video_list(
    *,
    capture: WebReadyPageCapture,
    assessment: WebPageAssessment,
    model_status: str,
    evaluations: Mapping[str, WebPageRuleEvaluation],
    minimum_confidence: float,
) -> bool:
    """非根菜单只有独立媒体卡证据充分时才能纠正模型首页误判。"""
    facts = capture.structure_facts
    regions = facts.subject_region_facts
    home = evaluations.get("home")
    video_list = evaluations.get("video_list")
    if (
        capture.page_role != "menu"
        or not capture.url
        or is_web_home_url(capture.url)
        or facts.is_canonical_root_url
        or model_status != "available"
        or assessment.page_type != "home"
        or assessment.confidence < minimum_confidence
        or assessment.needs_review
        or assessment.needs_review_reason
        or assessment.conflicts
        or capture.classification_hint is not None
        or (capture.inspection_readiness is not None
            and not capture.inspection_readiness.can_serve_evidence)
        or home is None
        or home.strong_conflicts
        or home.missing_facts != ("is_canonical_root_url:eq:True",)
        or video_list is None
        or not video_list.required_satisfied
        or video_list.strong_conflicts
        or "media_card_count:gte:2" not in video_list.positive_hits
        or regions is None
    ):
        return False
    if (
        facts.media_card_count < 2
        or facts.repeated_primary_card_count < 2
        or facts.media_card_count < facts.repeated_primary_card_count
        or facts.video_specific_card_count < facts.repeated_primary_card_count
        or facts.primary_detail_link_count < 2
        or facts.single_subject_element_count
        or facts.player_root_count
        or facts.primary_video_element_count
        or facts.confirmed_primary_video_resource_count
        or facts.article_body_count
        or facts.transaction_element_count
        or regions.primary_subject_count
        or regions.primary_player_count
        or regions.primary_article_body_count
    ):
        return False
    # 内容列表与合集的宽泛规则只证明重复卡片；不能覆盖视频卡与详情链接的独立媒体证明。
    for code in ("content_detail", "video_detail", "activity", "search_page"):
        candidate = evaluations.get(code)
        if (
            candidate is not None
            and candidate.required_satisfied
            and not candidate.strong_conflicts
        ):
            return False
    return True


def _trusted_short_video_home(
    *,
    capture: WebReadyPageCapture,
    assessment: WebPageAssessment,
    model_status: str,
    evaluations: Mapping[str, WebPageRuleEvaluation],
    minimum_confidence: float,
) -> bool:
    """已选中可信根页导航且有连续候选时，容许当前播放项无并列主卡。"""

    facts = capture.structure_facts
    regions = facts.subject_region_facts
    home = evaluations.get("home")
    hint = capture.classification_hint
    hint_conflict = hint is not None and (
        hint.mixed_content
        or (hint.confidence >= minimum_confidence and hint.expected_page_type != "home")
    )
    if (
        capture.page_role != "home"
        or not capture.url
        or not is_web_home_url(capture.url)
        or model_status != "available"
        or assessment.page_type != "home"
        or assessment.confidence < 0.6
        or assessment.conflicts
        or hint_conflict
        or (capture.inspection_readiness is not None
            and not capture.inspection_readiness.can_serve_evidence)
        or home is None
        or not home.required_satisfied
        or home.strong_conflicts
        or regions is None
    ):
        return False
    return bool(
        facts.is_canonical_root_url
        and facts.active_home_navigation
        and facts.top_level_obstruction_count == 0
        and facts.top_navigation_container_count >= 1
        and facts.visible_category_entry_count >= _SHORT_VIDEO_HOME_MINIMUM_NAV_ENTRIES
        and facts.primary_detail_link_count >= _SHORT_VIDEO_HOME_MINIMUM_NAV_ENTRIES
        and facts.repeated_link_count >= _SHORT_VIDEO_HOME_MINIMUM_NAV_ENTRIES
        and facts.player_root_count >= 1
        and facts.continuous_video_feed_count >= 2
        and regions.primary_subject_count == 1
        and regions.primary_subject_kind == "video"
        and regions.primary_player_count == 1
        and regions.primary_article_body_count == 0
        and regions.repeated_primary_card_count < _HOME_VIDEO_FEED_MINIMUM_CARDS
    )


def _trusted_home_video_feed(
    *,
    capture: WebReadyPageCapture,
    assessment: WebPageAssessment,
    model_status: str,
    evaluations: Mapping[str, WebPageRuleEvaluation],
    minimum_confidence: float,
) -> bool:
    """仅在模型低分误认详情且根页导航与重复内容同时成立时补签首页。"""

    facts = capture.structure_facts
    regions = facts.subject_region_facts
    home = evaluations.get("home")
    detail = evaluations.get("video_detail")
    hint = capture.classification_hint
    hint_conflict = hint is not None and (
        hint.mixed_content
        or (hint.confidence >= minimum_confidence and hint.expected_page_type != "home")
    )
    unsafe_readiness = (
        capture.inspection_readiness is not None
        and not capture.inspection_readiness.can_serve_evidence
    )
    if (
        capture.page_role != "home"
        or not capture.url
        or not is_web_home_url(capture.url)
        or model_status != "available"
        or assessment.page_type != "video_detail"
        or assessment.confidence >= minimum_confidence
        or any(
            conflict not in {
                "single_player_conflicts_with_home",
                "repeated_cards_conflict_with_detail",
            }
            for conflict in assessment.conflicts
        )
        or hint_conflict
        or unsafe_readiness
        or home is None
        or not home.required_satisfied
        or home.strong_conflicts
        or detail is None
        or "repeated_cards_conflict_with_detail" not in detail.strong_conflicts
        or regions is None
    ):
        return False
    return bool(
        facts.is_canonical_root_url
        and facts.active_home_navigation
        and facts.top_level_obstruction_count == 0
        and facts.top_navigation_container_count >= 1
        and facts.visible_category_entry_count >= 3
        and facts.repeated_primary_card_count >= _HOME_VIDEO_FEED_MINIMUM_CARDS
        and facts.primary_detail_link_count >= _HOME_VIDEO_FEED_MINIMUM_CARDS
        and facts.repeated_link_count >= _HOME_VIDEO_FEED_MINIMUM_CARDS
        and facts.player_root_count >= 1
        and regions.primary_subject_count == 1
        and regions.primary_subject_kind == "video"
        and regions.primary_player_count == 1
        and regions.repeated_primary_card_count >= _HOME_VIDEO_FEED_MINIMUM_CARDS
    )


def _strong_home_list_corroboration(
    *,
    page_role: str,
    page_type: str,
    confidence: float,
    facts: WebPageStructureFacts,
) -> bool:
    """用根 URL 与强可信聚合 DOM 佐证视觉结论，不把任一来源当白名单。"""

    if page_role != "home" or confidence < _STRONG_HOME_LIST_MINIMUM_CONFIDENCE:
        return False
    if page_type == "home":
        return bool(
            facts.is_canonical_root_url
            and facts.top_level_obstruction_count == 0
            and facts.top_navigation_container_count >= 1
            and facts.repeated_primary_card_count >= 2
            and facts.repeated_link_count >= 2
            and (facts.homepage_section_count >= 2 or facts.visible_category_entry_count >= 2)
        )
    if (
        page_type not in WEB_LIST_PAGE_TYPES
        or facts.primary_detail_link_count < _STRONG_HOME_LIST_MINIMUM_CARDS
        or facts.repeated_primary_card_count < _STRONG_HOME_LIST_MINIMUM_CARDS
        or facts.repeated_link_count < facts.repeated_primary_card_count
    ):
        return False
    if page_type == "video_list":
        return (
            facts.video_element_count > 0
            or facts.media_card_count >= _STRONG_HOME_LIST_MINIMUM_CARDS // 2
        )
    return True


def web_page_structure_facts(
    snapshot: Mapping[str, object],
    *,
    origin: str | None = None,
    current_url: str | None = None,
    page_role: str | None = None,
    element_geometry: Mapping[str, object] | None = None,
    navigation_sources: tuple[Mapping[str, object], ...] = (),
) -> WebPageStructureFacts:
    """只读取可信快照字段，产生有界且可审计的分类结构事实。"""

    raw_elements = snapshot.get("elements")
    elements = list(raw_elements) if isinstance(raw_elements, list) else []
    from services.api.app.application.web_ranked_video_structure import ranked_video_row_count
    from services.api.app.application.web_search_structure_facts import (
        web_article_auxiliary_nodes,
        web_secondary_region_nodes,
    )

    # 标题边界来自同一次浏览器求值，不能从正文文案或跨帧 DOM 猜测推荐归属。
    supplements = {node["selector"]: node for node in (
        *web_secondary_region_nodes(snapshot), *web_article_auxiliary_nodes(snapshot),
    )}
    elements = [
        dict(node, **supplements[node["selector"]])
        if isinstance(node, Mapping) and node.get("selector") in supplements else node
        for node in elements
    ]
    known = {node.get("selector") for node in elements if isinstance(node, Mapping)}
    elements.extend(node for key, node in supplements.items() if key not in known)
    navigation_assessment = assess_navigation_container(snapshot)
    navigation_facts = navigation_assessment.facts
    saved_navigation = snapshot.get("web_navigation")
    if isinstance(saved_navigation, Mapping) and isinstance(
        saved_navigation.get("structure_facts"), Mapping
    ):
        raw_facts = saved_navigation["structure_facts"]
        # 只消费 Capture 策略生成的完整有限计数；畸形历史摘要回退当前快照事实。
        expected = WebNavigationStructureFacts.__dataclass_fields__
        if set(raw_facts) == set(expected) and all(
            type(value) is bool
            if key == "navigation_obstructed"
            else type(value) is int and value >= 0
            for key, value in raw_facts.items()
        ):
            navigation_facts = WebNavigationStructureFacts(**raw_facts)
    navigation_count = 0
    repeated_links = 0
    video_count = 0
    audio_count = 0
    single_subject_count = 0
    article_body_count = 0
    paragraph_text_length = 0
    player_root_count = 0
    advertisement_count = 0
    transaction_count = 0
    obstruction_count = int(snapshot.get("human_verification_present") is True)
    homepage_sections: set[str] = set()
    paragraph_scopes: set[str] = set()
    repeated_subject_scopes: set[str] = set()
    paragraphs: list[tuple[str, str, bool, set[str]]] = []
    generic_main_scopes: set[str] = set()
    repeated_subject_count = 0
    primary_subject_tokens: set[str] = set()
    primary_video_count = 0
    primary_elements: list[Mapping[str, object]] = []
    trusted_detail_candidates = ()
    if origin and current_url:
        candidate_snapshot = dict(snapshot)
        candidate_snapshot.setdefault("capture_id", "web-structure-facts")
        try:
            trusted_detail_candidates = detail_candidates_from_ready_snapshot(
                candidate_snapshot,
                list_page_type="content_list",
                origin=origin,
                current_url=current_url,
            )
        except (TypeError, ValueError):
            trusted_detail_candidates = ()
    tokens: set[str] = set()
    body_scope = WebSnapshotBodyScopes(elements)
    # 已证明单篇正文时，推荐区域不得继续进入模型的主列表计数。
    from services.api.app.application.web_subject_region_scopes import SubjectRegions

    subject_regions = SubjectRegions(elements)
    secondary_recommendations = (
        subject_regions.recommendations if subject_regions.bodies else set()
    )
    trusted_detail_candidates = tuple(
        item for item in trusted_detail_candidates
        if not subject_regions.within(
            item.selector,
            secondary_recommendations | subject_regions.sidebar,
        )
    )
    card_scopes = body_scope.card_scopes(tuple(item.selector for item in trusted_detail_candidates))
    for raw in elements:
        if not isinstance(raw, Mapping) or raw.get("visible") is not True:
            continue
        if raw.get("is_advertisement") is True:
            advertisement_count += 1
        if (
            raw.get("web_obstruction_layer_structural") is True
            or raw.get("aria_modal") is True
            or raw.get("web_human_verification_present") is True
        ):
            obstruction_count += 1
        tag = str(raw.get("tag") or "").casefold()
        role = str(raw.get("role") or "").casefold()
        within_web_navigation = raw.get("within_web_navigation")
        # 显式 Web 布尔事实覆盖旧通用标记，前置过滤与正文计数必须同口径。
        within_navigation = (
            within_web_navigation is True
            if isinstance(within_web_navigation, bool)
            else raw.get("within_navigation") is True
        )
        raw_tokens = (
            raw.get("class_tokens"),
            raw.get("ancestor_class_tokens"),
        )
        element_tokens = {
            str(item).strip().casefold()[:100]
            for values in raw_tokens
            if isinstance(values, list)
            for item in values
            if str(item).strip()
        }
        selector = str(raw.get("selector") or "").strip()
        own_tokens = (
            {
                str(item).strip().casefold()
                for item in raw.get("class_tokens", [])
                if isinstance(item, str)
            }
            if isinstance(raw.get("class_tokens"), list)
            else set()
        )
        # 只读取当前节点的显式 id/class 选择器，不能从属性值或任意子串推断主体。
        selector_tail = re.split(r"\s+|[>+~]", selector)[-1]
        selector_identity = re.fullmatch(
            r"(?:[a-z][\w-]*)?[.#]([a-z][\w-]*)", selector_tail, re.IGNORECASE
        )
        if selector_identity:
            own_tokens.add(selector_identity.group(1).casefold())
        if within_navigation or tag == "nav" or role == "navigation":
            navigation_count += 1
        if (
            any(
                raw.get(name) is True
                for name in (
                    "is_advertisement",
                    "within_footer",
                    "within_pagination",
                    "within_breadcrumb",
                )
            )
            or within_navigation
            or tag == "nav"
            or role == "navigation"
            or subject_regions.within(selector, secondary_recommendations)
        ):
            continue
        primary_elements.append(raw)
        if tag == "section" and raw.get("within_repeated_structure") is not True:
            homepage_sections.add(selector or str(len(homepage_sections)))
        if selector:
            # selector 由浏览器侧依据真实 DOM 生成，可补足只有 id、没有语义 class 的正文根节点。
            tokens.add(selector.casefold()[:300])
        tokens.update(element_tokens)
        tokens.update(own_tokens)
        tokens.add(tag)
        tokens.add(role)
        repeated = raw.get("within_repeated_structure") is True
        repeat_size = raw.get("repeat_group_size")
        if isinstance(repeat_size, int) and not isinstance(repeat_size, bool):
            repeated = repeated or repeat_size >= 2
        if (
            repeated
            and (tag == "a" or role == "link")
            and raw.get("interactive") is True
            and not within_navigation
            and raw.get("within_pagination") is not True
            and raw.get("within_breadcrumb") is not True
            and raw.get("is_advertisement") is not True
        ):
            repeated_links += 1
        media_parent_tag = str(raw.get("media_parent_tag") or "").casefold()
        if tag == "video" or (tag == "source" and media_parent_tag == "video"):
            video_count += 1
            if not repeated:
                primary_video_count += 1
        if tag == "audio" or (tag == "source" and media_parent_tag == "audio"):
            audio_count += 1
        if any(
            marker in own_tokens
            for marker in ("payment", "purchase", "checkout", "subscribe", "paywall")
        ):
            transaction_count += 1
        is_player_root = tag == "video" or any(
            marker in own_tokens
            for marker in (
                "video-player",
                "video-js",
                "player-root",
                "playback",
                "episode-grid",
                "episode-list",
            )
        )
        is_article_root = is_web_article_body_node(raw)
        if repeated and (is_player_root or is_article_root):
            repeated_subject_count += 1
            if selector:
                repeated_subject_scopes.add(selector)
        if not repeated and (is_article_root or tag == "main") and selector:
            paragraph_scopes.add(selector)
            if tag == "main" and not is_article_root:
                generic_main_scopes.add(selector)
        if not repeated and (is_article_root or is_player_root):
            primary_subject_tokens.update(own_tokens)
        if is_player_root and not repeated:
            player_root_count += 1
        if not repeated and (
            is_player_root
            or tag == "main"
            or is_article_root
        ):
            single_subject_count += 1
        if not repeated and is_article_root:
            article_body_count += 1
        if tag == "p":
            paragraphs.append(
                (selector, str(raw.get("text") or "").strip(), repeated, element_tokens)
            )
    inferred_article_scopes: set[str] = set()
    for selector, _text, _repeated, element_tokens in paragraphs:
        ancestors = body_scope.ancestors(selector)
        article_scope = next(
            (
                scope
                for scope in ancestors
                if is_web_article_body_node(body_scope.nodes.get(scope, {}))
            ),
            None,
        )
        if article_scope is None and element_tokens & ARTICLE_TOKENS:
            article_scope = ancestors[0] if ancestors else None
        if article_scope:
            inferred_article_scopes.add(article_scope)
    missing_article_scopes = inferred_article_scopes - paragraph_scopes
    if missing_article_scopes:
        paragraph_scopes.update(missing_article_scopes)
        article_body_count += len(missing_article_scopes)
        single_subject_count += len(missing_article_scopes)
    proven_body_lengths: dict[str, int] = {}
    for selector, text, repeated, _element_tokens in paragraphs:
        # 同级正文段落也有重复标记，只有重复业务主体范围才应排除。
        within_body = body_scope.within(selector, paragraph_scopes)
        within_preview = body_scope.within(selector, repeated_subject_scopes | card_scopes)
        if not within_preview and (within_body or not repeated):
            paragraph_text_length += len(text)
            for scope in paragraph_scopes:
                if body_scope.within(selector, {scope}):
                    # 通用 main 仅组织页面，正文证据归属段落实际父子树，不能扩散到兄弟播放器。
                    ancestors = body_scope.ancestors(selector)
                    body_root = (
                        ancestors[0] if scope in generic_main_scopes and ancestors else scope
                    )
                    proven_body_lengths[body_root] = (
                        proven_body_lengths.get(body_root, 0) + len(text)
                    )
    if paragraph_text_length == 0:
        # 空 main 或只包含重复卡片的 main 不是单篇正文主体。
        single_subject_count -= len(generic_main_scopes)
    subject_facts = primary_subject_facts(
        primary_elements,
        proven_body_scopes={
            scope for scope, length in proven_body_lengths.items() if length >= 120
        },
    )
    from services.api.app.application.web_search_structure_facts import web_search_structure_facts

    # 同帧专项普查避免广告/导航挤占通用节点预算；不替代模型、清障和置信度门禁。
    for name, count in web_search_structure_facts(snapshot).items():
        subject_facts[name] = max(subject_facts[name], count)
    single_subject_count += subject_facts["image_gallery_root_count"]
    if subject_facts["confirmed_primary_video_resource_count"]:
        single_subject_count = max(single_subject_count, 1)
    regions = extract_subject_region_facts(
        elements, element_geometry=element_geometry, navigation_sources=navigation_sources
    )
    recovered = recover_truncated_detail_media(
        snapshot,
        elements=elements,
        regions=regions,
        detail_candidates=trusted_detail_candidates,
        page_role=page_role,
        current_url=current_url,
    )
    if recovered is not None:
        regions = recovered.regions
        video_count = max(video_count, 1)
        primary_video_count = max(primary_video_count, 1)
        player_root_count = max(player_root_count, 1)
        single_subject_count = max(single_subject_count, 1)
        subject_facts["confirmed_primary_video_resource_count"] = max(
            subject_facts["confirmed_primary_video_resource_count"], 1
        )
        tokens.add("video")
        if recovered.lower_cards_demoted:
            trusted_detail_candidates = ()
    return WebPageStructureFacts(
        subject_region_facts=regions,
        # 稀疏通用节点可能不含 Web 导航标记；已验证的同帧顶部分类入口仍须计入。
        navigation_element_count=max(
            navigation_count, navigation_facts.visible_category_entry_count
        ),
        repeated_link_count=repeated_links,
        video_element_count=video_count,
        single_subject_element_count=single_subject_count,
        dom_tokens=tuple(sorted(item for item in tokens if item)),
        audio_element_count=audio_count,
        primary_detail_link_count=len(trusted_detail_candidates),
        repeated_primary_card_count=len(trusted_detail_candidates),
        media_card_count=sum(1 for item in trusted_detail_candidates if item.is_media_card),
        video_specific_card_count=sum(
            1 for item in trusted_detail_candidates if item.is_video_specific_card
        ),
        article_body_count=article_body_count,
        paragraph_text_length=paragraph_text_length,
        player_root_count=player_root_count,
        advertisement_element_count=advertisement_count,
        transaction_element_count=transaction_count,
        is_canonical_root_url=bool(current_url and is_web_home_url(current_url)),
        active_home_navigation=has_trusted_active_home_navigation(
            snapshot, navigation_assessment, current_url=current_url
        ),
        top_level_obstruction_count=obstruction_count,
        top_navigation_container_count=navigation_facts.top_navigation_container_count,
        visible_category_entry_count=navigation_facts.visible_category_entry_count,
        navigation_structure_facts=navigation_facts,
        homepage_section_count=len(homepage_sections),
        repeated_subject_element_count=repeated_subject_count,
        continuous_video_feed_count=continuous_video_feed_count(snapshot),
        ranked_video_row_count=ranked_video_row_count(
            snapshot, origin=origin, current_url=current_url
        ),
        primary_subject_dom_tokens=tuple(sorted(primary_subject_tokens)),
        primary_video_element_count=primary_video_count,
        **subject_facts,
    )


def _structure_facts_payload(facts: WebPageStructureFacts) -> dict[str, object]:
    return asdict(facts)


def _knowledge_payload(value: object) -> object:
    """将冻结目录知识还原为评估端可消费的普通载荷。"""

    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _knowledge_payload(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _knowledge_payload(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_knowledge_payload(item) for item in value)
    return value


def _classification_hint_payload(
    hint: WebPageClassificationHint | None,
) -> dict[str, object] | None:
    if hint is None:
        return None
    return {
        "expected_page_type": hint.expected_page_type,
        "confidence": hint.confidence,
        "source": hint.source,
        "mixed_content": hint.mixed_content,
        "authoritative": False,
    }


def _public_catalog(catalog: PageTypeCatalog) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "code": item.code,
            "name": item.name,
            "runtime_traits": list(item.runtime_traits),
            "purpose": item.purpose,
            "required_positive_signals": list(item.required_positive_signals),
            "supporting_signals": list(item.supporting_signals),
            "strong_negative_signals": list(item.strong_negative_signals),
            "confusable_with": list(item.confusable_with),
            "missing_evidence_actions": list(item.missing_evidence_actions),
            "signals": {
                "ocr_keywords": list(item.signals.ocr_keywords),
                "dom_hints": list(item.signals.dom_hints),
                "layout_hints": list(item.signals.layout_hints),
            },
        }
        for item in catalog.page_types
    )


__all__ = [
    "WebPageAssessment",
    "WebPageAssessmentPort",
    "WebPageClassifier",
    "WebPageStructureFacts",
    "WebReadyPageCapture",
    "enforce_web_formal_confidence_floor",
    "unknown_web_page_assessment",
    "web_page_structure_facts",
]
