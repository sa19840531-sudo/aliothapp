"""App Screenshot Understanding 的不可变视觉事实合同。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from services.api.app.domain.exploration.app_visual_page_identity import (
    validate_app_visual_local_state,
    validated_app_visual_page_identity,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    freeze_json_mapping,
    optional_text,
    read_known_fields,
    require_mapping,
    require_sequence,
    require_text,
    stable_protocol_hash,
    thaw_json_mapping,
)

APP_VISUAL_CANDIDATE_VERSION = "app-visual-candidate.v3"
_SUPPORTED_APP_VISUAL_CANDIDATE_VERSIONS = frozenset(
    {"app-visual-candidate.v1", "app-visual-candidate.v2", APP_VISUAL_CANDIDATE_VERSION}
)
_LEGACY_APP_VISUAL_CANDIDATE_VERSIONS = frozenset(
    {"app-visual-candidate.v1", "app-visual-candidate.v2"}
)
APP_VISUAL_CONTROL_ROLES = frozenset({"dismiss", "skip"})
APP_VISUAL_ANALYSIS_VERSION = "app-visual-analysis.v2"
_SUPPORTED_APP_VISUAL_ANALYSIS_VERSIONS = frozenset(
    {"app-visual-analysis.v1", APP_VISUAL_ANALYSIS_VERSION}
)
_ALLOWED_ACTIONS = frozenset(
    {
        InteractionOperation.CLICK,
        InteractionOperation.SCROLL,
        InteractionOperation.BACK,
        InteractionOperation.OBSERVE,
        # 系统桌面没有可把目标 App 拉回前台的视觉坐标；该候选只能由
        # 平台包边界恢复逻辑签发，Provider 的 allowed_actions 仍不包含它。
        InteractionOperation.WARM_RESTORE,
    }
)
_APP_FRONTIER_SCOPES = frozenset({"local", "page", "app"})
_ORIENTATIONS = frozenset({"portrait", "landscape", "unknown"})
_PAGE_STATES = frozenset(
    {
        "unknown",
        "stable",
        "normal",
        "loaded",
        "loading",
        "line_loading",
        "content_available",
        "transitional",
        "blocked",
        "startup_ad",
        "countdown",
        "popup",
        "modal",
        "overlay",
        "dialog",
        "auth_gate",
        "auth_required",
        "login_required",
        "requires_auth",
        "system_dialog",
        "permission_dialog",
        "browser_permission",
    }
)


class AppVisualValidationError(ValueError):
    """视觉合同包含越界坐标、身份漂移或不受支持动作。"""


@dataclass(frozen=True, slots=True)
class NormalizedBounds:
    """与设备分辨率无关的左上右下边界。"""

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        values = (self.left, self.top, self.right, self.bottom)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 1
            for value in values
        ):
            raise AppVisualValidationError("normalized_bounds 必须为 [0, 1] 内的有限数值")
        if self.left >= self.right or self.top >= self.bottom:
            raise AppVisualValidationError(
                "normalized_bounds 必须满足 left < right 且 top < bottom"
            )
        for field_name in ("left", "top", "right", "bottom"):
            object.__setattr__(self, field_name, float(getattr(self, field_name)))

    @property
    def center(self) -> tuple[float, float]:
        return ((self.left + self.right) / 2, (self.top + self.bottom) / 2)

    def to_dict(self) -> dict[str, float]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> NormalizedBounds:
        values = _read(
            payload,
            known={"left", "top", "right", "bottom"},
            required={"left", "top", "right", "bottom"},
            field_name="NormalizedBounds",
        )
        return cls(
            left=values["left"],
            top=values["top"],
            right=values["right"],
            bottom=values["bottom"],
        )


@dataclass(frozen=True, kw_only=True)
class AppVisualCandidate:
    """由平台生成稳定身份、并严格绑定源截图的视觉动作候选。"""

    candidate_id: str
    source_capture_id: str
    source_image_signature: str
    source_width: int
    source_height: int
    source_orientation: str
    semantic_name: str
    action_type: InteractionOperation
    normalized_bounds: NormalizedBounds | None
    confidence: float
    risk_level: InteractionRiskLevel
    purpose: str
    control_role: str | None = None
    expected_state_hint: str | None = None
    frontier_scope: str = "local"
    # 导航入口发现来源；不参与 candidate_id 哈希，避免换标改变身份。
    discovery_source: str | None = None
    contract_version: str = APP_VISUAL_CANDIDATE_VERSION

    def __post_init__(self) -> None:
        try:
            for field_name in (
                "candidate_id",
                "source_capture_id",
                "source_image_signature",
                "semantic_name",
                "purpose",
            ):
                require_text(getattr(self, field_name), field_name)
            optional_text(self.control_role, "control_role")
            optional_text(self.expected_state_hint, "expected_state_hint")
            require_text(self.frontier_scope, "frontier_scope")
        except ProtocolContractError as exc:
            raise AppVisualValidationError(str(exc)) from exc
        _validate_image_signature(self.source_image_signature)
        _validate_dimensions(self.source_width, self.source_height)
        _validate_orientation(self.source_orientation)
        if self.action_type not in _ALLOWED_ACTIONS:
            raise AppVisualValidationError("App 视觉候选动作不允许")
        if not isinstance(self.risk_level, InteractionRiskLevel):
            raise AppVisualValidationError("risk_level 不受支持")
        if self.normalized_bounds is not None and not isinstance(
            self.normalized_bounds,
            NormalizedBounds,
        ):
            raise AppVisualValidationError("normalized_bounds 类型不正确")
        if self.action_type is InteractionOperation.CLICK and self.normalized_bounds is None:
            raise AppVisualValidationError("click 候选必须包含 normalized_bounds")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise AppVisualValidationError("confidence 必须为 [0, 1] 内的有限数值")
        if self.frontier_scope not in _APP_FRONTIER_SCOPES:
            raise AppVisualValidationError("frontier_scope 不受支持")
        if self.control_role is not None and self.control_role not in APP_VISUAL_CONTROL_ROLES:
            raise AppVisualValidationError("control_role 不受支持")
        if self.contract_version not in _SUPPORTED_APP_VISUAL_CANDIDATE_VERSIONS:
            raise AppVisualValidationError("AppVisualCandidate contract_version 不受支持")
        if (
            self.contract_version in _LEGACY_APP_VISUAL_CANDIDATE_VERSIONS
            and self.control_role is not None
        ):
            raise AppVisualValidationError("旧版视觉候选不能携带 control_role")
        if self.contract_version == "app-visual-candidate.v1" and self.frontier_scope != "local":
            raise AppVisualValidationError("v1 视觉候选不能携带任务级 Frontier 范围")
        if self.discovery_source is not None:
            from services.api.app.application.app_navigation_entry_provenance import (
                normalize_discovery_source,
            )

            normalized = normalize_discovery_source(self.discovery_source)
            if normalized is None:
                raise AppVisualValidationError("discovery_source 不受支持")
            object.__setattr__(self, "discovery_source", normalized)
        object.__setattr__(self, "confidence", float(self.confidence))
        expected_id = _candidate_id(
            source_capture_id=self.source_capture_id,
            source_image_signature=self.source_image_signature,
            semantic_name=self.semantic_name,
            action_type=self.action_type,
            normalized_bounds=self.normalized_bounds,
            control_role=self.control_role,
            frontier_scope=self.frontier_scope,
            contract_version=self.contract_version,
        )
        if self.candidate_id != expected_id:
            raise AppVisualValidationError("candidate_id 与视觉事实不一致")

    @classmethod
    def build(
        cls,
        *,
        source_capture_id: str,
        source_image_signature: str,
        source_width: int,
        source_height: int,
        source_orientation: str,
        semantic_name: str,
        action_type: InteractionOperation,
        normalized_bounds: NormalizedBounds | None,
        confidence: float,
        risk_level: InteractionRiskLevel,
        purpose: str,
        control_role: str | None = None,
        expected_state_hint: str | None = None,
        frontier_scope: str = "local",
        discovery_source: str | None = None,
    ) -> AppVisualCandidate:
        return cls(
            candidate_id=_candidate_id(
                source_capture_id=source_capture_id,
                source_image_signature=source_image_signature,
                semantic_name=semantic_name,
                action_type=action_type,
                normalized_bounds=normalized_bounds,
                control_role=control_role,
                frontier_scope=frontier_scope,
                contract_version=APP_VISUAL_CANDIDATE_VERSION,
            ),
            source_capture_id=source_capture_id,
            source_image_signature=source_image_signature,
            source_width=source_width,
            source_height=source_height,
            source_orientation=source_orientation,
            semantic_name=semantic_name,
            action_type=action_type,
            normalized_bounds=normalized_bounds,
            confidence=confidence,
            risk_level=risk_level,
            purpose=purpose,
            control_role=control_role,
            expected_state_hint=expected_state_hint,
            frontier_scope=frontier_scope,
            discovery_source=discovery_source,
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "contract_version": self.contract_version,
            "candidate_id": self.candidate_id,
            "source_capture_id": self.source_capture_id,
            "source_image_signature": self.source_image_signature,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "source_orientation": self.source_orientation,
            "semantic_name": self.semantic_name,
            "action_type": self.action_type.value,
            "normalized_bounds": (
                self.normalized_bounds.to_dict() if self.normalized_bounds is not None else None
            ),
            "confidence": self.confidence,
            "risk_level": self.risk_level.value,
            "purpose": self.purpose,
            "expected_state_hint": self.expected_state_hint,
        }
        if self.contract_version == APP_VISUAL_CANDIDATE_VERSION:
            result["control_role"] = self.control_role
        if self.contract_version != "app-visual-candidate.v1":
            result["frontier_scope"] = self.frontier_scope
        if self.discovery_source is not None:
            result["discovery_source"] = self.discovery_source
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppVisualCandidate:
        values = _read(
            payload,
            known={
                "contract_version",
                "candidate_id",
                "source_capture_id",
                "source_image_signature",
                "source_width",
                "source_height",
                "source_orientation",
                "semantic_name",
                "action_type",
                "normalized_bounds",
                "confidence",
                "risk_level",
                "purpose",
                "control_role",
                "expected_state_hint",
                "frontier_scope",
                "discovery_source",
            },
            required={
                "contract_version",
                "candidate_id",
                "source_capture_id",
                "source_image_signature",
                "source_width",
                "source_height",
                "source_orientation",
                "semantic_name",
                "action_type",
                "confidence",
                "risk_level",
                "purpose",
            },
            field_name="AppVisualCandidate",
        )
        if (
            values["contract_version"] in _LEGACY_APP_VISUAL_CANDIDATE_VERSIONS
            and "control_role" in values
        ):
            raise AppVisualValidationError("旧版视觉候选不能携带 control_role")
        raw_bounds = values.get("normalized_bounds")
        try:
            bounds = (
                NormalizedBounds.from_dict(require_mapping(raw_bounds, "normalized_bounds"))
                if raw_bounds is not None
                else None
            )
            action_type = InteractionOperation(values["action_type"])
            risk_level = InteractionRiskLevel(values["risk_level"])
        except (ProtocolContractError, ValueError) as exc:
            raise AppVisualValidationError(str(exc)) from exc
        return cls(
            candidate_id=values["candidate_id"],
            source_capture_id=values["source_capture_id"],
            source_image_signature=values["source_image_signature"],
            source_width=values["source_width"],
            source_height=values["source_height"],
            source_orientation=values["source_orientation"],
            semantic_name=values["semantic_name"],
            action_type=action_type,
            normalized_bounds=bounds,
            confidence=values["confidence"],
            risk_level=risk_level,
            purpose=values["purpose"],
            control_role=values.get("control_role"),
            expected_state_hint=values.get("expected_state_hint"),
            frontier_scope=values.get("frontier_scope", "local"),
            discovery_source=values.get("discovery_source"),
            contract_version=values["contract_version"],
        )


@dataclass(frozen=True, kw_only=True)
class AppVisualAnalysis:
    """Screenshot Understanding 的标准化、可重放输出。"""

    capture_id: str
    screenshot_artifact_id: str
    image_signature: str
    width: int
    height: int
    orientation: str
    visual_page_type: str | None
    visual_summary: str
    key_elements: tuple[str, ...]
    candidates: tuple[AppVisualCandidate, ...]
    provider_id: str
    model_id: str
    result_hash: str
    package_name: str | None = None
    activity: str | None = None
    page_state: str = "unknown"
    overlay_signature: str | None = None
    surface_facts: Mapping[str, Any] = field(default_factory=dict)
    contract_version: str = APP_VISUAL_ANALYSIS_VERSION

    def __post_init__(self) -> None:
        try:
            for field_name in (
                "capture_id",
                "screenshot_artifact_id",
                "image_signature",
                "visual_summary",
                "provider_id",
                "model_id",
                "result_hash",
            ):
                require_text(getattr(self, field_name), field_name)
            optional_text(self.visual_page_type, "visual_page_type")
            optional_text(self.package_name, "package_name")
            optional_text(self.activity, "activity")
            require_text(self.page_state, "page_state")
            optional_text(self.overlay_signature, "overlay_signature")
            surface_facts = freeze_json_mapping(self.surface_facts, "surface_facts")
        except ProtocolContractError as exc:
            raise AppVisualValidationError(str(exc)) from exc
        _validate_image_signature(self.image_signature)
        _validate_dimensions(self.width, self.height)
        _validate_orientation(self.orientation)
        if (
            "app_page_identity" in surface_facts
            and validated_app_visual_page_identity(
                surface_facts["app_page_identity"],
                capture_id=self.capture_id,
                screenshot_artifact_id=self.screenshot_artifact_id,
                image_signature=self.image_signature,
                package_name=self.package_name,
                activity=self.activity,
            )
            is None
        ):
            raise AppVisualValidationError("业务页面身份与当前截图事实不一致")
        if not validate_app_visual_local_state(
            facts=surface_facts,
            capture_id=self.capture_id,
            screenshot_artifact_id=self.screenshot_artifact_id,
            image_signature=self.image_signature,
            package_name=self.package_name,
            activity=self.activity,
        ):
            raise AppVisualValidationError("局部页面状态与当前截图事实不一致")
        key_elements = _unique_texts(self.key_elements, "key_elements")
        candidates = tuple(self.candidates)
        if any(not isinstance(item, AppVisualCandidate) for item in candidates):
            raise AppVisualValidationError("candidates 必须由 AppVisualCandidate 组成")
        if any(item.source_capture_id != self.capture_id for item in candidates):
            raise AppVisualValidationError("候选与 Analysis 的 Capture 不一致")
        if any(
            item.source_image_signature != self.image_signature
            or item.source_width != self.width
            or item.source_height != self.height
            or item.source_orientation != self.orientation
            for item in candidates
        ):
            raise AppVisualValidationError("候选与 Analysis 的截图事实不一致")
        candidate_ids = tuple(item.candidate_id for item in candidates)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise AppVisualValidationError("candidates 包含重复 candidate_id")
        if self.page_state not in _PAGE_STATES:
            raise AppVisualValidationError("page_state 不受支持")
        if self.contract_version not in _SUPPORTED_APP_VISUAL_ANALYSIS_VERSIONS:
            raise AppVisualValidationError("AppVisualAnalysis contract_version 不受支持")
        if self.contract_version == "app-visual-analysis.v1" and (
            self.page_state != "unknown" or self.overlay_signature is not None or surface_facts
        ):
            raise AppVisualValidationError("v1 视觉分析不能携带未纳入摘要的阶段事实")
        object.__setattr__(self, "key_elements", key_elements)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "surface_facts", surface_facts)
        expected_hash = stable_protocol_hash(self._hash_payload())
        if self.result_hash != expected_hash:
            raise AppVisualValidationError("result_hash 与视觉分析内容不一致")

    @classmethod
    def build(
        cls,
        *,
        capture_id: str,
        screenshot_artifact_id: str,
        image_signature: str,
        width: int,
        height: int,
        orientation: str,
        visual_page_type: str | None,
        visual_summary: str,
        key_elements: Sequence[str],
        candidates: Sequence[AppVisualCandidate],
        provider_id: str,
        model_id: str,
        package_name: str | None = None,
        activity: str | None = None,
        page_state: str = "unknown",
        overlay_signature: str | None = None,
        surface_facts: Mapping[str, Any] | None = None,
    ) -> AppVisualAnalysis:
        if surface_facts is not None and not isinstance(surface_facts, Mapping):
            raise AppVisualValidationError("surface_facts 必须为对象")
        candidate_tuple = tuple(candidates)
        key_tuple = tuple(key_elements)
        payload = _analysis_hash_payload(
            capture_id=capture_id,
            screenshot_artifact_id=screenshot_artifact_id,
            image_signature=image_signature,
            width=width,
            height=height,
            orientation=orientation,
            visual_page_type=visual_page_type,
            visual_summary=visual_summary,
            key_elements=key_tuple,
            candidates=candidate_tuple,
            provider_id=provider_id,
            model_id=model_id,
            package_name=package_name,
            activity=activity,
            page_state=page_state,
            overlay_signature=overlay_signature,
            surface_facts=surface_facts or {},
            contract_version=APP_VISUAL_ANALYSIS_VERSION,
        )
        return cls(
            capture_id=capture_id,
            screenshot_artifact_id=screenshot_artifact_id,
            image_signature=image_signature,
            width=width,
            height=height,
            orientation=orientation,
            visual_page_type=visual_page_type,
            visual_summary=visual_summary,
            key_elements=key_tuple,
            candidates=candidate_tuple,
            provider_id=provider_id,
            model_id=model_id,
            result_hash=stable_protocol_hash(payload),
            package_name=package_name,
            activity=activity,
            page_state=page_state,
            overlay_signature=overlay_signature,
            surface_facts=surface_facts or {},
        )

    def _hash_payload(self) -> dict[str, Any]:
        return _analysis_hash_payload(
            capture_id=self.capture_id,
            screenshot_artifact_id=self.screenshot_artifact_id,
            image_signature=self.image_signature,
            width=self.width,
            height=self.height,
            orientation=self.orientation,
            visual_page_type=self.visual_page_type,
            visual_summary=self.visual_summary,
            key_elements=self.key_elements,
            candidates=self.candidates,
            provider_id=self.provider_id,
            model_id=self.model_id,
            package_name=self.package_name,
            activity=self.activity,
            page_state=self.page_state,
            overlay_signature=self.overlay_signature,
            surface_facts=self.surface_facts,
            contract_version=self.contract_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            **self._hash_payload(),
            "result_hash": self.result_hash,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppVisualAnalysis:
        values = _read(
            payload,
            known={
                "contract_version",
                "capture_id",
                "screenshot_artifact_id",
                "image_signature",
                "width",
                "height",
                "orientation",
                "visual_page_type",
                "visual_summary",
                "key_elements",
                "candidates",
                "provider_id",
                "model_id",
                "result_hash",
                "package_name",
                "activity",
                "page_state",
                "overlay_signature",
                "surface_facts",
            },
            required={
                "contract_version",
                "capture_id",
                "screenshot_artifact_id",
                "image_signature",
                "width",
                "height",
                "orientation",
                "visual_summary",
                "key_elements",
                "candidates",
                "provider_id",
                "model_id",
                "result_hash",
            },
            field_name="AppVisualAnalysis",
        )
        try:
            key_elements = require_sequence(values["key_elements"], "key_elements")
            raw_candidates = require_sequence(values["candidates"], "candidates")
            candidates = tuple(
                AppVisualCandidate.from_dict(require_mapping(item, "candidates[]"))
                for item in raw_candidates
            )
        except ProtocolContractError as exc:
            raise AppVisualValidationError(str(exc)) from exc
        return cls(
            capture_id=values["capture_id"],
            screenshot_artifact_id=values["screenshot_artifact_id"],
            image_signature=values["image_signature"],
            width=values["width"],
            height=values["height"],
            orientation=values["orientation"],
            visual_page_type=values.get("visual_page_type"),
            visual_summary=values["visual_summary"],
            key_elements=key_elements,
            candidates=candidates,
            provider_id=values["provider_id"],
            model_id=values["model_id"],
            result_hash=values["result_hash"],
            package_name=values.get("package_name"),
            activity=values.get("activity"),
            page_state=values.get("page_state", "unknown"),
            overlay_signature=values.get("overlay_signature"),
            surface_facts=require_mapping(values.get("surface_facts", {}), "surface_facts"),
            contract_version=values["contract_version"],
        )


def _candidate_id(
    *,
    source_capture_id: str,
    source_image_signature: str,
    semantic_name: str,
    action_type: InteractionOperation,
    normalized_bounds: NormalizedBounds | None,
    control_role: str | None,
    frontier_scope: str,
    contract_version: str,
) -> str:
    action_value = (
        action_type.value if isinstance(action_type, InteractionOperation) else action_type
    )
    payload = {
        "source_capture_id": source_capture_id,
        "source_image_signature": source_image_signature,
        "semantic_name": semantic_name,
        "action_type": action_value,
        "normalized_bounds": (
            normalized_bounds.to_dict()
            if isinstance(normalized_bounds, NormalizedBounds)
            else normalized_bounds
        ),
    }
    if contract_version != "app-visual-candidate.v1":
        payload["frontier_scope"] = frontier_scope
    if contract_version == APP_VISUAL_CANDIDATE_VERSION:
        payload["control_role"] = control_role
    digest = stable_protocol_hash(payload)
    return f"candidate:{digest}"


def _analysis_hash_payload(
    *,
    capture_id: str,
    screenshot_artifact_id: str,
    image_signature: str,
    width: int,
    height: int,
    orientation: str,
    visual_page_type: str | None,
    visual_summary: str,
    key_elements: Sequence[str],
    candidates: Sequence[AppVisualCandidate],
    provider_id: str,
    model_id: str,
    package_name: str | None,
    activity: str | None,
    page_state: str,
    overlay_signature: str | None,
    surface_facts: Mapping[str, Any],
    contract_version: str,
) -> dict[str, Any]:
    payload = {
        "capture_id": capture_id,
        "screenshot_artifact_id": screenshot_artifact_id,
        "image_signature": image_signature,
        "width": width,
        "height": height,
        "orientation": orientation,
        "visual_page_type": visual_page_type,
        "visual_summary": visual_summary,
        "key_elements": list(key_elements),
        "candidates": [item.to_dict() for item in candidates],
        "provider_id": provider_id,
        "model_id": model_id,
        "package_name": package_name,
        "activity": activity,
    }
    if contract_version != "app-visual-analysis.v1":
        payload.update(
            {
                "page_state": page_state,
                "overlay_signature": overlay_signature,
                "surface_facts": thaw_json_mapping(surface_facts),
            }
        )
    return payload


def _validate_image_signature(value: str) -> None:
    if not value.startswith("sha256:") or len(value) != 71:
        raise AppVisualValidationError("image_signature 必须为 sha256 摘要")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise AppVisualValidationError("image_signature 必须为 sha256 摘要") from exc


def _validate_dimensions(width: int, height: int) -> None:
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (width, height)
    ):
        raise AppVisualValidationError("截图 width/height 必须为正整数")


def _validate_orientation(value: str) -> None:
    if value not in _ORIENTATIONS:
        raise AppVisualValidationError("source_orientation 不受支持")


def _unique_texts(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    result = tuple(values)
    try:
        for item in result:
            require_text(item, field_name)
    except ProtocolContractError as exc:
        raise AppVisualValidationError(str(exc)) from exc
    if len(set(result)) != len(result):
        raise AppVisualValidationError(f"{field_name} 不能重复")
    return result


def _read(
    payload: Mapping[str, Any],
    *,
    known: set[str],
    required: set[str],
    field_name: str,
) -> dict[str, Any]:
    try:
        return read_known_fields(
            payload,
            known_fields=frozenset(known),
            required_fields=frozenset(required),
            field_name=field_name,
        )
    except ProtocolContractError as exc:
        raise AppVisualValidationError(str(exc)) from exc
