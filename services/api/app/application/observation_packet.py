"""规则判定前的标准化 Observation Packet 组装用例。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from typing import Any, Protocol
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit

from PIL import Image

from services.api.app.application.app_session_events import build_display_safe_area
from services.api.app.application.evidence_payload_safety import redact_audit_payload
from services.api.app.application.evidence_presentation import (
    DECLARED_SESSION_AUTH_KEY,
    apply_declared_evidence_flags,
    derive_app_presentation,
    derive_h5_presentation,
    presentation_from_capture_metadata,
    reconcile_presentation_with_page_state,
    snapshot_for_presentation,
)
from services.api.app.domain.evidence.presentation import PRESENTATION_METADATA_KEY

OBSERVATION_PACKET_SCHEMA_VERSION = "observation-packet.v1"
MAX_VISIBLE_TEXT_CHARACTERS = 6_000
MAX_DOM_SUMMARY_ELEMENTS = 200
MAX_UI_SNAPSHOT_ELEMENTS = 80
MAX_RUNTIME_EVENTS = 100
MAX_ACTION_LOG_ITEMS = 50
MAX_AGENT_INPUT_BYTES = 4_096
MAX_JOURNEY_STEPS = 20
MAX_JOURNEY_TEXT_CHARACTERS = 1_000
MAX_JOURNEY_ELEMENTS = 40
MAX_JOURNEY_ACTIONS = 10
MAX_OBSERVATION_PACKET_BYTES = 180_000
MAX_UI_SEQUENCE_FRAMES = 8
MAX_UI_SEQUENCE_ELEMENTS_PER_FRAME = 8
MAX_TRANSIENT_MESSAGES = 50
MAX_SELECTOR_CHARACTERS = 512
MAX_RESOURCE_URL_CHARACTERS = 2048
MAX_CLASS_TOKEN_CHARACTERS = 100
PACKET_EVIDENCE_ARTIFACT_TYPES = frozenset(
    {
        "dom",
        "visible_text",
        "screenshot",
        "ui_snapshot",
        "ui_tree",
        "har",
        "network_trace",
        "device_log",
        "fault_artifact",
        "console_log",
        "action_trace",
        "media_state",
        "temporal_trace",
        "page_coverage",
    }
)

_APP_SURFACE_KINDS = frozenset({"android", "ios"})
_APP_STATIC_VISUAL_RULE_CODES = frozenset(
    {"alioth_spec_24", "alioth_spec_27", "alioth_spec_28"}
)
_APP_PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
_APP_ACTIVITY_PATTERN = re.compile(r"(?:\.[A-Za-z_$][A-Za-z0-9_$]*|[A-Za-z_$][A-Za-z0-9_$.]*)")
_MAX_APP_LOCATION_COMPONENT_CHARACTERS = 512

_ARTIFACT_ALIASES = {
    "dom_snapshot": "dom",
    "network_log": "har",
    "network_events": "har",
    "network_proxy": "network_trace",
}


@dataclass(frozen=True)
class ObservationArtifact:
    """已校验归属与完整性的页面证据。"""

    artifact_id: str
    artifact_type: str
    content_type: str
    payload: bytes


@dataclass(frozen=True)
class ObservationJourneyStep:
    """同一次 Agent Run 中由 Surface Runtime 采集的可信旅程节点。"""

    page_capture_id: str
    page_seq: int
    page_url: str
    page_title: str | None
    page_type: str | None
    page_state: str | None
    action_log: tuple[dict[str, Any], ...]
    artifacts: tuple[ObservationArtifact, ...]
    capture_role: str | None = None
    parent_capture_id: str | None = None
    surface_phase_run_id: str | None = None
    captured_at: str | None = None


# packet quality_gate.source 的取值：标记齐全性结论来自清单而非判定侧重算
MANIFEST_QUALITY_GATE_SOURCE = "evidence_manifest"


@dataclass(frozen=True)
class ManifestQualityProjection:
    """E22：清单侧已裁决的齐全性结论投影（判定侧只读，不得重算）。

    冻结依据 E02 条款 1.1：齐全性由且仅由清单写入侧裁决一次；
    条款 1.3：判定侧只保留载荷预算与投影。
    """

    manifest_id: str
    manifest_version: int
    quality_status: str
    quality_reason: str | None = None


@dataclass(frozen=True)
class ObservationSource:
    """同一任务、页面采集下的可信证据来源。"""

    task_id: str
    page_capture_id: str
    page_url: str
    page_title: str | None
    page_type: str | None
    page_state: str | None
    action_log: tuple[dict[str, Any], ...]
    artifacts: tuple[ObservationArtifact, ...]
    journey_steps: tuple[ObservationJourneyStep, ...] = ()
    surface_kind: str | None = None
    visual_observation: dict[str, Any] | None = None
    # App Capture 子表中的目标包、显示尺寸和设备快照；不得由视觉摘要推断。
    app_capture_facts: dict[str, Any] | None = None
    # 非 None 表示证据来自清单：build() 直接投影该结论，跳过齐全性重算
    manifest_quality: ManifestQualityProjection | None = None
    # 生产该页面采集的 Explorer/Evidence Run；消费者不得跨 Run 复用。
    agent_run_id: str | None = None
    capture_role: str | None = None
    parent_capture_id: str | None = None
    surface_phase_run_id: str | None = None
    captured_at: str | None = None
    # 采集写入口持久化的展示层评估（最上层是谁、底页是否可见、是否诊断材料）；
    # 缺失时由 build() 按平台从原始快照 / 视觉观察重新推导，保证判定器总能看到这一层事实。
    presentation: dict[str, Any] | None = None
    # 采集侧在证据清单里显式声明的字段（is_obstructed / diagnostic / session_auth）；
    # 有声明时判定侧直接读，不再靠识别去推。由 declared_evidence_flags() 从采集元数据取出。
    declared_flags: dict[str, Any] | None = None
    # 仅专用 InputSet 读取端口填充，不从页面 metadata 推断。
    web_sequence: dict[str, Any] | None = None
    web_sequence_sources: tuple[ObservationSource, ...] = ()
    # 仅用于区分 H5 活会话的可变页面事实；不得从 surface_kind 猜测。
    target_platform: str | None = None
    # 持久化的 Web 正文分类与发布就绪事实；不能仅凭 content_detail 标签假定正文存在。
    web_document_facts: dict[str, Any] | None = None
    original_page_input: dict[str, Any] | None = None


@dataclass(frozen=True)
class ObservationPacket:
    """持久化后的规则级标准证据输入包。"""

    observation_packet_id: str
    task_id: str
    page_capture_id: str
    rule_id: str
    rule_code: str
    evidence_artifact_ids: tuple[str, ...]
    packet_payload: dict[str, Any]
    quality_gate_status: str
    quality_gate_reason: str | None
    image_payloads: tuple[tuple[str, bytes, str], ...] = ()


@dataclass(frozen=True)
class BuildObservationPacketRequest:
    """组装规则级证据包的请求。"""

    task_id: str
    page_capture_id: str
    rule_id: str
    rule_code: str
    rule_name: str
    category: str
    executor_type: str
    check_point: str
    input_contract: dict[str, Any]
    judge_config: dict[str, Any]
    manual_review_policy: dict[str, Any]
    requested_evidence_artifact_ids: tuple[str, ...]
    agent_input: dict[str, Any]
    agent_run_id: str | None = None
    source_agent_run_id: str | None = None
    evidence_source_page_capture_id: str | None = None
    evidence_manifest_id: str | None = None
    evidence_input_set_id: str | None = None
    original_page_input_set_id: str | None = None
    expected_contract_hash: str | None = None
    work_item_id: str | None = None
    attempt_id: str | None = None
    lease_token: str | None = None
    evidence_capture_roles: dict[str, str] | None = None
    evidence_manifest_refs: dict[str, dict[str, str]] | None = None
    retained_baseline_artifact_ids: dict[str, tuple[str, ...]] | None = None


class ObservationPacketRepository(Protocol):
    """Observation Packet 证据读取和持久化契约。"""

    async def load_source(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> ObservationSource | None:
        """读取同一任务页面的可信基线证据。"""

    async def save_packet(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        rule_id: str,
        evidence_artifact_ids: tuple[str, ...],
        packet_payload: dict[str, Any],
        quality_gate_status: str,
        quality_gate_reason: str | None,
        agent_run_id: str | None,
    ) -> str:
        """保存规则级标准证据包并返回主键。"""


class ObservationPacketValidationError(ValueError):
    """页面采集或规则证据包不符合可信输入契约。"""


def build_observation_input_fingerprint(
    *,
    evidence_artifact_ids: tuple[str, ...] | list[str],
    capture_roles: dict[str, str] | None = None,
    semantic_revision: dict[str, Any] | None = None,
) -> str:
    """生成只描述证据绑定关系的稳定指纹。"""

    canonical_binding = {
        "capture_roles": dict(sorted((capture_roles or {}).items())),
        "evidence_artifact_ids": sorted(set(evidence_artifact_ids)),
    }
    if semantic_revision is not None:
        canonical_binding["semantic_revision"] = semantic_revision
    payload = json.dumps(
        canonical_binding,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ObservationPacketService:
    """从浏览器原始证据构造有界、可审计、可供 Judge 使用的事实包。"""

    def __init__(
        self, *, repository: ObservationPacketRepository, app_action_facts_reader=None,
    ) -> None:
        self._repository = repository
        self._app_action_facts_reader = app_action_facts_reader
        self._cache_lock = asyncio.Lock()
        self._source_cache: dict[
            tuple[str, str, str | None, str | None], ObservationSource | None
        ] = {}
        self._decoded_cache: dict[
            tuple[str, str, str | None, str | None], dict[str, Any]
        ] = {}
        self._image_cache: dict[
            tuple[str, str, str | None, str | None], tuple[tuple[str, bytes, str], ...]
        ] = {}

    async def _app_list_interaction_source(self, request, source, roles):
        from services.api.app.application.app_rule_action_readiness import (
            app_list_interaction_source_capture,
            app_rule_action_requirement,
        )
        if (
            source.surface_kind not in {"android", "ios"}
            or app_rule_action_requirement({"judge_config": request.judge_config})
            != "list_scroll_interaction"
        ):
            return None
        if self._app_action_facts_reader is None:
            from services.api.app.infrastructure.db.app_rule_action_facts_repository import (
                PostgresAppRuleActionFactsReader,
            )
            self._app_action_facts_reader = PostgresAppRuleActionFactsReader()
        facts = await self._app_action_facts_reader.get_facts(
            task_id=request.task_id, capture_id=source.page_capture_id,
            agent_run_id=source.agent_run_id,
        )
        before = app_list_interaction_source_capture(**facts) if facts is not None else None
        if before is None or (
            roles.get("before", {}).get("page_capture_id") != before["capture_id"]
            or roles.get("primary", {}).get("page_capture_id") != source.page_capture_id
        ):
            return None
        return {
            "source": "verified_app_action_facts",
            "rule_id": request.rule_id, "rule_code": request.rule_code,
            **facts["scope"], "primary_capture_id": source.page_capture_id,
            "source_capture_id": before["capture_id"],
            "presentation": derive_app_presentation(
                page_state=before.get("page_state"),
                overlay_signature=before.get("overlay_signature"),
                surface_facts=before.get("surface_facts"),
            ).to_dict(),
        }

    async def build(
        self,
        request: BuildObservationPacketRequest,
    ) -> ObservationPacket:
        evidence_source_page_capture_id = (
            request.evidence_source_page_capture_id or request.page_capture_id
        )
        cache_key = (
            request.task_id,
            evidence_source_page_capture_id,
            request.source_agent_run_id,
            request.evidence_manifest_id
            or (request.evidence_manifest_refs or {}).get("primary", {}).get("manifest_id"),
        )
        if request.evidence_input_set_id is not None:
            cache_key += (request.evidence_input_set_id, request.rule_id, request.expected_contract_hash,
                          request.work_item_id, request.attempt_id, request.agent_run_id, request.lease_token)
        if request.original_page_input_set_id is not None:
            if request.evidence_input_set_id is not None:
                raise ObservationPacketValidationError("页面原输入不得混用 Web 序列")
            cache_key += (request.original_page_input_set_id, request.rule_id, request.expected_contract_hash,
                          request.work_item_id, request.attempt_id, request.agent_run_id, request.lease_token,
                          "h5_page")
        source = await self._load_source_once(cache_key)
        if source is None:
            raise ObservationPacketValidationError("页面采集不存在或不属于当前任务")
        if (
            request.source_agent_run_id is not None
            and source.agent_run_id != request.source_agent_run_id
        ):
            raise ObservationPacketValidationError("页面采集不属于当前证据生产 Run")

        page_url = _resolve_observation_page_url(source)
        from services.api.app.application.web_sequence_observation import (
            project_web_sequence_semantics,
        )

        web_semantics = project_web_sequence_semantics(request=request, source=source)

        artifacts_by_type: dict[str, list[ObservationArtifact]] = {}
        for artifact in source.artifacts:
            artifacts_by_type.setdefault(artifact.artifact_type, []).append(artifact)

        app_ad_pair = _project_app_ad_landing_screenshot_pair(source)
        from services.api.app.application.observation_capture_roles import load_role_evidence

        role_artifacts, role_facts = await load_role_evidence(
            request=request, primary=source, load_source=self._load_source_once,
        )
        list_interaction_source = await self._app_list_interaction_source(
            request, source, role_facts
        )
        packet_artifacts = _merge_observation_artifacts(
            source.artifacts,
            (*app_ad_pair["artifacts"], *role_artifacts),
        )
        if request.evidence_input_set_id is not None:
            packet_artifacts = _merge_observation_artifacts(packet_artifacts, tuple(
                artifact for frame in source.web_sequence_sources for artifact in frame.artifacts
            ))

        decoded = self._decoded_cache.get(cache_key)
        if decoded is None:
            decoded = _decode_artifacts(
                artifacts_by_type,
                app_capture_facts=source.app_capture_facts,
            )
            self._remember_cache_value(self._decoded_cache, cache_key, decoded)
        packet_ui_snapshot = _app_observation_snapshot_sequence(
            source=source,
            current_snapshot=decoded["ui_snapshot"],
        )
        if source.target_platform == "h5":
            from services.api.app.application.h5_observation_sequence import (
                project_h5_role_sequence,
            )

            packet_ui_snapshot = project_h5_role_sequence(
                primary=source, role_facts=role_facts, current_snapshot=packet_ui_snapshot,
            )
        from services.api.app.application.observation_surface_guard import (
            is_empty_observed_surface,
            target_document_unavailability_reason,
        )

        empty_surface = is_empty_observed_surface(
            visible_text=str(decoded.get("visible_text") or ""),
            ui_snapshot=decoded.get("ui_snapshot"),
            image_payloads=_decode_screenshot_payloads(
                artifacts_by_type.get("screenshot", []), limit=None,
            ),
        )
        document_unavailable = target_document_unavailability_reason(
            title=source.page_title or str((decoded.get("ui_snapshot") or {}).get("title") or ""),
            visible_text=str(decoded.get("visible_text") or ""),
        )
        artifact_ids = tuple(
            artifact.artifact_id
            for artifact in packet_artifacts
            if artifact.artifact_type in PACKET_EVIDENCE_ARTIFACT_TYPES
        )
        if source.manifest_quality is not None:
            # E02 条款 1.1/1.4：证据来自清单时，齐全性结论只投影不重算
            gate_status = source.manifest_quality.quality_status
            gate_reason = source.manifest_quality.quality_reason
            missing_sources = []
        else:
            gate_status, gate_reason, missing_sources = _evaluate_quality_gate(
                input_contract=request.input_contract,
                artifacts_by_type=artifacts_by_type,
                decoded=decoded,
                page_state=source.page_state,
                surface_kind=source.surface_kind,
                visual_observation=source.visual_observation,
                web_sequence_admissible=(web_semantics or {}).get("status") == "admissible",
                app_static_visual_rule=_is_app_static_visual_rule(request),
            )
        for role, role_fact in role_facts.items():
            if role != "primary" and role_fact["quality_status"] not in {None, "pass"}:
                gate_status = role_fact["quality_status"]
                gate_reason = role_fact["quality_reason"] or f"{role} 角色材料尚未通过证据清单门禁"
                break
        visible_text = str(decoded.get("visible_text") or "")
        visual_facts = _compact_visual_observation(source.visual_observation)
        visible_text_source = "artifact"
        if not visible_text.strip() and visual_facts.get("derived_text"):
            # App 视觉摘要仅作派生可读文本，不冒充 OCR/DOM visible_text 制品
            visible_text = str(visual_facts["derived_text"])
            visible_text_source = "app_visual"
        packet_payload = {
            "schema_version": OBSERVATION_PACKET_SCHEMA_VERSION,
            "identity": {
                "source_agent_run_id": source.agent_run_id,
                "consumer_agent_run_id": request.agent_run_id,
                "evidence_manifest_id": request.evidence_manifest_id,
            },
            "rule": {
                "rule_id": request.rule_id,
                "rule_code": request.rule_code,
                "name": request.rule_name,
                "category": request.category,
                "executor_type": request.executor_type,
                "check_point": request.check_point,
                "input_contract": project_rule_input_contract(request.input_contract),
                "criteria": {
                    "deterministic_rules": _string_list(
                        request.judge_config.get("deterministic_rules")
                    ),
                    "llm_checks": _string_list(request.judge_config.get("llm_checks")),
                    "thresholds": _mapping(request.judge_config.get("thresholds")),
                    "engine_config": _mapping(request.judge_config.get("engine_config")),
                    "capability_config": _mapping(request.judge_config.get("capability_config")),
                },
                "manual_review_policy": _compact_mapping(request.manual_review_policy),
            },
            "page": {
                "page_capture_id": source.page_capture_id,
                "url": page_url,
                "title": source.page_title,
                "page_type": source.page_type,
                "page_state": source.page_state,
                "surface_kind": source.surface_kind,
                # 采集侧声明了登录态才带这一项，没声明的历史采集观察包内容不变
                **_declared_session_auth(source),
            },
            "facts": {
                "visible_text": visible_text,
                "visible_text_character_count": len(visible_text),
                "visible_text_truncated": (
                    False
                    if visible_text_source == "app_visual"
                    else decoded["visible_text_truncated"]
                ),
                "visible_text_source": visible_text_source,
                "dom_summary": decoded["dom_summary"],
                "ui_snapshot": packet_ui_snapshot,
                "media_state": decoded["media_state"],
                "content_attribution": _content_attribution_fact(
                    page_type=source.page_type,
                    ui_snapshot=decoded["ui_snapshot"],
                    journey_steps=source.journey_steps,
                ),
                "network_summary": decoded["network_summary"],
                "console_summary": decoded["console_summary"],
                "device_log_summary": decoded.get("device_log_summary") or {},
                "fault_summary": decoded.get("fault_summary") or {},
                "app_identity": decoded.get("app_identity") or {},
                "action_log": _compact_action_log(source.action_log),
                "journey": _compact_journey(source.journey_steps),
                "screenshot_evidence": [
                    *decoded.get("screenshot_evidence", []),
                    *app_ad_pair["screenshot_evidence"],
                    *_summarize_screenshot_evidence(list(role_artifacts)),
                ],
                "screenshot_count": len(
                    [
                        artifact
                        for artifact in packet_artifacts
                        if artifact.artifact_type == "screenshot"
                        and artifact.content_type.split(";", 1)[0].strip().casefold()
                        == "image/png"
                    ]
                ),
                "visual_observation": visual_facts,
                # 展示层评估同页共享：底层被遮挡或材料来自诊断操作时，判定器与预检都据此拒绝
                "presentation": _presentation_facts(source, artifacts_by_type),
                "has_ui_tree": bool(artifacts_by_type.get("ui_tree")),
                # ui_tree 是真实 Accessibility 证据时，可投影出兼容 ui_snapshot；
                # 这里反映 Judge 实际可消费的快照，而非只统计原始 Artifact 类型。
                "has_ui_snapshot": bool(packet_ui_snapshot),
                "screen_id": (
                    (decoded.get("ui_snapshot") or {}).get("screen_id")
                    if isinstance(decoded.get("ui_snapshot"), dict)
                    else None
                ),
                **(
                    {"structure_screen_mismatch": True}
                    if decoded.get("structure_screen_mismatch") is True
                    else {}
                ),
            },
            "candidate_input": _sanitize_agent_input(request.agent_input),
            "evidence": [
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "content_type": artifact.content_type,
                    "size_bytes": len(artifact.payload),
                }
                for artifact in packet_artifacts
            ],
            "quality_gate": {
                "status": gate_status,
                "reason": gate_reason,
                "missing_sources": missing_sources,
                "requested_artifact_ids": list(request.requested_evidence_artifact_ids),
            },
        }
        if request.evidence_manifest_id is None:
            packet_payload["identity"].pop("evidence_manifest_id")
        if source.target_platform is not None:
            packet_payload["identity"]["target_platform"] = source.target_platform
        if request.original_page_input_set_id is not None:
            if source.target_platform != "h5" or source.original_page_input is None:
                raise ObservationPacketValidationError("原页面读端口未认证真实 H5 来源")
            packet_payload["identity"]["original_page_input"] = dict(source.original_page_input)
        if role_facts:
            packet_payload["identity"]["capture_roles"] = {
                role: fact["page_capture_id"] for role, fact in role_facts.items()
            }
            packet_payload["facts"]["capture_roles"] = role_facts
        if list_interaction_source is not None:
            # 来源由正式动作读端口复核，随观察包冻结；模型输入不能提供它。
            packet_payload["facts"]["app_list_interaction_source"] = list_interaction_source
        if empty_surface:
            packet_payload["facts"]["empty_observed_surface"] = True
            packet_payload["identity"].setdefault(
                "capture_roles", {"primary": source.page_capture_id},
            )
        if document_unavailable:
            packet_payload["facts"]["target_document_unavailable_reason"] = document_unavailable
            packet_payload["identity"].setdefault(
                "capture_roles", {"primary": source.page_capture_id},
            )
        if source.web_document_facts:
            packet_payload["facts"]["web_document"] = source.web_document_facts
            packet_payload["identity"].setdefault(
                "capture_roles", {"primary": source.page_capture_id},
            )
        if web_semantics is not None:
            packet_payload["web_evidence"] = web_semantics
            packet_payload["facts"]["web_sequence"] = web_semantics
        if source.manifest_quality is not None:
            packet_payload["quality_gate"]["source"] = MANIFEST_QUALITY_GATE_SOURCE
            packet_payload["quality_gate"]["manifest_id"] = source.manifest_quality.manifest_id
            packet_payload["quality_gate"]["manifest_version"] = (
                source.manifest_quality.manifest_version
            )
        packet_payload, gate_status, gate_reason, missing_sources = _enforce_packet_budget(
            packet_payload=packet_payload,
            gate_status=gate_status,
            gate_reason=gate_reason,
            missing_sources=missing_sources,
            quality_is_authoritative=source.manifest_quality is not None,
        )
        script_images = ()
        if script_images:
            facts = packet_payload.get("facts")
            if isinstance(facts, dict):
                evidence = facts.get("screenshot_evidence")
                if not isinstance(evidence, list):
                    evidence = []
                    facts["screenshot_evidence"] = evidence
                known = {
                    str(item.get("artifact_id"))
                    for item in evidence
                    if isinstance(item, dict)
                }
                added = 0
                for artifact_id, _content in script_images:
                    if artifact_id in known:
                        continue
                    evidence.append(
                        {
                            "artifact_id": artifact_id,
                            "content_type": "image/png",
                            "source": "script_recording",
                        }
                    )
                    added += 1
                facts["screenshot_count"] = int(facts.get("screenshot_count") or 0) + added
            listed = packet_payload.get("evidence")
            if isinstance(listed, list):
                known_ids = {
                    str(item.get("artifact_id"))
                    for item in listed
                    if isinstance(item, dict)
                }
                for artifact_id, content in script_images:
                    if artifact_id in known_ids:
                        continue
                    listed.append(
                        {
                            "artifact_id": artifact_id,
                            "artifact_type": "screenshot",
                            "content_type": "image/png",
                            "size_bytes": len(content),
                        }
                    )
            artifact_ids = tuple(
                dict.fromkeys([*artifact_ids, *(item[0] for item in script_images)])
            )
        base_images = self._image_payloads_once(
            cache_key=cache_key + tuple(
                (artifact.artifact_id, hashlib.sha256(artifact.payload).hexdigest())
                for artifact in packet_artifacts if artifact.artifact_type == "screenshot"
            ),
            preserve_all=(request.evidence_input_set_id is not None
                          or request.original_page_input_set_id is not None or bool(role_facts)),
            artifacts=[
                artifact
                for artifact in packet_artifacts
                if artifact.artifact_type == "screenshot"
            ],
        )
        seen_images = {item[0] for item in base_images}
        image_payloads = (
            *base_images,
            *[
                (artifact_id, content, "image/png")
                for artifact_id, content in script_images
                if artifact_id not in seen_images
            ],
        )
        packet_id = await self._repository.save_packet(
            task_id=request.task_id,
            page_capture_id=request.page_capture_id,
            rule_id=request.rule_id,
            evidence_artifact_ids=artifact_ids,
            packet_payload=packet_payload,
            quality_gate_status=gate_status,
            quality_gate_reason=gate_reason,
            agent_run_id=request.agent_run_id,
        )
        return ObservationPacket(
            observation_packet_id=packet_id,
            task_id=request.task_id,
            page_capture_id=request.page_capture_id,
            rule_id=request.rule_id,
            rule_code=request.rule_code,
            evidence_artifact_ids=artifact_ids,
            packet_payload=packet_payload,
            quality_gate_status=gate_status,
            quality_gate_reason=gate_reason,
            image_payloads=image_payloads,
        )

    async def _load_source_once(
        self,
        cache_key: tuple[str, str, str | None, str | None],
    ) -> ObservationSource | None:
        if len(cache_key) > 4:
            # 冻结内容可缓存，当前任务/租约授权不可缓存；每次消费都重验周期。
            loader = getattr(self._repository, "load_source_for_page_input_set"
                             if len(cache_key) == 12 else "load_source_for_input_set", None)
            if loader is None:
                raise ObservationPacketValidationError("证据仓储不支持 Web InputSet 精确读取")
            manifest_identity = {"manifest_id": cache_key[3]} if len(cache_key) == 12 else {}
            return await loader(
                task_id=cache_key[0], page_capture_id=cache_key[1],
                source_agent_run_id=cache_key[2], input_set_id=cache_key[4],
                rule_id=cache_key[5], rule_contract_hash=cache_key[6],
                work_item_id=cache_key[7], attempt_id=cache_key[8],
                consumer_agent_run_id=cache_key[9], lease_token=cache_key[10],
                **manifest_identity,
            )
        if cache_key in self._source_cache:
            return self._source_cache[cache_key]
        async with self._cache_lock:
            if cache_key not in self._source_cache:
                manifest_id = cache_key[3]
                if manifest_id is None:
                    source = await self._repository.load_source(
                        task_id=cache_key[0],
                        page_capture_id=cache_key[1],
                    )
                else:
                    loader = getattr(self._repository, "load_source_for_manifest", None)
                    if loader is None:
                        raise ObservationPacketValidationError(
                            "证据仓储不支持按指定 Manifest 精确读取"
                        )
                    source = await loader(
                        task_id=cache_key[0],
                        page_capture_id=cache_key[1],
                        manifest_id=manifest_id,
                    )
                if source is not None and source.target_platform == "h5":
                    # H5 活会话会在同一 Capture 上完成页面状态与展示层投影；
                    # 该事实未封存前不可跨规则工作项缓存。
                    return source
                self._remember_cache_value(self._source_cache, cache_key, source)
            return self._source_cache[cache_key]

    def _image_payloads_once(
        self,
        *,
        cache_key: tuple[str, str, str | None, str | None],
        artifacts: list[ObservationArtifact],
        preserve_all: bool = False,
    ) -> tuple[tuple[str, bytes, str], ...]:
        image_cache_key = (*cache_key, preserve_all)
        images = self._image_cache.get(image_cache_key)
        if images is None:
            images = _decode_screenshot_payloads(artifacts, limit=None if preserve_all else 6)
            self._remember_cache_value(self._image_cache, image_cache_key, images)
        return images

    @staticmethod
    def _remember_cache_value(cache: dict[Any, Any], key: Any, value: Any) -> None:
        """保留最近页面的小型会话缓存，避免 Worker 长时间运行时无界增长。"""
        if len(cache) >= 32 and key not in cache:
            cache.pop(next(iter(cache)))
        cache[key] = value


def build_app_observation_location(
    *,
    package_name: Any,
    activity: Any,
) -> str | None:
    """从已持久化的 App Capture 身份构造有界的 App 页面 location。

    该值只表达 `app://package/activity` 的页面身份，不是可请求的 HTTP URL。
    只有同时具备合法 package 和 activity 时才生成，避免用 unknown 或视觉字段
    拼出看似可信的地址。
    """

    package = _normalize_app_location_component(
        package_name,
        pattern=_APP_PACKAGE_PATTERN,
    )
    activity_value = _normalize_app_location_component(
        activity,
        pattern=_APP_ACTIVITY_PATTERN,
    )
    if package is None or activity_value is None:
        return None
    return f"app://{package.casefold()}/{activity_value.lstrip('/')}"


def _resolve_observation_page_url(source: ObservationSource) -> str:
    """保留采集 URL；App URL 缺失时才回退到 Capture 身份。"""

    page_url = str(source.page_url or "").strip()
    if page_url:
        return page_url
    if str(source.surface_kind or "").casefold() not in _APP_SURFACE_KINDS:
        return ""
    facts = _mapping(source.app_capture_facts)
    return (
        build_app_observation_location(
            package_name=facts.get("package_name"),
            activity=facts.get("activity"),
        )
        or ""
    )


def _normalize_app_location_component(
    value: Any,
    *,
    pattern: re.Pattern[str],
) -> str | None:
    """校验 location 组件，拒绝会改变 URI 语义的控制符和分隔符。"""

    if not isinstance(value, str):
        return None
    component = value.strip()
    if (
        not component
        or len(component) > _MAX_APP_LOCATION_COMPONENT_CHARACTERS
        or any(char in component for char in "\r\n\t/?#")
        or pattern.fullmatch(component) is None
    ):
        return None
    return component


def _declared_session_auth(source: ObservationSource) -> dict[str, str]:
    flags = source.declared_flags if isinstance(source.declared_flags, dict) else {}
    value = flags.get(DECLARED_SESSION_AUTH_KEY)
    return {DECLARED_SESSION_AUTH_KEY: value} if isinstance(value, str) else {}


def _presentation_facts(
    source: ObservationSource,
    artifacts_by_type: dict[str, list[ObservationArtifact]],
) -> dict[str, Any]:
    """投影采集行上的展示层评估；没有持久化时按平台从原始事实重新推导。"""
    stored = (
        presentation_from_capture_metadata({PRESENTATION_METADATA_KEY: source.presentation})
        if source.presentation
        else None
    )
    is_app = str(source.surface_kind or "").casefold() in {"android", "ios"}
    if is_app:
        if stored is None:
            visual = (
                source.visual_observation if isinstance(source.visual_observation, dict) else {}
            )
            stored = derive_app_presentation(
                page_state=visual.get("page_state") or source.page_state,
                overlay_signature=visual.get("overlay_signature"),
                surface_facts=visual.get("surface_facts"),
            )
        return (apply_declared_evidence_flags(stored, source.declared_flags) or stored).to_dict()
    if stored is None:
        raw_snapshot = _first_json_object(artifacts_by_type.get("ui_snapshot", []))
        stored = derive_h5_presentation(
            ui_snapshot=snapshot_for_presentation(raw_snapshot) if raw_snapshot else None,
            page_state=source.page_state,
            action_log=source.action_log,
        )
    reconciled = reconcile_presentation_with_page_state(stored, page_state=source.page_state)
    resolved = reconciled or stored
    # 采集侧显式声明了遮挡 / 诊断的，以声明为准
    return (apply_declared_evidence_flags(resolved, source.declared_flags) or resolved).to_dict()


def _structure_activity_mismatches_capture(
    structure_activity: Any,
    capture_activity: Any,
) -> bool:
    """截图所属界面和结构树界面不同时，不能拿这棵树量控件间距。"""
    if not isinstance(structure_activity, str) or not isinstance(capture_activity, str):
        return False
    structure = structure_activity.strip()
    capture = capture_activity.strip()
    if not structure or not capture:
        return False
    return structure.casefold() != capture.casefold()


def _decode_artifacts(
    artifacts_by_type: dict[str, list[ObservationArtifact]],
    *,
    app_capture_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    visible_text = _first_text(artifacts_by_type.get("visible_text", []))
    dom = _first_text(artifacts_by_type.get("dom", []))
    ui_snapshot = _first_json_object(artifacts_by_type.get("ui_snapshot", []))
    trusted_app = _trusted_app_capture_projection(app_capture_facts)
    if not ui_snapshot and artifacts_by_type.get("ui_tree"):
        # Timed Capture/Handoff 只保存 ui_tree 时，从真实 hierarchy 投影有界快照；
        # 不使用视觉摘要或动作语义填充页面元素。
        ui_tree = _first_text(artifacts_by_type["ui_tree"])
        try:
            from services.api.app.application.app_surface_evidence import (
                project_ui_snapshot_from_hierarchy,
            )

            ui_snapshot = project_ui_snapshot_from_hierarchy(
                ui_tree,
                package_name=trusted_app.get("package_name"),
            )
        except (ValueError, ET.ParseError):
            # 损坏 hierarchy 不得被当成 ui_snapshot；由质量门禁保守拒判。
            ui_snapshot = {}
    else:
        ui_snapshot = dict(ui_snapshot)
    structure_screen_mismatch = _structure_activity_mismatches_capture(
        ui_snapshot.get("activity"),
        trusted_app.get("activity"),
    )
    # qc_app_capture 来自设备取证与 PNG 尺寸校验，身份和显示边界优先于
    # Provider/旧快照中的同名字段；缺少可信 Capture 事实时才保留快照值。
    for key in ("package_name", "activity", "safe_area"):
        if trusted_app.get(key):
            ui_snapshot[key] = trusted_app[key]
    media_state = _first_json_object(artifacts_by_type.get("media_state", []))
    if not media_state:
        media_state = _media_state_from_ui_snapshot(ui_snapshot)
    har = _first_json_value(artifacts_by_type.get("har", []))
    network_trace = _first_json_object(artifacts_by_type.get("network_trace", []))
    device_log = _first_json_object(artifacts_by_type.get("device_log", []))
    fault_artifact = _first_json_object(artifacts_by_type.get("fault_artifact", []))
    console = _first_json_value(artifacts_by_type.get("console_log", []))
    return {
        "structure_screen_mismatch": structure_screen_mismatch,
        "visible_text": visible_text[:MAX_VISIBLE_TEXT_CHARACTERS],
        "visible_text_character_count": len(visible_text),
        "visible_text_truncated": len(visible_text) > MAX_VISIBLE_TEXT_CHARACTERS,
        "dom_summary": _summarize_dom(dom),
        "ui_snapshot": _compact_ui_snapshot(ui_snapshot),
        "media_state": _compact_media_state(media_state),
        "screenshot_evidence": _summarize_screenshot_evidence(
            artifacts_by_type.get("screenshot", [])
        ),
        "app_identity": {
            key: value
            for key in ("package_name", "activity", "safe_area", "screen_id")
            if (
                value := (
                    trusted_app.get(key) or ui_snapshot.get(key)
                    if key in {"package_name", "activity"}
                    else ui_snapshot.get(key) or trusted_app.get(key)
                )
            )
            is not None
        },
        "network_summary": _summarize_network(har)
        if har
        else _summarize_app_network(network_trace),
        "console_summary": _summarize_console(console),
        "device_log_summary": {
            "status": device_log.get("status"),
            "char_count": device_log.get("char_count"),
            "checksum": device_log.get("checksum"),
        }
        if device_log
        else {},
        "fault_summary": {
            "detected": fault_artifact.get("detected"),
            "fault_kind": fault_artifact.get("fault_kind"),
            "status": fault_artifact.get("status"),
        }
        if fault_artifact
        else {},
    }


def _trusted_app_capture_projection(value: dict[str, Any] | None) -> dict[str, Any]:
    """投影 App Capture 的可信身份与显示边界，不读取视觉模型字段。"""
    facts = _mapping(value)
    projected: dict[str, Any] = {}
    for key in ("package_name", "activity"):
        text = facts.get(key)
        if isinstance(text, str) and text.strip():
            projected[key] = text.strip()

    device_snapshot = _mapping(facts.get("device_snapshot"))
    safe_area = _trusted_safe_area(facts.get("safe_area")) or _trusted_safe_area(
        device_snapshot.get("safe_area")
    )
    if safe_area:
        projected["safe_area"] = safe_area
    else:
        # qc_app_capture.width/height 已由 Capture Persistence 校验为真实 PNG
        # 尺寸；只有两者都存在时，才投影“显示边界”而不虚构 inset。
        width = _positive_integer(facts.get("width"))
        height = _positive_integer(facts.get("height"))
        if width is not None and height is not None:
            projected["safe_area"] = build_display_safe_area(
                width=width,
                height=height,
            )
    return projected


def _trusted_safe_area(value: Any) -> dict[str, Any]:
    """只接受带完整边界、正尺寸和来源的设备 safe_area。"""

    area = _mapping(value)
    width = _positive_integer(area.get("width"))
    height = _positive_integer(area.get("height"))
    source = area.get("source")
    if width is None or height is None or not isinstance(source, str) or not source.strip():
        return {}
    for field_name in ("top", "right", "bottom", "left"):
        inset = _number(area.get(field_name))
        if inset is None or inset < 0:
            return {}
    return dict(area)


def _evaluate_quality_gate(
    *,
    input_contract: dict[str, Any],
    artifacts_by_type: dict[str, list[ObservationArtifact]],
    decoded: dict[str, Any],
    page_state: str | None,
    surface_kind: str | None = None,
    visual_observation: dict[str, Any] | None = None,
    web_sequence_admissible: bool = False,
    app_static_visual_rule: bool = False,
) -> tuple[str, str | None, list[str]]:
    required = {
        _ARTIFACT_ALIASES.get(item, item)
        for item in _string_list(input_contract.get("required_artifacts"))
    }
    is_app = str(surface_kind or "").casefold() in {"android", "ios"}
    # App 不强制 Web DOM；visible_text 优先 Accessibility 制品，可软满足
    if is_app:
        required -= {"dom"}
        # ui_snapshot 与 ui_tree 同源；仅有 hierarchy 时视为可满足 Snapshot 要求
        if "ui_snapshot" in required and not artifacts_by_type.get("ui_snapshot"):
            if artifacts_by_type.get("ui_tree") and decoded.get("ui_snapshot"):
                required.discard("ui_snapshot")
            elif app_static_visual_rule and _has_decodable_app_visual_screenshot(
                artifacts_by_type.get("screenshot", []), decoded=decoded,
            ):
                # 已有合同允许视觉判断的静态规则可读真实像素，不把身份投影冒充节点快照。
                # 动作、时序、其他必需来源和后续页面状态门禁仍按原合同检查。
                required.discard("ui_snapshot")
        if "visible_text" in required and not artifacts_by_type.get("visible_text"):
            if artifacts_by_type.get("ui_tree") or visual_observation:
                required.discard("visible_text")
    missing = sorted(
        artifact_type for artifact_type in required if not artifacts_by_type.get(artifact_type)
    )
    quality_gate = _mapping(input_contract.get("quality_gate"))
    # crash/ANR：规则显式 fail_on_app_fault 时，检出故障即门禁失败
    if bool(quality_gate.get("fail_on_app_fault")):
        fault = _first_json_object(artifacts_by_type.get("fault_artifact", []))
        if fault.get("detected") and str(fault.get("fault_kind") or "") in {
            "crash",
            "anr",
        }:
            missing.append(f"fault_artifact({fault.get('fault_kind')})")
    # App 身份门禁：默认要求 package + safe_area；activity 可通过 require_app_activity 加严
    if is_app and bool(quality_gate.get("require_app_identity", True)):
        snap = decoded.get("ui_snapshot") if isinstance(decoded.get("ui_snapshot"), dict) else {}
        identity = (
            decoded.get("app_identity") if isinstance(decoded.get("app_identity"), dict) else {}
        )
        package = str(identity.get("package_name") or snap.get("package_name") or "").strip()
        activity = str(identity.get("activity") or snap.get("activity") or "").strip()
        safe_area = identity.get("safe_area") or snap.get("safe_area")
        if not package:
            missing.append("identity.package")
        if bool(quality_gate.get("require_app_activity")) and not activity:
            missing.append("identity.activity")
        if not isinstance(safe_area, dict) or not safe_area:
            missing.append("safe_area")
    allow_empty_visible_text = bool(quality_gate.get("allow_empty_visible_text"))
    if is_app:
        allow_empty_visible_text = True
    derived_visual_text = ""
    if visual_observation:
        derived_visual_text = str(
            visual_observation.get("visual_summary") or visual_observation.get("derived_text") or ""
        ).strip()
    visible = str(decoded.get("visible_text") or "").strip() or derived_visual_text
    if "visible_text" in required and not allow_empty_visible_text and not visible:
        missing.append("visible_text(non_empty)")
    min_screenshot_count = _integer(quality_gate.get("min_screenshot_count"))
    screenshot_count = len(artifacts_by_type.get("screenshot", []))
    if min_screenshot_count > screenshot_count:
        missing.append(f"screenshot(min={min_screenshot_count})")
    if not web_sequence_admissible and str(page_state or "").strip().casefold() in {
        "loading",
        "line_loading",
        "transitioning",
        "needs_more_observation",
        "detection_error",
    }:
        missing.append(f"page_state({page_state})")
    if missing:
        reason = "缺少规则输入契约要求的可信证据：" + "、".join(missing)
        return "fail", reason, missing
    return "pass", None, []


def _is_app_static_visual_rule(request: BuildObservationPacketRequest) -> bool:
    """仅识别既有静态视觉合同，不扩展所有 App 或所有混合规则的证据许可。"""
    capability = _mapping(request.judge_config.get("capability_config"))
    roles = _string_list(request.input_contract.get("required_capture_roles"))
    return (
        request.rule_code in _APP_STATIC_VISUAL_RULE_CODES
        and request.executor_type in {"llm", "hybrid"}
        and capability.get("semantic_judge") == "visual_rule_judge_tool"
        and capability.get("requires_journey") is False
        and not (set(roles) - {"primary"})
    )


def _has_decodable_app_visual_screenshot(
    artifacts: list[ObservationArtifact], *, decoded: dict[str, Any],
) -> bool:
    """只接受可完整解码且匹配可信显示边界的真实 PNG，文件头和制品计数不足以放行。"""
    identity = _mapping(decoded.get("app_identity"))
    area = _mapping(identity.get("safe_area"))
    width, height = _positive_integer(area.get("width")), _positive_integer(area.get("height"))
    if width is None or height is None:
        return False
    insets = [_number(area.get(field)) for field in ("left", "right", "top", "bottom")]
    if any(inset is None or inset < 0 or not inset.is_integer() for inset in insets):
        return False
    # window_insets 的宽高已扣除四边安全距离，PNG 保留系统栏，须还原全屏边界。
    # display_bounds 的四边为零，继续严格匹配原显示尺寸，不接纳裁切安全区的图片。
    width += int(insets[0]) + int(insets[1])
    height += int(insets[2]) + int(insets[3])
    for artifact in artifacts:
        if artifact.content_type.split(";", 1)[0].strip().casefold() != "image/png":
            continue
        try:
            with Image.open(BytesIO(artifact.payload)) as image:
                if image.format != "PNG" or image.size != (width, height):
                    continue
                image.load()
                return True
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
    return False


def _compact_visual_observation(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    summary = str(value.get("visual_summary") or "").strip()
    elements = value.get("key_elements")
    element_texts: list[str] = []
    if isinstance(elements, list):
        for item in elements[:20]:
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("label") or "").strip()
                if text:
                    element_texts.append(text[:120])
            elif isinstance(item, str) and item.strip():
                element_texts.append(item.strip()[:120])
    derived = summary
    if element_texts:
        joined = "；".join(element_texts)
        derived = f"{summary} {joined}".strip() if summary else joined
    compact = {
        "source": str(value.get("source") or "app_visual"),
        "visual_summary": summary[:1000] if summary else None,
        "key_element_texts": element_texts,
        "derived_text": derived[:2000] if derived else None,
        "result_hash": value.get("result_hash"),
    }
    return {key: item for key, item in compact.items() if item is not None}


def _enforce_packet_budget(
    *,
    packet_payload: dict[str, Any],
    gate_status: str,
    gate_reason: str | None,
    missing_sources: list[str],
    quality_is_authoritative: bool = False,
) -> tuple[dict[str, Any], str, str | None, list[str]]:
    """规则事实包必须在模型预算前预先有界，超限时保留审计摘要并阻断自动结论。

    E02 条款 1.5：预算超限**不是证据不足**。`quality_is_authoritative=True`
    （齐全性结论来自清单）时，超限只记 `payload_budget` 审计事实，
    不改写清单裁决、不写 `missing_sources`——拒判由 E21 按
    `payload_budget_exceeded` 独立原因码产出。
    """
    encoded = json.dumps(packet_payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= MAX_OBSERVATION_PACKET_BYTES:
        return packet_payload, gate_status, gate_reason, missing_sources
    facts = _mapping(packet_payload.get("facts"))
    h5_packet = _mapping(packet_payload.get("identity")).get("target_platform") == "h5"
    ui_snapshot = _mapping(facts.get("ui_snapshot"))
    dom_summary = _mapping(facts.get("dom_summary"))
    _limit_packet_ui_snapshot(
        ui_snapshot,
        maximum_frames=MAX_UI_SEQUENCE_FRAMES,
        maximum_elements=MAX_UI_SEQUENCE_ELEMENTS_PER_FRAME,
        project_h5_resources=h5_packet,
    )
    ui_snapshot["budget_truncated"] = True
    dom_summary["elements"] = list(dom_summary.get("elements") or [])[:50]
    dom_summary["budget_truncated"] = True
    if h5_packet:
        # 角色快照是同一批制品的语义投影，不能漏过预算后反复触发现场补采。
        for role_fact in _mapping(facts.get("capture_roles")).values():
            if isinstance(role_fact, dict):
                _limit_packet_ui_snapshot(
                    role_fact.get("ui_snapshot"),
                    maximum_frames=MAX_UI_SEQUENCE_FRAMES,
                    maximum_elements=MAX_UI_SEQUENCE_ELEMENTS_PER_FRAME,
                    project_h5_resources=True,
                )
    facts["ui_snapshot"] = ui_snapshot
    facts["dom_summary"] = dom_summary
    facts["journey"] = _bounded_journey_items(
        list(facts.get("journey") or []),
        maximum_steps=10,
    )
    for step in facts["journey"]:
        if isinstance(step, dict):
            _limit_packet_ui_snapshot(
                step.get("ui_snapshot"),
                maximum_frames=3,
                maximum_elements=10,
                project_h5_resources=h5_packet,
            )
    facts["visible_text"] = str(facts.get("visible_text") or "")[:2_000]
    facts["network_summary"] = _truncate_runtime_summary(
        facts.get("network_summary"), maximum_entries=20
    )
    facts["console_summary"] = _truncate_runtime_summary(
        facts.get("console_summary"), maximum_entries=20
    )
    facts["action_log"] = list(facts.get("action_log") or [])[:10]
    facts["packet_budget"] = {
        "original_bytes": len(encoded),
        "maximum_bytes": MAX_OBSERVATION_PACKET_BYTES,
        "truncated": True,
    }
    packet_payload["facts"] = facts
    bounded = json.dumps(packet_payload, ensure_ascii=False, default=str).encode("utf-8")
    facts["packet_budget"]["final_bytes"] = len(bounded)
    bounded = json.dumps(packet_payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(bounded) <= MAX_OBSERVATION_PACKET_BYTES:
        # 一次有损压缩后仍保留足够事实时，不应把“原始包偏大”误判成证据不足。
        return packet_payload, gate_status, gate_reason, missing_sources

    reason = "Observation Packet 超过模型安全输入预算，压缩后仍无法安全判定。"
    if quality_is_authoritative or h5_packet:
        # E02 条款 1.5：预算超限只进审计，既不改写清单裁决也不报证据不足
        missing = list(missing_sources)
        packet_payload["quality_gate"] = {
            **_mapping(packet_payload.get("quality_gate")),
            "payload_budget_exceeded": True,
            "payload_budget_reason": reason,
        }
    else:
        missing = [*missing_sources, "packet_input_budget"]
        packet_payload["quality_gate"] = {
            **_mapping(packet_payload.get("quality_gate")),
            "status": "fail",
            "reason": reason,
            "missing_sources": missing,
        }
    if len(bounded) > MAX_OBSERVATION_PACKET_BYTES:
        _limit_packet_ui_snapshot(
            ui_snapshot,
            maximum_frames=3,
            maximum_elements=0,
            project_h5_resources=h5_packet,
        )
        dom_summary["elements"] = []
        if h5_packet:
            for role_fact in _mapping(facts.get("capture_roles")).values():
                if isinstance(role_fact, dict):
                    _limit_packet_ui_snapshot(
                        role_fact.get("ui_snapshot"), maximum_frames=3, maximum_elements=0,
                        project_h5_resources=True,
                    )
        facts["journey"] = [
            {
                key: item.get(key)
                for key in (
                    "page_capture_id",
                    "page_seq",
                    "url",
                    "page_type",
                    "page_state",
                    "evidence_artifact_ids",
                )
                if item.get(key) is not None
            }
            for item in _bounded_journey_items(
                list(facts.get("journey") or []),
                maximum_steps=5,
            )
            if isinstance(item, dict)
        ]
        facts["network_summary"] = _truncate_runtime_summary(
            facts.get("network_summary"), maximum_entries=0
        )
        facts["console_summary"] = _truncate_runtime_summary(
            facts.get("console_summary"), maximum_entries=0
        )
        facts["packet_budget"]["hard_truncated"] = True
        packet_payload["facts"] = facts
        bounded = json.dumps(packet_payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(bounded) > MAX_OBSERVATION_PACKET_BYTES:
        rule = _mapping(packet_payload.get("rule"))
        criteria = _mapping(rule.get("criteria"))
        criteria_bytes = json.dumps(criteria, ensure_ascii=False, default=str).encode("utf-8")
        rule["criteria"] = {
            "budget_truncated": True,
            "configured_keys": sorted(str(key) for key in criteria),
            "sha256": hashlib.sha256(criteria_bytes).hexdigest(),
        }
        packet_payload["rule"] = rule
        packet_payload["candidate_input"] = {"budget_truncated": True}
        facts["visible_text"] = ""
        facts["action_log"] = []
        packet_payload["facts"] = facts
        bounded = json.dumps(packet_payload, ensure_ascii=False, default=str).encode("utf-8")
    facts["packet_budget"]["final_bytes"] = len(bounded)
    if quality_is_authoritative:
        # 清单结论保持权威：预算超限不改判齐全性（E02 条款 1.5）
        return packet_payload, gate_status, gate_reason, missing
    return packet_payload, "fail", reason, missing


def _limit_packet_ui_snapshot(
    value: Any,
    *,
    maximum_frames: int,
    maximum_elements: int,
    project_h5_resources: bool = False,
) -> None:
    """压缩模型语义视图；原始 UI 证据仍完整保存在证据仓。"""
    if not isinstance(value, dict):
        return
    if value.get("sequence") is True:
        frames = value.get("frames")
        if not isinstance(frames, list):
            value["frames"] = []
            return
        selected = _evenly_spaced_items(frames, maximum_frames)
        value["frames"] = selected
        for frame in selected:
            if isinstance(frame, dict):
                _limit_packet_ui_snapshot(
                    frame, maximum_frames=maximum_frames, maximum_elements=maximum_elements,
                    project_h5_resources=project_h5_resources,
                )
                frame["budget_truncated"] = True
        return
    elements = list(value.get("elements") or [])
    census = value.get("h5_modal_census_selectors")
    if isinstance(census, list):
        # 普查成员包括关闭控件；正文压缩不能淘汰已采集的完整弹窗事实。
        retained = [
            item for item in elements
            if isinstance(item, dict) and item.get("selector") in census
        ][:maximum_elements]
        retained_selectors = {item.get("selector") for item in retained}
        remaining = [
            item for item in elements
            if not isinstance(item, dict) or item.get("selector") not in retained_selectors
        ]
        value["elements"] = [
            *retained,
            *_select_priority_ui_elements(remaining, max(0, maximum_elements - len(retained))),
        ]
        if any(selector not in retained_selectors for selector in census):
            value["h5_modal_census_truncated"] = True
    else:
        value["elements"] = _select_priority_ui_elements(elements, maximum_elements)
    if project_h5_resources:
        # 资源表是节点摘要的冗余索引；只投影保留节点，原制品及其哈希不变。
        selectors = {
            item.get("selector") for item in value["elements"] if isinstance(item, dict)
        }
        media = value.get("media")
        if isinstance(media, list):
            value["media"] = [
                item for item in media if isinstance(item, dict) and (
                    str(item.get("tag") or "").casefold() in {"video", "audio"}
                    or item.get("selector") in selectors
                )
            ]
            selectors.update(item.get("selector") for item in value["media"])
        resources = value.get("resource_summaries")
        if isinstance(resources, list):
            value["resource_summaries"] = [
                item for item in resources
                if isinstance(item, dict) and item.get("selector") in selectors
            ]


def _evenly_spaced_items(items: list[Any], maximum: int) -> list[Any]:
    """保留首尾及均匀中间帧，避免倒计时压缩只留下开始阶段。"""
    if maximum <= 0 or not items:
        return []
    if len(items) <= maximum:
        return list(items)
    if maximum == 1:
        return [items[0]]
    indexes = {round(index * (len(items) - 1) / (maximum - 1)) for index in range(maximum)}
    return [items[index] for index in sorted(indexes)]


def _select_priority_ui_elements(items: list[Any], maximum: int) -> list[Any]:
    """压缩时保留广告、弹窗、大图和交互控件，避免只截取 DOM 前部。"""
    if maximum <= 0 or not items:
        return []
    if len(items) <= maximum:
        return list(items)

    def score(index: int, item: Any) -> tuple[float, int]:
        if not isinstance(item, dict):
            return 0.0, -index
        identity = (
            " ".join(
                str(item.get(key) or "")
                for key in (
                    "selector",
                    "role",
                    "text",
                    "aria_label",
                    "alt",
                    "src",
                )
            )
            + " "
            + " ".join(str(value) for value in item.get("class_tokens") or [])
        )
        priority = 0.0
        if re.search(
            r"advert|\bad(?:s|[-_ ])|banner|promo|popup|modal|dialog|overlay|"
            r"广告|推广|关闭|跳过|下载|二维码",
            identity,
            re.IGNORECASE,
        ):
            priority += 200.0
        if str(item.get("role") or "").casefold() in {"dialog", "alertdialog"}:
            priority += 200.0
        from services.api.app.application.h5_probe_targets import (
            back_to_top_structure,
            disclosure_structure,
            payment_plan_structure,
        )

        if (item.get("h5_probe_local_display") is True
            and (payment_plan_structure(item) or back_to_top_structure(item)
                 or disclosure_structure(item))):
            priority += 200.0
        if str(item.get("tag") or "").casefold() in {"img", "video", "canvas"}:
            priority += 80.0
        rect = _mapping(item.get("rect"))
        width = _number(rect.get("width")) or 0.0
        height = _number(rect.get("height")) or 0.0
        priority += min(max(width, 0.0) * max(height, 0.0) / 5_000, 100.0)
        if item.get("href") or item.get("hit_test"):
            priority += 20.0
        return priority, -index

    anchor_count = min(2, maximum)
    selected_indexes = set(range(anchor_count))
    ranked = sorted(
        range(anchor_count, len(items)),
        key=lambda index: score(index, items[index]),
        reverse=True,
    )
    selected_indexes.update(ranked[: maximum - anchor_count])
    return [items[index] for index in sorted(selected_indexes)]


def _truncate_runtime_summary(value: Any, *, maximum_entries: int) -> dict[str, Any]:
    summary = _mapping(value)
    return {
        **summary,
        "entries": list(summary.get("entries") or [])[:maximum_entries],
        "errors": list(summary.get("errors") or [])[:maximum_entries],
        "failed_responses": list(summary.get("failed_responses") or [])[:maximum_entries],
        "budget_truncated": True,
    }


class _DomSummaryParser(HTMLParser):
    """只抽取有界结构事实，不保留完整 DOM。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tag_counts: dict[str, int] = {}
        self.elements: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.casefold()
        self.tag_counts[normalized] = self.tag_counts.get(normalized, 0) + 1
        if normalized in {"script", "style", "noscript", "template"}:
            self._ignored_depth += 1
            self._current = None
            return
        if self._ignored_depth:
            return
        if len(self.elements) >= MAX_DOM_SUMMARY_ELEMENTS:
            self._current = None
            return
        attributes = {key.casefold(): value for key, value in attrs}
        if normalized not in {
            "a",
            "button",
            "input",
            "textarea",
            "select",
            "img",
            "video",
            "audio",
            "dialog",
        } and not any(key in attributes for key in ("role", "aria-label", "data-testid")):
            self._current = None
            return
        element = {
            "tag": normalized,
            "id": attributes.get("id"),
            "role": attributes.get("role"),
            "aria_label": attributes.get("aria-label"),
            "href": _compact_resource(attributes.get("href"))[0],
            "target": attributes.get("target"),
            "rel": attributes.get("rel"),
            "src": _compact_resource(attributes.get("src"))[0],
            "type": attributes.get("type"),
            "name": attributes.get("name"),
            "accept": attributes.get("accept"),
            "class_tokens": [
                token[:MAX_CLASS_TOKEN_CHARACTERS]
                for token in str(attributes.get("class") or "").split()[:12]
            ],
            "text": "",
        }
        self.elements.append(element)
        self._current = element

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or self._current is None:
            return
        combined = f"{self._current['text']} {data}".strip()
        self._current["text"] = re.sub(r"\s+", " ", combined)[:300]

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in {"script", "style", "noscript", "template"}:
            self._ignored_depth = max(self._ignored_depth - 1, 0)
        if not self._ignored_depth:
            self._current = None


