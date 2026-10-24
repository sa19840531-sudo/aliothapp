"""Web v4 业务、生命周期与待复核页型的直接持久化出口。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from services.api.app.application.page_type_judgements import PageTypeJudgementLedger
from services.api.app.application.web_age_gate import (
    WebAgeGateAssessment,
    WebAgeGateEvidence,
    WebAgeGateEvidenceReader,
)
from services.api.app.application.web_detail_classification_policy import (
    classification_decision_audit,
)
from services.api.app.application.web_logical_page import web_logical_page_id
from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebReadyPageCapture,
    enforce_web_formal_confidence_floor,
    safe_web_page_type_candidate,
)
from services.api.app.application.web_product_capability_assessment import (
    assess_web_product_capabilities,
)
from services.api.app.application.web_readiness_contracts import (
    WebEvidenceRole,
    WebReadinessStatus,
)
from services.api.app.application.web_stale_candidate_recovery import (
    web_recovery_evidence_matches,
)
from services.api.app.application.web_subject_classification_audit import (
    subject_classification_audit,
)
from services.api.app.domain.exploration.page_type_assessment import (
    PageTypeAssessmentStatus,
    build_page_type_assessment,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.page.web_page_catalog import (
    WEB_BUSINESS_PAGE_TYPE_CODES,
    WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES,
    WEB_LEGACY_PAGE_TYPE_CODES,
    WEB_LIFECYCLE_PAGE_TYPE_CODES,
    WEB_PAGE_TYPE_CODES,
    WEB_V3_BUSINESS_PAGE_TYPE_CODES,
    load_web_page_type_catalog,
    parse_web_page_type_catalog,
)
from services.api.app.domain.page.web_url_identity import (
    DEFAULT_WEB_URL_IDENTITY_POLICY,
    WebUrlIdentityPolicy,
)
from services.api.app.domain.protocol import stable_protocol_hash

WEB_PAGE_TYPE_ASSESSMENT_VERSION = "web-page-type-assessment.v4"
WEB_CANONICAL_PAGE_TYPES = frozenset(WEB_BUSINESS_PAGE_TYPE_CODES)
_UNCLASSIFIED_PAGE_TYPE = "__unclassified__"


@dataclass(frozen=True, kw_only=True)
class WebPageTypeCatalogIdentity:
    """Run 冻结 Web Catalog 完整规范正文的不可变身份。"""

    catalog_version: str
    catalog_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.catalog_version, str)
            or not self.catalog_version.strip()
            or self.catalog_version != self.catalog_version.strip()
        ):
            raise ValueError("冻结 Web Catalog version 无效")
        digest = self.catalog_sha256.removeprefix("sha256:")
        if (
            not self.catalog_sha256.startswith("sha256:")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("冻结 Web Catalog SHA-256 无效")

    @classmethod
    def from_frozen_catalog_payload(
        cls, payload: Mapping[str, object]
    ) -> WebPageTypeCatalogIdentity:
        """验证冻结目录，并对完整规范 JSON 正文取摘要。"""

        catalog = parse_web_page_type_catalog(payload, legacy_frozen=True)
        return cls(
            catalog_version=catalog.version,
            catalog_sha256="sha256:" + stable_protocol_hash(payload),
        )


class WebPageTypeAssessmentRepository(Protocol):
    """保存 Web 页型终态与 Capture 审计事实。"""

    async def persist_web_page_type_assessment(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        canonical_page_type: str | None,
        audit_metadata: Mapping[str, object],
        assessment_payload: Mapping[str, object],
    ) -> str | None: ...


@dataclass(frozen=True, kw_only=True)
class WebPageTypeAssessmentResult:
    classification_status: str
    final_page_type: str | None
    proposed_page_type: str | None
    assessment_record_id: str | None


class WebAgeGatePageTypeRecorder:
    """在任何确认动作前，将原始年龄门 Capture 固化为生命周期事实。"""

    def __init__(
        self, *, repository: WebPageTypeAssessmentRepository,
        evidence_reader: WebAgeGateEvidenceReader | None = None,
        catalog_identity: WebPageTypeCatalogIdentity | None = None,
    ) -> None:
        self._repository = repository
        self._evidence_reader = evidence_reader
        self._catalog_identity = catalog_identity

    async def record(
        self, *, evidence: WebAgeGateEvidence, assessment: WebAgeGateAssessment
    ) -> WebPageTypeAssessmentResult:
        if self._catalog_identity is None:
            raise ValueError("Web v4 年龄门判定缺少 Run 冻结 Web Catalog 身份")
        if (not assessment.gate_present or assessment.human_verification_present
                or assessment.confidence < 0.7):
            raise ValueError("年龄门正式分类需要非人机验证且置信度达到 0.7")
        if evidence.captured_at is None and self._evidence_reader is not None:
            # 就绪协调器的旧投影不含时间；仅从同归属的可信 Capture 补齐，绝不取当前时钟。
            trusted = await self._evidence_reader.read(capture_id=evidence.capture_id)
            if (not web_recovery_evidence_matches(trusted, original=evidence,
                    expected_capture_id=evidence.capture_id)
                    or trusted.screenshot_artifact_id != evidence.screenshot_artifact_id
                    or trusted.dom_artifact_id != evidence.dom_artifact_id):
                raise ValueError("年龄门补齐证据归属不一致")
            evidence = replace(evidence, captured_at=trusted.captured_at, url=trusted.url)
        if evidence.captured_at is None:
            raise ValueError("年龄门可信 Capture 必须携带 captured_at")
        primary_artifact_ids = [evidence.screenshot_artifact_id, evidence.dom_artifact_id]
        supplemental_artifact_ids = list(evidence.supplemental_screenshot_artifact_ids)
        artifact_ids = [*primary_artifact_ids, *supplemental_artifact_ids]
        lifecycle = {
            "pre_gate_capture_id": evidence.capture_id,
            "gate_present": True,
            "dismiss_action_id": None,
            "clicked_candidate_id": None,
            "post_gate_capture_id": None,
            "gate_disappeared": False,
            "supplemental_evidence_status": evidence.supplemental_evidence_status,
            "supplemental_evidence_reason_code": evidence.supplemental_evidence_reason_code,
        }
        payload = {
            "schema_version": WEB_PAGE_TYPE_ASSESSMENT_VERSION,
            "assessment_id": "web-age-gate:" + stable_protocol_hash({
                "task_id": evidence.task_id,
                "agent_run_id": evidence.agent_run_id,
                "session_id": evidence.session_id,
                "capture_id": evidence.capture_id,
                "catalog_sha256": self._catalog_identity.catalog_sha256,
            }),
            "task_id": evidence.task_id,
            "agent_run_id": evidence.agent_run_id,
            "session_id": evidence.session_id,
            "page_type": "age_gate",
            "final_page_type": "age_gate",
            "proposed_page_type": "age_gate",
            "raw_page_type": "age_gate",
            "coverage_group": "lifecycle",
            "classification_status": "classified",
            "detection_status": "recognized",
            "status": "covered",
            "needs_review": False,
            "confidence": assessment.confidence,
            "reason_code": "WEB_AGE_GATE_CLASSIFIED",
            "reason_codes": ["WEB_AGE_GATE_CLASSIFIED"],
            "assessed_at": evidence.captured_at.isoformat(),
            "page_capture_ids": [evidence.capture_id],
            "evidence_artifact_ids": artifact_ids,
            "primary_evidence_artifact_ids": primary_artifact_ids,
            "supplemental_screenshot_artifact_ids": supplemental_artifact_ids,
            "catalog_version": self._catalog_identity.catalog_version,
            "catalog_sha256": self._catalog_identity.catalog_sha256,
            "page_states": ["blocked"],
            "web_capture_stage": "web_page_probe",
            "page_role": "lifecycle",
            "attempt_count": 1,
            **lifecycle,
        }
        record_id = await self._repository.persist_web_page_type_assessment(
            task_id=evidence.task_id, agent_run_id=evidence.agent_run_id,
            session_id=evidence.session_id, capture_id=evidence.capture_id,
            canonical_page_type="age_gate",
            assessment_payload=payload,
            audit_metadata={"web_page_type_assessment": {
                **payload, "version": WEB_PAGE_TYPE_ASSESSMENT_VERSION,
                "evidence": {"artifact_ids": artifact_ids,
                             "classifier_evidence": [assessment.reason]},
            }, "web_age_gate_lifecycle": lifecycle},
        )
        return WebPageTypeAssessmentResult(classification_status="classified",
            final_page_type="age_gate", proposed_page_type="age_gate",
            assessment_record_id=record_id)


class WebPageTypeAssessmentRecorder:
    """不经别名映射，按冻结版本记录正式页型或待复核候选。"""

    def __init__(
        self,
        *,
        repository: WebPageTypeAssessmentRepository,
        identity_policy: WebUrlIdentityPolicy = DEFAULT_WEB_URL_IDENTITY_POLICY,
        judgement_ledger: PageTypeJudgementLedger | None = None,
        assessment_schema_version: str = WEB_PAGE_TYPE_ASSESSMENT_VERSION,
        catalog_identity: WebPageTypeCatalogIdentity | None = None,
    ) -> None:
        if assessment_schema_version not in {
            "web-page-type-assessment.v2", "web-page-type-assessment.v3",
            WEB_PAGE_TYPE_ASSESSMENT_VERSION
        }:
            raise ValueError("Web assessment 协议不受支持")
        self._assessment_schema_version = assessment_schema_version
        self._repository = repository
        self._catalog_identity = catalog_identity
        self._identity_policy = identity_policy
        # 页型判断台账：模型判断与观察判断各留一条，冲突在台账里可见；不改正式评估。
        self._judgement_ledger = judgement_ledger

    async def persist(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture: WebReadyPageCapture,
        assessment: WebPageAssessment,
        attempt_count: int = 1,
    ) -> WebPageTypeAssessmentResult:
        if (
            self._assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION
            and self._catalog_identity is None
        ):
            raise ValueError("Web v4 判定缺少 Run 冻结 Web Catalog 身份")
        if isinstance(attempt_count, bool) or not 1 <= attempt_count <= 3:
            raise ValueError("attempt_count 必须在 1 到 3 之间")
        assessment = enforce_web_formal_confidence_floor(assessment)
        proposed = _normalized_page_type(
            assessment.proposed_page_type
            or assessment.page_type
            or assessment.raw_page_type
        )
        raw_page_type = safe_web_page_type_candidate(assessment.raw_page_type)
        allowed_types = _assessment_page_types(self._assessment_schema_version)
        is_v4_unknown = (
            self._assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION
            and proposed == "unknown"
        )
        final_page_type = (
            "unknown"
            if is_v4_unknown
            else proposed
            if assessment.page_type is not None
            and proposed in allowed_types
            and assessment.needs_review_reason is None
            else None
        )
        classification_status = (
            "needs_review"
            if is_v4_unknown or final_page_type is None
            else "classified"
        )
        payload_needs_review = (
            True
            if self._assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION
            and classification_status == "needs_review"
            else assessment.needs_review
        )
        guide_version = (
            self._catalog_identity.catalog_version
            if self._assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION
            else assessment.guide_version or load_web_page_type_catalog().version
        )
        logical_page_id = (
            web_logical_page_id(capture.url, identity_policy=self._identity_policy)
            if capture.url is not None else None
        )
        structure = capture.structure_facts
        inspection_readiness = capture.inspection_readiness
        product_capability_assessment = assess_web_product_capabilities(
            readiness_status=(
                inspection_readiness.status.value
                if inspection_readiness is not None
                else "unknown"
            ),
            business_visible=(
                inspection_readiness is not None
                and inspection_readiness.status is WebReadinessStatus.STABLE
            ),
            navigation_element_count=structure.navigation_element_count,
            repeated_card_count=structure.repeated_primary_card_count,
            video_element_count=(
                structure.video_element_count + structure.player_root_count
            ),
            article_body_count=structure.article_body_count,
            advertisement_element_count=structure.advertisement_element_count,
            transaction_element_count=structure.transaction_element_count,
        )
        evidence_artifact_ids = (
            capture.screenshot_artifact_id,
            capture.dom_artifact_id,
            capture.visible_text_artifact_id,
            *(
                capture.supplemental_screenshot_artifact_ids
                if self._assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION
                else ()
            ),
        )
        audit_metadata = {
            "logical_page_id": logical_page_id,
            "identity_policy_version": self._identity_policy.version,
            "capture_purpose": capture.capture_purpose,
            "menu_entry_ids": list(capture.menu_entry_ids),
            "web_product_capability_assessment": (
                product_capability_assessment.to_dict()
            ),
            "web_page_type_assessment": {
                **subject_classification_audit(capture.structure_facts.subject_region_facts),
                **classification_decision_audit(assessment),
                "version": self._assessment_schema_version,
                "page_role": capture.page_role,
                "classification_status": classification_status,
                "raw_page_type": raw_page_type,
                "proposed_page_type": proposed,
                "observed_page_type": assessment.observed_page_type,
                "expected_page_type": assessment.expected_page_type,
                "final_page_type": final_page_type,
                "confidence": assessment.confidence,
                "attempt_count": attempt_count,
                "reason_code": _stable_reason_code(assessment.needs_review_reason),
                "conflict_reason_code": assessment.conflict_reason_code,
                "diagnostic_message": assessment.diagnostic_message,
                "reason_codes": list(assessment.reason_codes),
                "conflicts": list(assessment.conflicts),
                "needs_review": payload_needs_review,
                "guide_version": guide_version,
                "evidence": {
                    "artifact_ids": list(evidence_artifact_ids),
                    "classifier_evidence": list(assessment.evidence),
                },
            }
        }
        if inspection_readiness is not None:
            audit_metadata["web_inspection_readiness"] = {
                "role": inspection_readiness.role.value,
                "status": inspection_readiness.status.value,
                "blocking_reasons": list(inspection_readiness.blocking_reasons),
            }
        if self._assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION:
            audit_metadata["web_page_type_assessment"].update(
                session_id=session_id,
                catalog_version=self._catalog_identity.catalog_version,
                catalog_sha256=self._catalog_identity.catalog_sha256,
                primary_evidence_artifact_ids=[
                    capture.screenshot_artifact_id,
                    capture.dom_artifact_id,
                    capture.visible_text_artifact_id,
                ],
                supplemental_screenshot_artifact_ids=list(
                    capture.supplemental_screenshot_artifact_ids
                ),
                supplemental_evidence_status=capture.supplemental_evidence_status,
                supplemental_evidence_reason_code=capture.supplemental_evidence_reason_code,
            )
        payload = _assessment_payload(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            capture=capture,
            assessment=assessment,
            final_page_type=final_page_type,
            proposed_page_type=proposed,
            classification_status=classification_status,
            attempt_count=attempt_count,
            assessment_schema_version=self._assessment_schema_version,
            catalog_identity=self._catalog_identity,
        )
        if (
            final_page_type in WEB_BUSINESS_PAGE_TYPE_CODES
            and inspection_readiness is not None
            and inspection_readiness.role is WebEvidenceRole.INSPECTION
            and inspection_readiness.status is WebReadinessStatus.STABLE
        ):
            payload["inspection_evidence_role"] = WebEvidenceRole.INSPECTION.value
        if self._assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION:
            classifier_evidence = audit_metadata["web_page_type_assessment"][
                "evidence"
            ]["classifier_evidence"]
            audit_metadata["web_page_type_assessment"] = {
                **payload,
                "version": self._assessment_schema_version,
                "evidence": {
                    "artifact_ids": list(evidence_artifact_ids),
                    "classifier_evidence": classifier_evidence,
                },
            }
        record_id = await self._repository.persist_web_page_type_assessment(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            capture_id=capture.capture_id,
            canonical_page_type=final_page_type,
            audit_metadata=audit_metadata,
            assessment_payload=payload,
        )
        if self._judgement_ledger is not None:
            await self._judgement_ledger.record_web_assessment(
                task_id=task_id,
                agent_run_id=agent_run_id,
                page_capture_id=capture.capture_id,
                proposed_page_type=proposed,
                observed_page_type=assessment.observed_page_type,
                final_page_type=final_page_type,
                classification_status=classification_status,
                confidence=assessment.confidence,
                reason_code=_stable_reason_code(assessment.needs_review_reason),
                conflict_reason_code=assessment.conflict_reason_code,
                expected_page_type=assessment.expected_page_type,
                evidence_artifact_ids=evidence_artifact_ids,
            )
        return WebPageTypeAssessmentResult(
            classification_status=classification_status,
            final_page_type=final_page_type,
            proposed_page_type=proposed,
            assessment_record_id=record_id,
        )


class WebPageTypeAssessmentRecordingClassifier:
    """让首页、菜单、详情及扩展业务页共用判定持久化出口。"""

    def __init__(
        self,
        *,
        classifier: object,
        recorder: WebPageTypeAssessmentRecorder,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        max_attempts_per_evidence: int = 2,
    ) -> None:
        self._classifier = classifier
        self._recorder = recorder
        self._task_id = task_id
        self._agent_run_id = agent_run_id
        self._session_id = session_id
        if isinstance(max_attempts_per_evidence, bool) or not 1 <= max_attempts_per_evidence <= 2:
            raise ValueError("max_attempts_per_evidence 必须为 1 或 2")
        self._max_attempts_per_evidence = max_attempts_per_evidence

    async def classify(self, capture: WebReadyPageCapture) -> WebPageAssessment:
        classify = getattr(self._classifier, "classify", None)
        if not callable(classify):
            raise TypeError("Web 页型分类器必须提供 classify")
        attempt_count = 0
        maximum_attempts = (
            1
            if getattr(capture, "capture_purpose", "web_page_ready_baseline")
            == "web_primary_content_evidence"
            else self._max_attempts_per_evidence
        )
        while True:
            attempt_count += 1
            assessment = enforce_web_formal_confidence_floor(await classify(capture))
            if (
                assessment.page_type is not None
                or assessment.raw_page_type is not None
                or attempt_count >= maximum_attempts
            ):
                break
        await self._recorder.persist(
            task_id=self._task_id,
            agent_run_id=self._agent_run_id,
            session_id=self._session_id,
            capture=capture,
            assessment=assessment,
            attempt_count=attempt_count,
        )
        return assessment


def _assessment_payload(
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    capture: WebReadyPageCapture,
    assessment: WebPageAssessment,
    final_page_type: str | None,
    proposed_page_type: str | None,
    classification_status: str,
    attempt_count: int,
    assessment_schema_version: str = WEB_PAGE_TYPE_ASSESSMENT_VERSION,
    catalog_identity: WebPageTypeCatalogIdentity | None = None,
) -> dict[str, object]:
    if capture.captured_at is None:
        raise ValueError("Web 可信 Capture 必须携带稳定 captured_at")
    artifact_ids = (
        capture.screenshot_artifact_id,
        capture.dom_artifact_id,
        capture.visible_text_artifact_id,
        *(
            capture.supplemental_screenshot_artifact_ids
            if assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION
            else ()
        ),
    )
    readiness = capture.inspection_readiness
    page_state = readiness.status.value if readiness is not None else "unknown"
    if assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION:
        if catalog_identity is None:
            raise ValueError("Web v4 Assessment 缺少 Run 冻结 Web Catalog 身份")
        payload = _build_web_page_type_assessment_payload(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            capture=capture,
            assessment=assessment,
            final_page_type=final_page_type,
            proposed_page_type=proposed_page_type,
            classification_status=classification_status,
            artifact_ids=artifact_ids,
            page_state=page_state,
            catalog_identity=catalog_identity,
        )
        detection_status = "recognized" if classification_status == "classified" else "needs_review"
    elif final_page_type is not None:
        payload = build_page_type_assessment(
            task_id=task_id,
            agent_run_id=agent_run_id,
            page_type=final_page_type,
            status=PageTypeAssessmentStatus.COVERED,
            reason_code="web_page_type_classified",
            assessed_at=capture.captured_at,
            page_capture_ids=(capture.capture_id,),
            evidence_artifact_ids=artifact_ids,
            page_states=(page_state,),
            registry=get_default_page_type_registry(),
        ).to_dict()
        detection_status = "recognized"
    else:
        reason_code = _stable_reason_code(assessment.needs_review_reason)
        payload = {
            "assessment_id": f"web-page-type-review:{capture.capture_id}",
            "task_id": task_id,
            "agent_run_id": agent_run_id,
            "page_type": proposed_page_type or _UNCLASSIFIED_PAGE_TYPE,
            "status": "evidence_insufficient",
            "reason_code": reason_code,
            "assessed_at": capture.captured_at.isoformat(),
            "page_capture_ids": [capture.capture_id],
            "evidence_artifact_ids": list(artifact_ids),
            "page_states": [page_state],
        }
        detection_status = "needs_review"
    payload.update(
        {
            **subject_classification_audit(capture.structure_facts.subject_region_facts),
            **classification_decision_audit(assessment),
            "schema_version": assessment_schema_version,
            "web_capture_stage": capture.web_capture_stage,
            "page_role": capture.page_role,
            "detection_status": detection_status,
            "classification_status": classification_status,
            "raw_page_type": safe_web_page_type_candidate(assessment.raw_page_type),
            "proposed_page_type": proposed_page_type,
            "observed_page_type": assessment.observed_page_type,
            "expected_page_type": assessment.expected_page_type,
            "final_page_type": final_page_type,
            "confidence": assessment.confidence,
            "attempt_count": attempt_count,
            "conflict_reason_code": assessment.conflict_reason_code,
            "diagnostic_message": assessment.diagnostic_message,
            "reason_codes": list(assessment.reason_codes),
            "conflicts": list(assessment.conflicts),
            "needs_review": (
                True
                if assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION
                and classification_status == "needs_review"
                else assessment.needs_review
            ),
            "guide_version": (
                catalog_identity.catalog_version
                if assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION
                and catalog_identity is not None
                else assessment.guide_version or load_web_page_type_catalog().version
            ),
        }
    )
    if assessment_schema_version == WEB_PAGE_TYPE_ASSESSMENT_VERSION:
        payload.update(
            supplemental_evidence_status=capture.supplemental_evidence_status,
            supplemental_evidence_reason_code=capture.supplemental_evidence_reason_code,
            primary_evidence_artifact_ids=[
                capture.screenshot_artifact_id,
                capture.dom_artifact_id,
                capture.visible_text_artifact_id,
            ],
            supplemental_screenshot_artifact_ids=list(
                capture.supplemental_screenshot_artifact_ids
            ),
        )
    return payload


def _build_web_page_type_assessment_payload(
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    capture: WebReadyPageCapture,
    assessment: WebPageAssessment,
    final_page_type: str | None,
    proposed_page_type: str | None,
    classification_status: str,
    artifact_ids: tuple[str, ...],
    page_state: str,
    catalog_identity: WebPageTypeCatalogIdentity,
) -> dict[str, object]:
    """按 Web v8 分组构造 v4 事实，不经过共享 App/H5 Registry。"""

    page_type = final_page_type or proposed_page_type or _UNCLASSIFIED_PAGE_TYPE
    if page_type in WEB_BUSINESS_PAGE_TYPE_CODES:
        coverage_group = "business"
    elif page_type in WEB_LIFECYCLE_PAGE_TYPE_CODES:
        coverage_group = "lifecycle"
    elif page_type in WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES:
        coverage_group = "evidence_only"
    else:
        coverage_group = "neither"
    status = "covered" if classification_status == "classified" else "evidence_insufficient"
    reason_code = (
        "web_page_type_classified"
        if classification_status == "classified"
        else _stable_reason_code(assessment.needs_review_reason)
    )
    return {
        "assessment_id": "web-page-type:" + stable_protocol_hash(
            {
                "task_id": task_id,
                "agent_run_id": agent_run_id,
                "capture_id": capture.capture_id,
                "page_type": page_type,
                "catalog_sha256": catalog_identity.catalog_sha256,
            }
        ),
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "session_id": session_id,
        "page_type": page_type,
        "status": status,
        "reason_code": reason_code,
        "catalog_version": catalog_identity.catalog_version,
        "catalog_sha256": catalog_identity.catalog_sha256,
        "coverage_group": coverage_group,
        "assessed_at": capture.captured_at.isoformat(),
        "page_capture_ids": [capture.capture_id],
        "evidence_artifact_ids": list(artifact_ids),
        "page_states": [page_state],
    }


def _assessment_page_types(version: str) -> frozenset[str]:
    if version == "web-page-type-assessment.v2":
        return frozenset(WEB_LEGACY_PAGE_TYPE_CODES)
    if version == "web-page-type-assessment.v3":
        return frozenset(WEB_V3_BUSINESS_PAGE_TYPE_CODES)
    if version == WEB_PAGE_TYPE_ASSESSMENT_VERSION:
        return frozenset(WEB_PAGE_TYPE_CODES)
    raise ValueError("Web assessment 协议不受支持")


def _normalized_page_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized or None


def _stable_reason_code(value: object) -> str:
    """模型自由文本不得进入 reason_code。"""

    stable_codes = {
        "WEB_PAGE_TYPE_UNSUPPORTED",
        "WEB_PAGE_TYPE_LOW_CONFIDENCE",
        "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT",
        "WEB_PAGE_STRUCTURE_INSUFFICIENT",
        "URL_HINT_DOM_CONFLICT",
        "WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW",
        "WEB_PAGE_TYPE_MODEL_PERMISSION_DENIED",
        "WEB_PAGE_TYPE_STRONG_NEGATIVE_CONFLICT",
        "WEB_PAGE_TYPE_PRIMARY_REGION_CONFLICT",
    }
    normalized = _normalized_page_type(value)
    if normalized is not None:
        candidate = normalized.upper()
        if candidate in stable_codes:
            return candidate
    return "WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW"


__all__ = [
    "WEB_PAGE_TYPE_ASSESSMENT_VERSION",
    "WEB_CANONICAL_PAGE_TYPES",
    "WebPageTypeCatalogIdentity",
    "WebPageTypeAssessmentRecorder",
    "WebAgeGatePageTypeRecorder",
    "WebPageTypeAssessmentRecordingClassifier",
    "WebPageTypeAssessmentResult",
]
