"""把持久化 Web Capture 投影为各探索阶段所需的领域证据。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from services.api.app.application.web_age_gate import WebAgeGateEvidence
from services.api.app.application.web_detail_candidate_extraction import (
    detail_candidates_from_ready_snapshot,
)
from services.api.app.application.web_evidence_admission import (
    assess_web_capture_evidence_admission,
)
from services.api.app.application.web_interaction_facts import web_capture_content_facts
from services.api.app.application.web_interaction_precision import (
    WebInteractionEvidenceMode,
    WebTargetObservation,
)
from services.api.app.application.web_list_detail_navigation import (
    WebDetailCandidate,
    WebListDetailEvidence,
    build_web_detail_target_observation,
)
from services.api.app.application.web_obstruction_geometry import (
    web_has_only_benign_ordinary_overlays,
    web_obstruction_geometry_facts,
)
from services.api.app.application.web_page_classification import (
    WebReadyPageCapture,
    web_page_structure_facts,
)
from services.api.app.application.web_page_readiness import WebPageEvidence
from services.api.app.application.web_readiness_contracts import (
    DEFAULT_WEB_READINESS_PROFILE,
    WebEvidenceRole,
    WebReadinessProfile,
    assess_web_readiness,
    read_web_readiness_assessment,
)
from services.api.app.application.web_runtime_discovery import WebCapture
from services.api.app.domain.exploration import (
    build_web_obstruction_candidates,
    web_obstruction_state_fingerprint,
)
from services.api.app.domain.page.web_url_identity import (
    DEFAULT_WEB_URL_IDENTITY_POLICY,
    WebUrlIdentityPolicy,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    WebCaptureArtifactIndex,
)


class WebCaptureEvidenceIndexReader(Protocol):
    async def read_current_capture_id(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
    ) -> str: ...

    async def matches_active_capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        capture_id: str,
        current_only: bool = False,
    ) -> bool: ...

    async def read(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> WebCaptureArtifactIndex: ...


class WebTrustedSnapshotReader(Protocol):
    async def read_ui_snapshot(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> Mapping[str, object]: ...

    async def read_media_state(
        self, *, task_id: str, page_capture_id: str
    ) -> Mapping[str, object] | None: ...


@dataclass(frozen=True, kw_only=True)
class WebRuntimeEvidenceSource:
    task_id: str
    agent_run_id: str
    session_id: str
    deadline: datetime
    index_reader: WebCaptureEvidenceIndexReader
    snapshot_reader: WebTrustedSnapshotReader
    readiness_profile: WebReadinessProfile = DEFAULT_WEB_READINESS_PROFILE
    identity_policy: WebUrlIdentityPolicy = DEFAULT_WEB_URL_IDENTITY_POLICY
    logical_target_repository: object | None = None

    async def matches_target(self, *, capture_id: str, expected_url: str) -> bool:
        if self.logical_target_repository is None:
            return False
        actual_url = await self.read_url(capture_id=capture_id)
        target = await self.logical_target_repository.find(
            task_id=self.task_id,
            agent_run_id=self.agent_run_id,
            session_id=self.session_id,
            source_capture_id=capture_id,
            url=expected_url,
        )
        return target is not None and target["logical_page_id"] == self.identity_policy.logical_id(
            actual_url
        )

    async def read_age(self, *, capture_id: str) -> WebAgeGateEvidence:
        index, snapshot = await self._read(capture_id)
        return WebAgeGateEvidence(
            task_id=self.task_id,
            agent_run_id=self.agent_run_id,
            session_id=self.session_id,
            capture_id=capture_id,
            screenshot_artifact_id=_required_artifact(index, "screenshot"),
            dom_artifact_id=_required_artifact(index, "dom"),
            candidates=build_web_obstruction_candidates(
                capture_id=capture_id,
                ui_snapshot=snapshot,
            ),
            deadline=self.deadline,
            captured_at=index.captured_at,
            url=index.url,
        )

    async def read_page(self, *, capture_id: str) -> WebPageEvidence:
        index, snapshot = await self._read(capture_id, current_only=True)
        readiness = read_web_readiness_assessment(
            index.pipeline_metrics, profile=self.readiness_profile
        )
        return WebPageEvidence(
            task_id=self.task_id,
            agent_run_id=self.agent_run_id,
            session_id=self.session_id,
            capture_id=capture_id,
            screenshot_artifact_id=_required_artifact(index, "screenshot"),
            dom_artifact_id=_required_artifact(index, "dom"),
            candidates=build_web_obstruction_candidates(
                capture_id=capture_id,
                ui_snapshot=snapshot,
            ),
            deadline=self.deadline,
            obstruction_state_fingerprint=web_obstruction_state_fingerprint(snapshot),
            benign_ordinary_overlays_only=web_has_only_benign_ordinary_overlays(snapshot),
            obstruction_geometry=(web_obstruction_geometry_facts(snapshot)
                                  if snapshot.get("web_detail_candidate_priority_enabled") is True
                                  or "web_overlay_geometry" in snapshot else None),
            readiness=read_web_readiness_assessment(
                index.pipeline_metrics,
                profile=self.readiness_profile,
            ),
            interaction_facts=replace(
                web_capture_content_facts(snapshot, url=index.url),
                identity_verified=True,
                readiness_accepted=bool(readiness and readiness.can_serve_evidence),
                evidence_mode=(
                    WebInteractionEvidenceMode.DIAGNOSTIC
                    if index.pipeline_metrics.get("diagnostic_capture") is True
                    else WebInteractionEvidenceMode.NATURAL
                ),
            ),
        )

    async def read_ready(
        self,
        *,
        capture_id: str,
        page_role: str,
    ) -> WebReadyPageCapture:
        index, snapshot = await self._read(capture_id, current_only=True)
        from services.api.app.application.web_logical_page import resolve_web_logical_page_role

        page_role = (
            resolve_web_logical_page_role(
                canonical_url=self.identity_policy.canonical(index.url), asserted_roles=(page_role,)
            )
            or "menu"
        )
        if "web_readiness" not in index.pipeline_metrics:
            raise RuntimeError("WEB_CAPTURE_READINESS_MISSING")
        readiness = read_web_readiness_assessment(
            index.pipeline_metrics,
            profile=self.readiness_profile,
        )
        admission = assess_web_capture_evidence_admission(
            index.pipeline_metrics,
            capture_role="after_action",
            profile=self.readiness_profile,
            diagnostic=index.pipeline_metrics.get("diagnostic_capture") is True,
        )
        if not admission.can_enter_formal_bundle:
            raise RuntimeError("WEB_CAPTURE_NOT_FORMAL_EVIDENCE")
        parsed = urlsplit(index.url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        structure_facts = web_page_structure_facts(
            snapshot, origin=origin, current_url=index.url, page_role=page_role
        )
        if not structure_facts.is_canonical_root_url and await self.matches_target(
            capture_id=capture_id, expected_url=origin + "/"
        ):
            # 只有同一次运行、当前 Capture 的持久化根重定向别名能建立首页事实。
            structure_facts = replace(structure_facts, is_canonical_root_url=True)
            page_role = "home"
        capture_purpose = index.capture_purpose
        web_capture_stage = str(snapshot.get("web_capture_stage") or "")
        if capture_purpose == "web_primary_content_evidence":
            web_capture_stage = capture_purpose
        return WebReadyPageCapture(
            capture_id=capture_id,
            web_capture_stage=web_capture_stage,
            screenshot_artifact_id=_required_artifact(index, "screenshot"),
            dom_artifact_id=_required_artifact(index, "dom"),
            visible_text_artifact_id=_required_artifact(index, "visible_text"),
            page_role=page_role,
            captured_at=index.captured_at,
            url=index.url,
            capture_purpose=capture_purpose,
            requested_url=index.requested_url,
            redirect_chain=index.redirect_chain,
            finalization_epoch=index.finalization_epoch,
            capture_state_fingerprint=web_capture_state_fingerprint(index),
            inspection_readiness=assess_web_readiness(
                readiness.signals,
                role=WebEvidenceRole.INSPECTION,
            ),
            structure_facts=structure_facts,
            interaction_facts=replace(
                web_capture_content_facts(snapshot, url=index.url),
                readiness_accepted=readiness.can_serve_evidence,
                identity_verified=True,
            ),
        )

    async def read_list(self, *, capture_id: str) -> WebListDetailEvidence:
        index, snapshot = await self._read(capture_id)
        parsed = urlsplit(index.url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        return WebListDetailEvidence(
            task_id=self.task_id,
            agent_run_id=self.agent_run_id,
            session_id=self.session_id,
            ready_capture_id=capture_id,
            screenshot_artifact_id=_required_artifact(index, "screenshot"),
            dom_artifact_id=_required_artifact(index, "dom"),
            origin=origin,
            current_url=index.url,
            ui_snapshot=snapshot,
            deadline=self.deadline,
        )

    async def read_url(self, *, capture_id: str) -> str:
        index = await self.index_reader.read(
            task_id=self.task_id,
            capture_id=capture_id,
        )
        if not index.url:
            raise RuntimeError("WEB_CAPTURE_FINAL_URL_MISSING")
        return index.url

    async def read_snapshot(self, *, capture_id: str) -> Mapping[str, object]:
        """读取已持久化的可信 UI Snapshot，供密封主体候选重建与动作后复验。"""

        _index, snapshot = await self._read(capture_id)
        return snapshot

    async def read_media_state(self, *, capture_id: str) -> Mapping[str, object] | None:
        """在当前 Run/Capture 身份门后读取独立媒体状态附件。"""

        await self._read(capture_id)
        return await self.snapshot_reader.read_media_state(
            task_id=self.task_id, page_capture_id=capture_id
        )

    async def read_detail_target_observation(
        self,
        *,
        capture_id: str,
        candidate: WebDetailCandidate,
        list_page_type: str,
        origin: str,
        current_url: str,
        previous_scroll_offset: float,
    ) -> WebTargetObservation:
        """滚动后从新 Capture 重建详情候选，禁止跨 epoch 复用旧 selector。"""

        index, snapshot = await self._read(capture_id, current_only=True)
        rebound_candidates = tuple(
            item
            for item in detail_candidates_from_ready_snapshot(
                snapshot,
                list_page_type=list_page_type,
                origin=origin,
                current_url=index.url or current_url,
                identity_policy=self.identity_policy,
            )
            if item.canonical_url == candidate.canonical_url
            and item.expected_page_type == candidate.expected_page_type
        )
        rebound = rebound_candidates[0] if len(rebound_candidates) == 1 else None
        observation = build_web_detail_target_observation(
            snapshot,
            capture_id=capture_id,
            candidate=rebound or candidate,
            previous_scroll_offset=previous_scroll_offset,
        )
        readiness = read_web_readiness_assessment(
            index.pipeline_metrics, profile=self.readiness_profile
        )
        facts = web_capture_content_facts(snapshot, url=index.url)
        observation = replace(
            observation,
            readiness_accepted=bool(readiness and readiness.can_serve_evidence),
            identity_verified=True,
            business_layer_visible=facts.business_layer_visible,
            content_identity=facts.content_identity,
            url=index.url,
            evidence_mode=(
                WebInteractionEvidenceMode.DIAGNOSTIC
                if index.pipeline_metrics.get("diagnostic_capture") is True
                else WebInteractionEvidenceMode.NATURAL
            ),
        )
        if rebound is None:
            return replace(
                observation,
                matching_target_count=0,
                target_visible=False,
                target_enabled=False,
                target_hit_test_passed=False,
            )
        return observation

    async def _read(
        self,
        capture_id: str,
        *,
        current_only: bool = False,
    ) -> tuple[WebCaptureArtifactIndex, dict[str, object]]:
        if not await self.index_reader.matches_active_capture(
            task_id=self.task_id,
            agent_run_id=self.agent_run_id,
            session_id=self.session_id,
            capture_id=capture_id,
            current_only=current_only,
        ):
            raise RuntimeError("WEB_CAPTURE_IDENTITY_MISMATCH")
        index = await self.index_reader.read(
            task_id=self.task_id,
            capture_id=capture_id,
        )
        raw_snapshot = await self.snapshot_reader.read_ui_snapshot(
            task_id=self.task_id,
            page_capture_id=capture_id,
        )
        if index.capture_id != capture_id:
            raise RuntimeError("WEB_CAPTURE_IDENTITY_MISMATCH")
        snapshot = dict(raw_snapshot)
        snapshot["capture_id"] = capture_id
        return index, snapshot


@dataclass(frozen=True, kw_only=True)
class WebAgeEvidenceReaderAdapter:
    source: WebRuntimeEvidenceSource

    async def read(self, *, capture_id: str) -> WebAgeGateEvidence:
        return await self.source.read_age(capture_id=capture_id)


async def supplement_web_age_evidence(
    evidence: WebAgeGateEvidence, *, service: object
) -> WebAgeGateEvidence:
    """年龄门分类前补图；补图失败仅附加缺口，不改变主 Capture 或吞掉取消。"""
    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementalCommand,
    )

    reason = "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    if evidence.url:
        try:
            result = await service.capture(
                WebFullPageSupplementalCommand(
                    task_id=evidence.task_id,
                    agent_run_id=evidence.agent_run_id,
                    session_id=evidence.session_id,
                    source_capture_id=evidence.capture_id,
                    source_url=evidence.url,
                    deadline=evidence.deadline,
                    source_eligibility="trusted_pre_gate",
                )
            )
            if result.source_capture_id == evidence.capture_id:
                if result.status == "captured" and result.screenshot_artifact_ids:
                    return replace(
                        evidence,
                        supplemental_screenshot_artifact_ids=result.screenshot_artifact_ids,
                        supplemental_evidence_status="captured",
                        supplemental_evidence_reason_code=None,
                    )
                reason = result.reason_code
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 可选长图故障不能覆盖年龄门主证据。
            reason = "WEB_FULL_PAGE_SCREENSHOT_SUPPLEMENTAL_UNAVAILABLE"
    return replace(
        evidence,
        supplemental_screenshot_artifact_ids=(),
        supplemental_evidence_status="failed",
        supplemental_evidence_reason_code=reason,
    )


@dataclass(frozen=True, kw_only=True)
class WebPageEvidenceReaderAdapter:
    source: WebRuntimeEvidenceSource

    async def read(self, *, capture_id: str) -> WebPageEvidence:
        return await self.source.read_page(capture_id=capture_id)


@dataclass(frozen=True, kw_only=True)
class WebReadyCaptureReaderAdapter:
    source: WebRuntimeEvidenceSource

    async def read_snapshot(self, *, capture_id: str) -> Mapping[str, object]:
        """只读当前 ready Capture 的可信快照，供阶段诊断复核。"""

        _index, snapshot = await self.source._read(capture_id, current_only=True)
        return snapshot

    async def read_current(self, *, page_role: str) -> WebReadyPageCapture:
        capture_id = await self.source.index_reader.read_current_capture_id(
            task_id=self.source.task_id,
            agent_run_id=self.source.agent_run_id,
            session_id=self.source.session_id,
        )
        # 当前帧可能已滚动但未被选为主体图；禁止直接沿用旧分类帧或信任补图返回 ID。
        return await self.source.read_ready(capture_id=capture_id, page_role=page_role)

    async def read(
        self,
        *,
        capture_id: str,
        page_role: str,
    ) -> WebReadyPageCapture:
        return await self.source.read_ready(
            capture_id=capture_id,
            page_role=page_role,
        )


@dataclass(frozen=True, kw_only=True)
class WebFrontierCaptureMetadataReaderAdapter:
    """只读取 Capture 身份与就绪 metadata，禁止越过门禁读取 UI Snapshot。"""

    source: WebRuntimeEvidenceSource

    async def read(self, *, capture_id: str, depth: int = 0) -> WebCapture:
        owned = await self.source.index_reader.matches_active_capture(
            task_id=self.source.task_id,
            agent_run_id=self.source.agent_run_id,
            session_id=self.source.session_id,
            capture_id=capture_id,
            current_only=False,
        )
        if not owned:
            raise RuntimeError("WEB_CAPTURE_IDENTITY_MISMATCH")
        current = await self.source.index_reader.matches_active_capture(
            task_id=self.source.task_id,
            agent_run_id=self.source.agent_run_id,
            session_id=self.source.session_id,
            capture_id=capture_id,
            current_only=True,
        )
        index = await self.source.index_reader.read(
            task_id=self.source.task_id,
            capture_id=capture_id,
        )
        if index.capture_id != capture_id:
            raise RuntimeError("WEB_CAPTURE_IDENTITY_MISMATCH")
        admission = assess_web_capture_evidence_admission(
            index.pipeline_metrics,
            capture_role="after_action",
            profile=self.source.readiness_profile,
            diagnostic=index.pipeline_metrics.get("diagnostic_capture") is True,
        )
        chain = tuple(index.redirect_chain)
        requested_url = str(index.requested_url or (chain[0] if chain else index.url))
        return WebCapture(
            capture_id=capture_id,
            session_id=self.source.session_id,
            finalization_epoch=index.finalization_epoch,
            requested_url=requested_url,
            final_url=index.url,
            redirect_chain=chain,
            current=current,
            ready=admission.can_enter_formal_bundle,
            capture_state_fingerprint=web_capture_state_fingerprint(index),
            depth=depth,
        )


def web_capture_state_fingerprint(index: WebCaptureArtifactIndex) -> str:
    """以持久 Capture 与 DOM 证据生成一次发现状态身份。"""
    payload = "\x1f".join(
        (
            index.capture_id,
            index.url,
            str(index.artifact_ids.get("dom") or ""),
            str(index.finalization_epoch),
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _required_artifact(index: WebCaptureArtifactIndex, artifact_type: str) -> str:
    artifact_id = str(index.artifact_ids.get(artifact_type) or "")
    if not artifact_id:
        raise RuntimeError(f"WEB_CAPTURE_{artifact_type.upper()}_MISSING")
    return artifact_id


__all__ = [
    "WebAgeEvidenceReaderAdapter",
    "WebFrontierCaptureMetadataReaderAdapter",
    "WebPageEvidenceReaderAdapter",
    "WebReadyCaptureReaderAdapter",
    "WebRuntimeEvidenceSource",
]