def _summarize_dom(dom: str) -> dict[str, Any]:
    if not dom:
        return {"character_count": 0, "tag_counts": {}, "elements": []}
    parser = _DomSummaryParser()
    try:
        parser.feed(dom)
    except Exception:  # noqa: BLE001 - 损坏 DOM 只降级摘要，不遮蔽其他证据
        return {
            "character_count": len(dom),
            "tag_counts": {},
            "elements": [],
            "parse_error": True,
        }
    return {
        "character_count": len(dom),
        "tag_counts": parser.tag_counts,
        "elements": parser.elements,
        "element_limit_reached": len(parser.elements) >= MAX_DOM_SUMMARY_ELEMENTS,
    }


def _compact_ui_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    if not value:
        return {}
    if value.get("sequence") is True:
        frames = value.get("frames")
        selected_frames = (
            _evenly_spaced_items(frames, MAX_UI_SEQUENCE_FRAMES) if isinstance(frames, list) else []
        )
        return {
            "schema_version": value.get("schema_version"),
            "sequence": True,
            "interval_ms": value.get("interval_ms"),
            **({"capture_role": value["capture_role"]}
               if value.get("capture_role") in {"h5_vertical_scroll_collection", "h5_payment_horizontal_collection"}
               else {}),
            **({"source": value["source"]}
               if value.get("source") == "h5_bound_action_capture_roles" else {}),
            "frames": [
                {
                    "index": item.get("index"),
                    **({"capture_id": item.get("capture_id")}
                       if value.get("source") == "h5_bound_action_capture_roles" else {}),
                    **_compact_ui_snapshot(
                        {key: field for key, field in item.items() if key != "index"}
                    ),
                }
                for item in selected_frames
                if isinstance(item, dict)
            ]
            if selected_frames
            else [],
        }
    compact = {
        key: value.get(key)
        for key in (
            "schema_version",
            "captured_at",
            "performance_now_ms",
            "url",
            "title",
            "ready_state",
            "visibility_state",
            "viewport",
            "document",
            "performance",
            "element_count",
            "element_limit_reached",
            "truncated_reason",
            "structure_signature",
            "resource_summaries",
            "screen_id",
            "package_name",
            "activity",
            "safe_area",
            "source",
            "surface_kind",
            "h5_probe_preview_roots", "h5_probe_preview_roots_truncated",
            "h5_probe_preview_root_states",
        )
        if value.get(key) is not None
    }
    elements = value.get("elements")
    surface_kind = value.get("surface_kind")
    native_controls = (
        surface_kind in {"android", "ios"}
        and value.get("source") == f"{surface_kind}_ui_hierarchy"
    )
    raw_regions = value.get("h5_scroll_regions")
    if isinstance(raw_regions, list):
        compact["h5_scroll_regions"] = [
            {
                "selector": region["selector"][:512],
                **{key: region[key] for key in ("scroll_top", "scroll_height", "client_height")},
            }
            for region in raw_regions[:16]
            if isinstance(region, dict) and isinstance(region.get("selector"), str)
            and all(
                isinstance(region.get(key), (int, float))
                and not isinstance(region[key], bool) and math.isfinite(region[key])
                for key in ("scroll_top", "scroll_height", "client_height")
            )
        ]
    compact["elements"] = (
        [
            _compact_ui_element(item, native_controls=native_controls)
            for item in _select_priority_ui_elements(
                elements,
                MAX_UI_SNAPSHOT_ELEMENTS,
            )
            if isinstance(item, dict)
        ]
        if isinstance(elements, list)
        else []
    )
    if "h5_modal_census_truncated" in value:
        # H5 普查包含容器和控件，压缩不能只留 CSS 标签后假定没有弹窗。
        raw_census = value.get("h5_modal_census_selectors")
        census_invalid = not isinstance(raw_census, list) or any(
            not isinstance(selector, str) or not selector for selector in (raw_census or [])
        )
        census = list(dict.fromkeys(
            selector for selector in (raw_census if isinstance(raw_census, list) else [])
            if isinstance(selector, str) and selector
        ))
        raw_by_selector = {
            item.get("selector"): item for item in (elements or []) if isinstance(item, dict)
        }
        by_selector = {item["selector"]: item for item in compact["elements"]}
        for selector in census:
            if selector in raw_by_selector and selector not in by_selector:
                by_selector[selector] = _compact_ui_element(raw_by_selector[selector])
        ordered = [by_selector[key] for key in census if key in by_selector]
        ordered.extend(item for key, item in by_selector.items() if key not in census)
        compact["elements"] = ordered[:MAX_UI_SNAPSHOT_ELEMENTS]
        for item in compact["elements"]:
            raw = raw_by_selector.get(item["selector"], {})
            for key in ("aria_modal", "dismiss_semantics", "h5_modal_ancestor_selectors"):
                if key in raw:
                    item[key] = _scrub_data_values(raw[key])
        compact["h5_modal_census_selectors"] = census[:MAX_UI_SNAPSHOT_ELEMENTS]
        compact["h5_modal_census_truncated"] = bool(
            value.get("h5_modal_census_truncated") is not False
            or census_invalid
            or len(census) > MAX_UI_SNAPSHOT_ELEMENTS
            or not set(census).issubset({item["selector"] for item in compact["elements"]})
        )
        for key in ("capture_degraded", "collection_error", "budget_truncated"):
            if key in value:
                compact[key] = value[key]
    return compact


def _compact_media_state(value: dict[str, Any]) -> dict[str, Any]:
    """保留播放器规则需要的有界媒体真值，不暴露资源 URL。"""
    if not value:
        return {}
    compact = {
        key: value.get(key)
        for key in (
            "schema_version",
            "source",
            "captured_at",
            "performance_now_ms",
            "item_count",
            "items_truncated",
            "capture_degraded",
            "collection_error",
            "playback_state_available",
            "native_interaction",
            "continuous_observation",
            "gap_codes",
            "limitations",
        )
        if value.get(key) is not None
    }
    raw_items = value.get("items")
    compact["items"] = [
        {
            key: item.get(key)
            for key in (
                "media_index",
                "selector",
                "tag",
                "kind",
                "source",
                "bounds",
                "current_time",
                "duration",
                "paused",
                "ended",
                "muted",
                "controls",
                "seeking",
                "ready_state",
                "playback_rate",
                "seekable_ranges",
            )
            if item.get(key) is not None
        }
        for item in list(raw_items or [])[:20]
        if isinstance(item, dict)
    ]
    raw_display = value.get("display_candidates")
    compact["display_candidates"] = [
        {
            "selector": str(item.get("selector") or "")[:MAX_SELECTOR_CHARACTERS],
            "text": str(item.get("text") or "")[:160],
            "bounds": item.get("bounds") if isinstance(item.get("bounds"), dict) else {},
        }
        for item in list(raw_display or [])[:30]
        if isinstance(item, dict)
    ]
    return compact


def _compact_ui_element(
    value: dict[str, Any], *, native_controls: bool = False,
) -> dict[str, Any]:
    allowed_keys = {
        "selector",
        "tag",
        "role",
        "text",
        "aria_label",
        "class_tokens",
        "rect",
        "hit_bounds",
        "visual_bounds",
        "style",
        "href",
        "src",
        "alt",
        "type",
        "name",
        "placeholder",
        "disabled",
        "checked",
        "visible", "interactive", "aria_checked", "aria_selected", "aria_disabled",
        "aria_expanded", "aria_controls", "parent_class_tokens", "ancestor_class_tokens",
        "parent_selector", "dom_sibling_index", "h5_probe_local_display", "form_associated", "media",
        "h5_media_instance_id", "h5_probe_plan_label", "h5_probe_controlled_region",
        "is_advertisement", "within_web_navigation", "h5_probe_preview_root",
        "h5_probe_preview_controlled_root", "h5_probe_image_resources",
        "h5_probe_gallery_binding", "h5_probe_gallery_item",
        "hit_test",
        "image",
        "attribution",
        "text_metrics",
    }
    if native_controls:
        # 原生控件事实只进入同源快照，其他平台维持原有冻结内容。
        allowed_keys.update({"clickable", "enabled", "visible"})
    element = _scrub_data_values({key: item for key, item in value.items() if key in allowed_keys})
    element["selector"] = str(element.get("selector") or "")[:MAX_SELECTOR_CHARACTERS]
    class_tokens = element.get("class_tokens")
    element["class_tokens"] = (
        [str(token)[:MAX_CLASS_TOKEN_CHARACTERS] for token in class_tokens[:12]]
        if isinstance(class_tokens, list)
        else []
    )
    for key in ("href", "src"):
        resource, summary = _compact_resource(element.get(key))
        element[key] = resource
        if summary is not None:
            element[f"{key}_resource_summary"] = summary
    element["text"] = str(element.get("text") or "")[:500]
    for key in ("parent_class_tokens", "ancestor_class_tokens"):
        if isinstance(element.get(key), list):
            element[key] = [str(token)[:MAX_CLASS_TOKEN_CHARACTERS] for token in element[key][:16]]
    if "h5_probe_plan_label" in element:
        element["h5_probe_plan_label"] = str(element["h5_probe_plan_label"] or "")[:500]
    style = element.get("style")
    if isinstance(style, dict):
        element["style"] = {
            key: style.get(key)
            for key in (
                "display",
                "visibility",
                "opacity",
                "position",
                "z_index",
                "overflow",
                "overflow_x",
                "overflow_y",
                "font_size",
                "font_weight",
                "object_fit",
                "text_overflow",
                "white_space",
                "webkit_line_clamp",
                "line_clamp",
            )
            if style.get(key) is not None
        }
    metrics = element.get("text_metrics")
    if isinstance(metrics, dict):
        element["text_metrics"] = {
            key: metrics.get(key)
            for key in (
                "scroll_width",
                "client_width",
                "offset_width",
                "scroll_height",
                "client_height",
                "offset_height",
                "horizontal_overflow",
                "vertical_overflow",
                "text_overflow_ellipsis",
                "line_clamp",
            )
            if metrics.get(key) is not None
        }
    else:
        element.pop("text_metrics", None)
    return element


def _media_state_from_ui_snapshot(ui_snapshot: dict[str, Any]) -> dict[str, Any]:
    """把 Android Accessibility 媒体控件投影成有界 media_state。"""
    from services.api.app.application.app_surface_evidence import (
        project_media_state_from_ui_snapshot,
    )

    return project_media_state_from_ui_snapshot(ui_snapshot)


def _content_attribution_fact(
    *,
    page_type: str | None,
    ui_snapshot: dict[str, Any],
    journey_steps: tuple[ObservationJourneyStep, ...],
) -> dict[str, Any]:
    """区分已发布 UGC 与未知来源；署名不等于当前账号拥有内容。"""

    elements = ui_snapshot.get("elements")
    elements = elements if isinstance(elements, list) else []
    bylines = [
        dict(attribution)
        for item in elements
        if isinstance(item, dict) and isinstance((attribution := item.get("attribution")), dict)
    ][:20]
    normalized_page_type = str(page_type or "").casefold()
    journey_page_types = {
        str(step.page_type or "").casefold() for step in journey_steps if step.page_type
    }
    semantic_ugc = any(
        re.search(
            r"ugc|comment|reply|post|user[-_ ]?content|评论|回复|帖子",
            " ".join(
                (
                    str(item.get("selector") or ""),
                    str(item.get("role") or ""),
                    " ".join(str(value) for value in item.get("class_tokens") or []),
                )
            ),
            re.IGNORECASE,
        )
        for item in elements
        if isinstance(item, dict)
    )
    ugc_scope = (
        normalized_page_type == "post_list"
        or semantic_ugc
        or (normalized_page_type == "content_detail" and "post_list" in journey_page_types)
    )
    basis: list[str] = []
    if normalized_page_type == "post_list":
        basis.append("page_type:post_list")
    if semantic_ugc:
        basis.append("ui_semantics")
    if normalized_page_type == "content_detail" and "post_list" in journey_page_types:
        basis.append("journey:post_list_to_content_detail")
    if bylines:
        basis.append("accessibility_byline")
    return {
        "scope": "ugc" if ugc_scope else "platform_or_unknown",
        "basis": basis,
        "bylines": bylines,
        "ownership": {
            "status": "unverified",
            "owner_account_id": None,
            "basis": [],
        },
    }


def _compact_resource(value: Any) -> tuple[str | None, dict[str, Any] | None]:
    if value is None:
        return None, None
    raw = str(value)
    if not raw.startswith("data:"):
        return raw[:MAX_RESOURCE_URL_CHARACTERS], None
    summarized = re.fullmatch(
        r"data:(?P<mime>[^;]{1,200});bytes=(?P<bytes>\d+);sha256=(?P<sha>[0-9a-f]{64})",
        raw,
    )
    if summarized is not None:
        return raw, {
            "scheme": "data",
            "mime": summarized.group("mime"),
            "byte_length": int(summarized.group("bytes")),
            "sha256": summarized.group("sha"),
        }
    header, _, payload = raw.partition(",")
    metadata = header[5:].split(";")
    mime = metadata[0] or "text/plain"
    try:
        content = (
            base64.b64decode(payload, validate=False)
            if any(item.casefold() == "base64" for item in metadata[1:])
            else unquote_to_bytes(payload)
        )
    except (TypeError, ValueError):
        content = payload.encode("utf-8", errors="replace")
    sha256 = hashlib.sha256(content).hexdigest()
    summary = {
        "scheme": "data",
        "mime": mime[:200],
        "byte_length": len(content),
        "sha256": sha256,
    }
    return f"data:{summary['mime']};bytes={len(content)};sha256={sha256}", summary


def _scrub_data_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _scrub_data_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_data_values(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return _compact_resource(value)[0]
    return value


def _compact_journey(
    steps: tuple[ObservationJourneyStep, ...],
) -> list[dict[str, Any]]:
    """构造有界可信旅程，供跨页规则使用，不把模型摘要当成事实。"""
    compact: list[dict[str, Any]] = []
    bounded_steps = _bounded_journey_items(
        list(steps),
        maximum_steps=MAX_JOURNEY_STEPS,
    )
    for step in bounded_steps:
        artifacts_by_type: dict[str, list[ObservationArtifact]] = {}
        for artifact in step.artifacts:
            artifacts_by_type.setdefault(artifact.artifact_type, []).append(artifact)
        text = _first_text(artifacts_by_type.get("visible_text", []))
        snapshot = _first_json_object(artifacts_by_type.get("ui_snapshot", []))
        snapshot = _compact_ui_snapshot(snapshot)
        media_state = _compact_media_state(
            _first_json_object(artifacts_by_type.get("media_state", []))
        )
        snapshot = {
            key: snapshot.get(key)
            for key in (
                "schema_version",
                "url",
                "viewport",
                "document",
                "element_count",
                "truncated_reason",
                "structure_signature",
            )
            if snapshot.get(key) is not None
        }
        compact.append(
            {
                "page_capture_id": step.page_capture_id,
                "page_seq": step.page_seq,
                "url": step.page_url,
                "title": step.page_title,
                "page_type": step.page_type,
                "page_state": step.page_state,
                "visible_text_excerpt": text[:MAX_JOURNEY_TEXT_CHARACTERS],
                "visible_text_character_count": len(text),
                "ui_snapshot": snapshot,
                "media_state": media_state,
                "action_log": [
                    {
                        key: item.get(key)
                        for key in (
                            "action",
                            "selector",
                            "result",
                            "transition_kind",
                            "navigation",
                            "state_diff",
                        )
                        if item.get(key) is not None
                    }
                    for item in step.action_log[:MAX_JOURNEY_ACTIONS]
                    if isinstance(item, dict)
                ],
                "evidence_artifact_ids": [artifact.artifact_id for artifact in step.artifacts],
            }
        )
    return compact


def _app_observation_snapshot_sequence(
    *,
    source: ObservationSource,
    current_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """把同一 App Surface Phase 的真实重新观察投影为有界多帧证据。

    App 的 ``TimedObservationIntent`` 每次只提交一张独立 Capture。规则侧只有在
    当前 Capture 与历史步骤都明确属于同一生产 Run、同一 Surface Phase 且角色均为
    ``observation`` 时，才允许把各自清单中的 ``ui_snapshot`` 串成序列。普通动作前后
    Capture、跨阶段旅程和 Web temporal trace 均不进入这条投影。
    """

    if (
        str(source.surface_kind or "").casefold() not in {"android", "ios"}
        or source.capture_role != "observation"
        or not source.surface_phase_run_id
        or not current_snapshot
    ):
        return current_snapshot

    frames_by_capture: dict[str, tuple[int, dict[str, Any]]] = {}
    for step in source.journey_steps:
        if (
            step.capture_role != "observation"
            or step.surface_phase_run_id != source.surface_phase_run_id
        ):
            continue
        artifacts_by_type: dict[str, list[ObservationArtifact]] = {}
        for artifact in step.artifacts:
            artifacts_by_type.setdefault(artifact.artifact_type, []).append(artifact)
        snapshot = _compact_ui_snapshot(
            _first_json_object(artifacts_by_type.get("ui_snapshot", []))
        )
        if not snapshot and artifacts_by_type.get("ui_tree"):
            try:
                from services.api.app.application.app_surface_evidence import (
                    project_ui_snapshot_from_hierarchy,
                )

                snapshot = _compact_ui_snapshot(
                    project_ui_snapshot_from_hierarchy(
                        _first_text(artifacts_by_type["ui_tree"]),
                    )
                )
            except (ObservationPacketValidationError, ValueError, ET.ParseError):
                snapshot = {}
        if snapshot:
            if step.captured_at and not snapshot.get("captured_at"):
                snapshot["captured_at"] = step.captured_at
            frames_by_capture[step.page_capture_id] = (step.page_seq, snapshot)

    current_seq = max(
        (
            step.page_seq
            for step in source.journey_steps
            if step.page_capture_id == source.page_capture_id
        ),
        default=len(source.journey_steps) + 1,
    )
    latest_snapshot = _compact_ui_snapshot(dict(current_snapshot))
    if source.captured_at and not latest_snapshot.get("captured_at"):
        latest_snapshot["captured_at"] = source.captured_at
    frames_by_capture[source.page_capture_id] = (
        current_seq,
        latest_snapshot,
    )
    ordered = sorted(
        frames_by_capture.items(),
        key=lambda item: (item[1][0], item[0]),
    )
    selected = _evenly_spaced_items(ordered, MAX_UI_SEQUENCE_FRAMES)
    if len(selected) < 2:
        return current_snapshot
    return {
        "schema_version": "app-ui-observation-sequence.v1",
        "sequence": True,
        "source": "app_surface_phase_observations",
        "surface_phase_run_id": source.surface_phase_run_id,
        "frames": [
            {
                "index": index,
                "capture_id": capture_id,
                **snapshot,
            }
            for index, (capture_id, (_page_seq, snapshot)) in enumerate(selected)
        ],
    }


def _bounded_journey_items(
    items: list[Any],
    *,
    maximum_steps: int,
) -> list[Any]:
    """压缩旅程时同时保留入口与最近步骤，避免长任务丢失启动态。"""
    if maximum_steps <= 0:
        return []
    if len(items) <= maximum_steps:
        return list(items)
    head_count = min(3, max(1, maximum_steps // 3))
    tail_count = maximum_steps - head_count
    if tail_count <= 0:
        return list(items[:head_count])
    return [*items[:head_count], *items[-tail_count:]]


def _summarize_network(value: Any) -> dict[str, Any]:
    entries = value.get("entries") if isinstance(value, dict) else value
    entries = entries if isinstance(entries, list) else []
    bounded = [
        _compact_runtime_entry(item, kind="network")
        for item in entries[:MAX_RUNTIME_EVENTS]
        if isinstance(item, dict)
    ]
    failed = [
        item
        for item in bounded
        if isinstance(item.get("status"), int) and int(item["status"]) >= 400
    ]
    return {
        "entry_count": len(entries),
        "entries": bounded,
        "failed_responses": failed,
        "truncated": len(entries) > MAX_RUNTIME_EVENTS,
    }


def _summarize_app_network(value: dict[str, Any]) -> dict[str, Any]:
    """App network_trace 摘要（无 MITM HAR 时的最小网络事实）。"""
    if not value:
        return {}
    entries = value.get("entries") if isinstance(value.get("entries"), list) else []
    return {
        "source": value.get("source"),
        "status": value.get("status"),
        "proxy_attached": bool(value.get("proxy_attached")),
        "entry_count": int(value.get("entry_count") or len(entries)),
        "entries": entries[:MAX_RUNTIME_EVENTS],
        "truncated": False,
        "checksum": value.get("checksum"),
    }


def _summarize_console(value: Any) -> dict[str, Any]:
    entries = value if isinstance(value, list) else []
    bounded = [
        _compact_runtime_entry(item, kind="console")
        for item in entries[:MAX_RUNTIME_EVENTS]
        if isinstance(item, dict)
    ]
    errors = [
        item
        for item in bounded
        if str(item.get("type") or item.get("level") or "").casefold()
        in {"error", "warning", "warn"}
    ]
    return {
        "entry_count": len(entries),
        "entries": bounded,
        "errors": errors,
        "truncated": len(entries) > MAX_RUNTIME_EVENTS,
    }


def _compact_script_recording(value: dict[str, Any]) -> dict[str, Any]:
    """只留截圖證據編號和動作，不把本機路徑或圖片位元組放進觀察包。"""

    shots: list[dict[str, str]] = []
    for shot in value.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        name = str(shot.get("name") or "").strip()
        artifact_id = str(shot.get("artifact_id") or "").strip()
        if name and artifact_id:
            shots.append({"name": name, "artifact_id": artifact_id})
    operations: list[dict[str, str]] = []
    for operation in value.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        kept = {
            key: str(operation.get(key))
            for key in ("kind", "shot_after")
            if operation.get(key) is not None
        }
        if kept:
            operations.append(kept)
    return {
        "verified": value.get("verified") is True,
        "shots": shots,
        "operations": operations,
    }


def _verified_script_shot_ids(packet_payload: dict[str, Any]) -> tuple[str, ...]:
    facts = packet_payload.get("facts") if isinstance(packet_payload, dict) else None
    if not isinstance(facts, dict):
        return ()
    recordings: list[dict[str, Any]] = []
    direct = facts.get("script_recording")
    if isinstance(direct, dict):
        recordings.append(direct)
    for item in facts.get("action_log") or []:
        if isinstance(item, dict) and isinstance(item.get("script_recording"), dict):
            recordings.append(item["script_recording"])
    shots: list[str] = []
    seen: set[str] = set()
    for recording in recordings:
        for shot in recording.get("shots") or []:
            if not isinstance(shot, dict):
                continue
            artifact_id = str(shot.get("artifact_id") or "").strip()
            if not artifact_id or artifact_id in seen:
                continue
            seen.add(artifact_id)
            shots.append(artifact_id)
    return tuple(shots)


async def _load_verified_script_screenshots(
    packet_payload: dict[str, Any],
) -> tuple[tuple[str, bytes], ...]:
    """腳本已經存進任務的截圖，按海角的截圖證據讀回。讀不到的那張略過，不另加通過條件。"""

    shots = _verified_script_shot_ids(packet_payload)
    if not shots:
        return ()
    from services.api.app.application.app_screenshot_artifacts import (
        get_app_screenshot_artifact_service,
    )

    service = get_app_screenshot_artifact_service()
    loaded: list[tuple[str, bytes]] = []
    for artifact_id in shots:
        try:
            _ref, content = await service.read_png(artifact_id)
        except Exception:  # noqa: BLE001 - 這一張讀不到就略過，其餘截圖仍留給 Agent
            continue
        if not isinstance(content, bytes) or not content.startswith(b"\x89PNG"):
            continue
        loaded.append((artifact_id, content))
    return tuple(loaded)


def _compact_action_log(items: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [
        _compact_runtime_entry(item, kind="action")
        for item in items[:MAX_ACTION_LOG_ITEMS]
        if isinstance(item, dict)
    ]


def _compact_runtime_entry(value: dict[str, Any], *, kind: str) -> dict[str, Any]:
    allowed = {
        "network": {
            "event",
            "method",
            "url",
            "status",
            "resource_type",
            "error",
        },
        "console": {"type", "level", "text"},
        "action": {
            "action",
            "selector",
            "result",
            "before_url",
            "after_url",
            "new_page_urls",
            "transition_kind",
            "navigation",
            "purpose",
            "semantic_target",
            "status",
            "execution_outcome",
            "verification_outcome",
            "intent_id",
            "action_execution_id",
            "source_capture_id",
            "result_capture_id",
            "error_code",
            "target_before",
            "target_after",
            "state_diff",
            "target_fraction",
            "target_time_seconds",
            "media_before",
            "media_after",
            "fault_url_pattern_length",
            "fault_url_pattern_sha256",
            "matched_request_count",
            "matched_request_urls",
            "matched_requests_truncated",
            "route_restored",
            "reload_error",
            "transient_message_observation",
            "transient_messages",
            "ad_navigation_evidence",
            "evidence_capture_roles",
            "interaction_feedback",
            "gap_codes",
            "script_recording",
        },
    }.get(kind, set())
    compact: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if item is None:
            continue
        if key == "url":
            compact[key] = str(item)[:MAX_RESOURCE_URL_CHARACTERS]
        elif key == "selector":
            compact[key] = str(item)[:MAX_SELECTOR_CHARACTERS]
        elif key == "script_recording" and isinstance(item, dict):
            compact[key] = _compact_script_recording(item)
        elif key == "transient_messages" and isinstance(item, list):
            compact[key] = [
                _compact_transient_message(value)
                for value in item[:MAX_TRANSIENT_MESSAGES]
                if isinstance(value, dict)
            ]
        elif key == "gap_codes" and isinstance(item, (list, tuple)):
            compact[key] = [str(value)[:80] for value in list(item)[:8] if str(value).strip()]
        elif key == "ad_navigation_evidence" and isinstance(item, dict):
            compact[key] = _compact_ad_navigation_evidence(item)
        elif key == "navigation" and isinstance(item, dict):
            compact[key] = _compact_navigation_fact(item)
        elif key == "new_page_urls" and isinstance(item, list):
            compact[key] = [
                _compact_navigation_url(value) for value in item[:10] if str(value).strip()
            ]
        elif key == "matched_request_urls" and isinstance(item, list):
            compact[key] = [
                _compact_navigation_url(value) for value in item[:20] if str(value).strip()
            ]
        elif key in {"before_url", "after_url"}:
            compact[key] = _compact_navigation_url(item)
        elif isinstance(item, str):
            compact[key] = item[:1_000]
        elif isinstance(item, dict):
            compact[key] = {
                str(nested_key)[:100]: (
                    nested_value[:500]
                    if isinstance(nested_value, str)
                    else nested_value
                    if isinstance(nested_value, (bool, int, float)) or nested_value is None
                    else str(nested_value)[:500]
                )
                for nested_key, nested_value in list(item.items())[:20]
            }
        elif isinstance(item, (bool, int, float)):
            compact[key] = item
        else:
            compact[key] = str(item)[:1_000]
    return compact


def _compact_transient_message(value: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: value.get(key)
        for key in (
            "event_id",
            "kind",
            "platform_ui_confidence",
            "selector",
            "text",
            "role",
            "aria_live",
            "class_tokens",
            "position_zone",
            "first_seen_ms",
            "last_seen_ms",
            "disappeared_at_ms",
            "observation_window_ms",
            "rect",
            "viewport",
            "style",
        )
        if value.get(key) not in (None, "", [], {})
    }
    if "selector" in compact:
        compact["selector"] = str(compact["selector"])[:MAX_SELECTOR_CHARACTERS]
    if "text" in compact:
        compact["text"] = str(compact["text"])[:500]
    if isinstance(compact.get("class_tokens"), list):
        compact["class_tokens"] = [
            str(item)[:MAX_CLASS_TOKEN_CHARACTERS] for item in compact["class_tokens"][:10]
        ]
    for key in ("rect", "viewport", "style"):
        nested = compact.get(key)
        if isinstance(nested, dict):
            compact[key] = {
                str(nested_key)[:100]: (
                    nested_value[:200] if isinstance(nested_value, str) else nested_value
                )
                for nested_key, nested_value in list(nested.items())[:12]
                if isinstance(nested_value, (str, bool, int, float)) or nested_value is None
            }
    return compact


def _compact_ad_navigation_evidence(value: dict[str, Any]) -> dict[str, Any]:
    """只开放广告源页与落地页比对所需的有界结构化字段。"""
    source = value.get("source") if isinstance(value.get("source"), dict) else {}
    landing = value.get("landing") if isinstance(value.get("landing"), dict) else {}
    target = source.get("target") if isinstance(source.get("target"), dict) else {}
    compact_target = _compact_ad_source_target(target)
    anchors = landing.get("semantic_anchors")
    compact = {
        "schema_version": str(value.get("schema_version") or "")[:100],
        "status": str(value.get("status") or "")[:100],
        "navigation_mode": str(value.get("navigation_mode") or "")[:100],
        "pair_complete": value.get("pair_complete") is True,
        "source": {
            "url": _compact_navigation_url(source.get("url")),
            "screenshot_ref": str(source.get("screenshot_ref") or "")[:200],
            "capture_id": str(source.get("capture_id") or "")[:200],
            "artifact_id": str(source.get("artifact_id") or "")[:200],
            "package_name": str(source.get("package_name") or "")[:300],
            "activity": str(source.get("activity") or "")[:500],
            "page_type": str(source.get("page_type") or "")[:100],
            **({"target": compact_target} if compact_target else {}),
        },
        "landing": {
            "url": _compact_navigation_url(landing.get("url")),
            "title": str(landing.get("title") or "")[:500],
            "screenshot_ref": str(landing.get("screenshot_ref") or "")[:200],
            "capture_id": str(landing.get("capture_id") or "")[:200],
            "artifact_id": str(landing.get("artifact_id") or "")[:200],
            "package_name": str(landing.get("package_name") or "")[:300],
            "activity": str(landing.get("activity") or "")[:500],
            "semantic_anchors": [str(item)[:160] for item in anchors[:20] if str(item).strip()]
            if isinstance(anchors, list)
            else [],
        },
    }
    reason = str(value.get("reason") or "").strip()
    if reason:
        compact["reason"] = reason[:500]
    return compact


def _compact_navigation_url(value: Any) -> str:
    """导航事实只保留 scheme/host/path，剥离凭据、查询参数和 fragment。"""
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit(
        (parsed.scheme.casefold(), netloc, parsed.path[:MAX_RESOURCE_URL_CHARACTERS], "", "")
    )[:MAX_RESOURCE_URL_CHARACTERS]


def _compact_navigation_fact(value: dict[str, Any]) -> dict[str, Any]:
    urls = value.get("new_page_urls")
    return {
        "before_url": _compact_navigation_url(value.get("before_url")),
        "after_url": _compact_navigation_url(value.get("after_url")),
        "current_url_changed": value.get("current_url_changed") is True,
        "new_page_count": max(_integer(value.get("new_page_count")), 0),
        "new_page_urls": [_compact_navigation_url(item) for item in urls[:10] if str(item).strip()]
        if isinstance(urls, list)
        else [],
        "new_pages_truncated": value.get("new_pages_truncated") is True,
        "closed_after_observation_count": max(
            _integer(value.get("closed_after_observation_count")), 0
        ),
    }


def _compact_ad_source_target(target: dict[str, Any]) -> dict[str, Any]:
    class_tokens = target.get("class_tokens")
    rect = target.get("rect")
    compact = {
        key: str(target.get(key) or "")[:limit]
        for key, limit in {
            "selector": MAX_SELECTOR_CHARACTERS,
            "tag": 100,
            "role": 100,
            "text": 500,
            "aria_label": 500,
            "alt": 500,
        }.items()
        if str(target.get(key) or "").strip()
    }
    if isinstance(class_tokens, list):
        compact["class_tokens"] = [
            str(item)[:MAX_CLASS_TOKEN_CHARACTERS]
            for item in class_tokens[:10]
            if str(item).strip()
        ]
    if isinstance(rect, dict):
        compact["rect"] = {
            str(key)[:100]: item
            for key, item in list(rect.items())[:8]
            if isinstance(item, (bool, int, float)) or item is None
        }
    for key in ("href", "src"):
        value = str(target.get(key) or "").strip()
        if value:
            compact[key] = _compact_navigation_url(value)
    return compact


def _first_text(artifacts: list[ObservationArtifact]) -> str:
    if not artifacts:
        return ""
    try:
        return artifacts[0].payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ObservationPacketValidationError("文本证据不是有效 UTF-8") from exc


def _first_json_object(artifacts: list[ObservationArtifact]) -> dict[str, Any]:
    first: dict[str, Any] = {}
    for artifact in artifacts:
        value = _json_value(artifact)
        if not isinstance(value, dict):
            continue
        if not first:
            first = value
        if value.get("sequence") is True:
            return value
    return first


def _first_json_value(artifacts: list[ObservationArtifact]) -> Any:
    if not artifacts:
        return None
    return _json_value(artifacts[0])


def _json_value(artifact: ObservationArtifact) -> Any:
    try:
        return json.loads(artifact.payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObservationPacketValidationError("JSON 证据内容无效") from exc


def _summarize_screenshot_evidence(
    artifacts: list[ObservationArtifact],
) -> list[dict[str, Any]]:
    """公开截图序列的角色和真实 Artifact 引用，不把图像内容写入 Packet。"""
    values: list[dict[str, Any]] = []
    for artifact in artifacts[:10]:
        content_type = artifact.content_type.split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            continue
        try:
            value = json.loads(artifact.payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        frames = value.get("frames") if isinstance(value, dict) else None
        if not isinstance(frames, list) or value.get("sequence") is not True:
            continue
        compact_frames: list[dict[str, Any]] = []
        artifact_id = str(artifact.artifact_id)[:200]
        purpose = str(value.get("purpose") or "")[:100]
        for position, frame in enumerate(frames[:10]):
            if not isinstance(frame, dict):
                continue
            frame_index = frame.get("index", position)
            if isinstance(frame_index, bool) or not isinstance(frame_index, int):
                frame_index = position
            role = str(frame.get("role") or "").strip()[:100]
            payload_valid = _decode_screenshot_frame_payload(frame) is not None
            compact_frames.append(
                {
                    "index": frame_index,
                    **({"role": role} if role else {}),
                    "artifact_ref": f"{artifact_id}#frame-{frame_index}",
                    "payload_valid": payload_valid,
                    **(
                        {"logical_ref": f"{purpose}#frame-{frame_index}"}
                        if purpose and role in {"source", "landing"}
                        else {}
                    ),
                }
            )
        values.append(
            {
                "artifact_id": artifact_id,
                "purpose": purpose,
                "frame_count": len(frames),
                "frames": compact_frames,
                "truncated": len(frames) > len(compact_frames),
            }
        )
    return values


def _project_app_ad_landing_screenshot_pair(
    source: ObservationSource,
) -> dict[str, Any]:
    """从同一 App Action 的 before/after Capture 投影真实广告截图对。"""

    empty = {"artifacts": (), "screenshot_evidence": ()}
    if str(source.surface_kind or "").casefold() not in _APP_SURFACE_KINDS:
        return empty
    complete_actions = [
        action
        for action in source.action_log
        if isinstance(action, dict)
        and isinstance(action.get("ad_navigation_evidence"), dict)
        and action["ad_navigation_evidence"].get("status") == "observed"
        and action["ad_navigation_evidence"].get("pair_complete") is True
        and action["ad_navigation_evidence"].get("navigation_mode")
        == "external_browser"
    ]
    if len(complete_actions) != 1 or source.capture_role != "after_action":
        return empty
    action = complete_actions[0]
    evidence = _mapping(action.get("ad_navigation_evidence"))
    source_fact = _mapping(evidence.get("source"))
    landing_fact = _mapping(evidence.get("landing"))
    source_capture_id = str(source_fact.get("capture_id") or "")
    landing_capture_id = str(landing_fact.get("capture_id") or "")
    source_artifact_id = str(source_fact.get("artifact_id") or "")
    landing_artifact_id = str(landing_fact.get("artifact_id") or "")
    if (
        not source_capture_id
        or landing_capture_id != source.page_capture_id
        or source.parent_capture_id != source_capture_id
        or not source_artifact_id
        or not landing_artifact_id
        or source_artifact_id == landing_artifact_id
    ):
        return empty

    artifacts_by_capture: dict[str, dict[str, ObservationArtifact]] = {}
    artifacts_by_capture[source.page_capture_id] = {
        artifact.artifact_id: artifact for artifact in source.artifacts
    }
    for step in source.journey_steps:
        artifacts_by_capture.setdefault(step.page_capture_id, {}).update(
            {artifact.artifact_id: artifact for artifact in step.artifacts}
        )
    source_artifact = artifacts_by_capture.get(source_capture_id, {}).get(
        source_artifact_id
    )
    landing_artifact = artifacts_by_capture.get(landing_capture_id, {}).get(
        landing_artifact_id
    )
    if not _is_valid_png_observation_artifact(source_artifact) or not (
        _is_valid_png_observation_artifact(landing_artifact)
    ):
        return empty
    execution_id = str(action.get("action_execution_id") or "")
    return {
        "artifacts": (source_artifact, landing_artifact),
        "screenshot_evidence": (
            {
                "artifact_id": execution_id or landing_artifact_id,
                "purpose": "ad_landing_consistency",
                "frame_count": 2,
                "frames": [
                    {
                        "index": 0,
                        "role": "source",
                        "artifact_ref": source_artifact_id,
                        "logical_ref": "ad_landing_consistency#frame-0",
                        "payload_valid": True,
                    },
                    {
                        "index": 1,
                        "role": "landing",
                        "artifact_ref": landing_artifact_id,
                        "logical_ref": "ad_landing_consistency#frame-1",
                        "payload_valid": True,
                    },
                ],
                "truncated": False,
                "source": "app_action_capture_pair",
            },
        ),
    }


def _is_valid_png_observation_artifact(
    artifact: ObservationArtifact | None,
) -> bool:
    return bool(
        isinstance(artifact, ObservationArtifact)
        and artifact.artifact_type == "screenshot"
        and artifact.content_type.split(";", 1)[0].strip().casefold() == "image/png"
        and artifact.payload.startswith(b"\x89PNG\r\n\x1a\n")
    )


def _merge_observation_artifacts(
    primary: tuple[ObservationArtifact, ...],
    extra: tuple[ObservationArtifact, ...],
) -> tuple[ObservationArtifact, ...]:
    merged: dict[str, ObservationArtifact] = {}
    for artifact in (*primary, *extra):
        merged.setdefault(artifact.artifact_id, artifact)
    return tuple(merged.values())


def _decode_screenshot_payloads(
    artifacts: list[ObservationArtifact],
    *, limit: int | None = 6,
) -> tuple[tuple[str, bytes, str], ...]:
    images: list[tuple[str, bytes, str]] = []
    for artifact in artifacts:
        content_type = artifact.content_type.split(";", 1)[0].strip().casefold()
        if content_type == "image/png":
            if artifact.payload.startswith(b"\x89PNG\r\n\x1a\n"):
                images.append((artifact.artifact_id, artifact.payload, "image/png"))
            continue
        if content_type != "application/json":
            continue
        try:
            value = json.loads(artifact.payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        frames = value.get("frames") if isinstance(value, dict) else None
        if not isinstance(frames, list):
            continue
        for index, frame in enumerate(frames):
            if not isinstance(frame, dict):
                continue
            payload = _decode_screenshot_frame_payload(frame)
            if payload is None:
                continue
            frame_index = frame.get("index", index)
            images.append(
                (
                    f"{artifact.artifact_id}#frame-{frame_index}",
                    payload,
                    "image/png",
                )
            )
    return tuple(images if limit is None else images[:limit])


def _decode_screenshot_frame_payload(frame: dict[str, Any]) -> bytes | None:
    """只接受可严格解码且带 PNG 文件头的截图帧。"""
    if frame.get("encoding") != "base64":
        return None
    encoded = frame.get("data")
    if not isinstance(encoded, str):
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    return payload if payload.startswith(b"\x89PNG\r\n\x1a\n") else None


def _sanitize_agent_input(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    forbidden = {
        "base64",
        "dom",
        "dom_snapshot",
        "full_dom",
        "html",
        "image_base64",
        "page_source",
        "screenshot_base64",
        "visible_text",
    }
    # E26：键名黑名单挡不住藏在自定义键下的凭证与原文，统一再过一遍脱敏
    compact = redact_audit_payload(
        {key: item for key, item in value.items() if key not in forbidden}
    )
    if not isinstance(compact, dict):  # pragma: no cover - 超限已由下方分支表达
        return compact
    encoded = json.dumps(compact, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= MAX_AGENT_INPUT_BYTES:
        return compact
    return {
        "truncated": True,
        "keys": sorted(str(key) for key in compact),
        "byte_count": len(encoded),
    }


def project_rule_input_contract(value: dict[str, Any]) -> dict[str, Any]:
    """规则要求的安全投影有损时显式阻断判读，不能把删减后的合同当成完整要求。"""
    projected = redact_audit_payload(value)
    if projected != value:
        return {**projected, "projection_incomplete": True}
    return projected


def _compact_mapping(value: Any) -> dict[str, Any]:
    mapping = _mapping(value)
    return {
        key: mapping.get(key)
        for key in (
            "schema_version",
            "triggers",
            "min_confidence",
            "reviewer_group",
        )
        if mapping.get(key) is not None
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item or "").strip())]


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _positive_integer(value: Any) -> int | None:
    integer = _integer(value)
    return integer if integer > 0 else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None
