"""H5 SPA / Flutter Web 逻辑页面的稳定领域契约。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

LOGICAL_SURFACE_PROTOCOL_VERSION = "logical_surface.v1"
LOGICAL_SURFACE_TRANSITION_PROTOCOL_VERSION = "logical_surface_transition.v1"
CONFIRMED_LOGICAL_RENDER_PROFILES = frozenset({"dom_document", "dom_spa", "flutter_web", "hybrid"})

_TRUSTED_PROOF = "trusted"
_LOGICAL_IDENTITY_ANCHORS = frozenset(
    {"active_navigation", "page_heading", "semantics_root"}
)
_LOGICAL_STRUCTURE_CORROBORATORS = frozenset({"main_structure", "stable_controls"})


def h5_obstruction_facts_confirm_clearance(facts: object) -> bool:
    """只有完整本帧遮挡清单可纠正模型弹层标签，不授权任何点击或规则通过。"""
    return bool(
        isinstance(facts, Mapping)
        and facts.get("schema_version") == "h5-obstruction-fact.v1"
        and facts.get("snapshot_evidence_status") == "ready"
        and facts.get("modal_census_complete") is True
        and type(facts.get("modal_census_count")) is int
        and facts.get("modal_census_count") == 0
        and facts.get("kind") in {"none", "nonblocking_notification"}
        and facts.get("blocking") is False
    )


def stable_h5_surface_hash(value: Any) -> str:
    """对归一化 JSON 生成带算法前缀的稳定摘要。"""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def h5_logical_route_hash(value: Any) -> str | None:
    """保留未知查询的业务身份，只忽略明确缓存时钟和普通页内锚点。"""
    location = _normalized_http_location(value)
    if location is None:
        return None
    url, origin = location
    parsed = urlsplit(url)
    fragment = parsed.fragment
    def query_identity(query: str) -> list[tuple[str, str]]:
        return sorted(
            (key, value) for key, value in parse_qsl(query, keep_blank_values=True)
            if key.casefold() not in {"timestamp", "_", "cache_bust"}
        )

    route_fragment = None
    if fragment.startswith(("/", "!/")):
        route = urlsplit(fragment.removeprefix("!"))
        route_fragment = (route.path, query_identity(route.query))
    return stable_h5_surface_hash((
        origin, parsed.path or "/", query_identity(parsed.query), route_fragment,
    ))


def verify_h5_bottom_navigation_transition_proof(
    *,
    proof: Mapping[str, Any] | None,
    before_url: str,
    after_url: str,
    navigation_family: str,
    navigation_index: int,
    interaction_pattern_signature: str | None = None,
    before_identity_fingerprint: str,
    after_identity_fingerprint: str,
) -> bool:
    """复核持久化的 DOM 底栏证明与原始动作 URL、结构位置和身份绑定。"""
    if not isinstance(proof, Mapping):
        return False
    allowed_keys = {
        "protocol_version",
        "status",
        "effect",
        "before_location_hash",
        "after_location_hash",
        "origin_hash",
        "navigation_family",
        "navigation_index",
        "before_identity_fingerprint",
        "after_identity_fingerprint",
    }
    if navigation_family == "adaptive_snapshot":
        allowed_keys.add("interaction_pattern_signature")
    effect = proof.get("effect")
    if effect == "activation_state_change":
        allowed_keys.update(
            {"activation_before_sha256", "activation_after_sha256"}
        )
    if set(proof) != allowed_keys or proof.get("protocol_version") != (
        "bottom_navigation_transition_proof.v1"
    ):
        return False
    if proof.get("status") != _TRUSTED_PROOF or effect not in {
        "same_origin_url_change",
        "activation_state_change",
    }:
        return False
    if navigation_family not in {
        "vant",
        "pages_foot",
        "footer_wrap",
        "adaptive_snapshot",
    }:
        return False
    if navigation_family == "adaptive_snapshot" and (
        not isinstance(interaction_pattern_signature, str)
        or re.fullmatch(r"[0-9a-f]{64}", interaction_pattern_signature) is None
        or proof.get("interaction_pattern_signature")
        != interaction_pattern_signature
    ):
        return False
    if (
        isinstance(navigation_index, bool)
        or not isinstance(navigation_index, int)
        or not 1 <= navigation_index <= 50
        or proof.get("navigation_family") != navigation_family
        or proof.get("navigation_index") != navigation_index
    ):
        return False
    before_identity = _required_fingerprint(before_identity_fingerprint)
    after_identity = _required_fingerprint(after_identity_fingerprint)
    if (
        before_identity is None
        or after_identity is None
        or before_identity == after_identity
        or proof.get("before_identity_fingerprint") != before_identity
        or proof.get("after_identity_fingerprint") != after_identity
    ):
        return False
    before_location = _normalized_http_location(before_url)
    after_location = _normalized_http_location(after_url)
    if before_location is None or after_location is None:
        return False
    normalized_before_url, before_origin = before_location
    normalized_after_url, after_origin = after_location
    if before_origin != after_origin:
        return False
    if effect == "activation_state_change":
        activation_before_hash = proof.get("activation_before_sha256")
        activation_after_hash = proof.get("activation_after_sha256")
        return bool(
            normalized_before_url == normalized_after_url
            and isinstance(activation_before_hash, str)
            and isinstance(activation_after_hash, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", activation_before_hash)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", activation_after_hash)
            and activation_before_hash != activation_after_hash
            and proof.get("before_location_hash")
            == stable_h5_surface_hash(normalized_before_url)
            and proof.get("after_location_hash")
            == stable_h5_surface_hash(normalized_after_url)
            and proof.get("origin_hash") == stable_h5_surface_hash(before_origin)
        )
    return bool(
        normalized_before_url != normalized_after_url
        and proof.get("before_location_hash")
        == stable_h5_surface_hash(normalized_before_url)
        and proof.get("after_location_hash")
        == stable_h5_surface_hash(normalized_after_url)
        and proof.get("origin_hash") == stable_h5_surface_hash(before_origin)
    )


def build_h5_logical_surface_contract(
    *,
    render_profile: Mapping[str, Any],
    document_identity_material: Sequence[Any],
    identity_signals: Mapping[str, Any],
    state_signals: Mapping[str, Any],
    evidence_sources: Sequence[str],
    evidence_quality: str,
    degraded_reasons: Sequence[str],
) -> dict[str, Any]:
    """由适配器证据构造有界、可持久化的逻辑页面快照。"""
    normalized_identity_signals = _json_mapping(identity_signals)
    normalized_state_signals = _json_mapping(state_signals)
    identity_fingerprint = stable_h5_surface_hash(normalized_identity_signals)
    return {
        "protocol_version": LOGICAL_SURFACE_PROTOCOL_VERSION,
        "render_profile": _json_mapping(render_profile),
        "document_identity": stable_h5_surface_hash(list(document_identity_material)),
        "identity_fingerprint": identity_fingerprint,
        "state_fingerprint": stable_h5_surface_hash(
            [identity_fingerprint, normalized_state_signals]
        ),
        "identity_signals": normalized_identity_signals,
        "state_signals": normalized_state_signals,
        "evidence_sources": _unique_text_list(evidence_sources),
        "evidence_quality": str(evidence_quality or "insufficient"),
        "degraded_reasons": _unique_text_list(degraded_reasons),
    }


def compare_h5_logical_surfaces(
    *,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    visual_proof: Mapping[str, Any] | None = None,
    bottom_navigation_proof: Mapping[str, Any] | None = None,
    page_tab_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """比较动作前后逻辑页面，避免把局部表面变化计成新业务页面。"""
    if not _valid_surface(before) or not _valid_surface(after):
        return _transition_payload(
            transition_kind="uncertain",
            proof="insufficient",
            before=before,
            after=after,
            corroborating_signals=(),
            reason_code="logical_surface_missing",
        )

    before_identity = _required_fingerprint(before.get("identity_fingerprint"))
    after_identity = _required_fingerprint(after.get("identity_fingerprint"))
    before_state = _required_fingerprint(before.get("state_fingerprint"))
    after_state = _required_fingerprint(after.get("state_fingerprint"))
    if not all((before_identity, after_identity, before_state, after_state)):
        return _transition_payload(
            transition_kind="uncertain",
            proof="insufficient",
            before=before,
            after=after,
            corroborating_signals=(),
            reason_code="logical_fingerprint_missing",
        )

    normalized_visual_proof = _safe_visual_proof(visual_proof)
    if visual_proof is not None and normalized_visual_proof is None:
        return _transition_payload(
            transition_kind="uncertain",
            proof="insufficient",
            before=before,
            after=after,
            corroborating_signals=(),
            reason_code="visual_transition_proof_invalid",
        )
    if normalized_visual_proof is not None:
        changed_groups = _changed_identity_signal_groups(before=before, after=after)
        if normalized_visual_proof["status"] != _TRUSTED_PROOF:
            return _transition_payload(
                transition_kind=(
                    "unchanged"
                    if before_identity == after_identity and before_state == after_state
                    else "surface_state_changed"
                    if before_identity == after_identity
                    else "uncertain"
                ),
                proof="uncertain",
                before=before,
                after=after,
                corroborating_signals=changed_groups,
                reason_code="visual_transition_untrusted",
                visual_proof=normalized_visual_proof,
            )
        if not _trusted_visual_transition_is_bound(
            after=after,
            changed_groups=changed_groups,
            visual_proof=normalized_visual_proof,
        ):
            return _transition_payload(
                transition_kind="uncertain",
                proof="insufficient",
                before=before,
                after=after,
                corroborating_signals=changed_groups,
                reason_code="visual_transition_identity_unbound",
                visual_proof=normalized_visual_proof,
            )
        return _transition_payload(
            transition_kind="logical_page_changed",
            proof=_TRUSTED_PROOF,
            before=before,
            after=after,
            corroborating_signals=changed_groups,
            visual_proof=normalized_visual_proof,
        )

    normalized_page_tab_proof = _safe_page_tab_proof(
        page_tab_proof,
        before=before,
        after=after,
    )
    if page_tab_proof is not None and normalized_page_tab_proof is None:
        return _transition_payload(
            transition_kind="uncertain",
            proof="insufficient",
            before=before,
            after=after,
            corroborating_signals=(),
            reason_code="page_tab_transition_proof_invalid",
        )
    if normalized_page_tab_proof is not None:
        changed_groups = _changed_identity_signal_groups(before=before, after=after)
        if normalized_page_tab_proof["effect"] == "route_activation_change":
            if not _trusted_page_tab_route_change_is_bound(before=before, after=after):
                return _transition_payload(
                    transition_kind="uncertain",
                    proof="insufficient",
                    before=before,
                    after=after,
                    corroborating_signals=changed_groups,
                    reason_code="page_tab_route_change_unbound",
                    page_tab_proof=normalized_page_tab_proof,
                )
            return _transition_payload(
                transition_kind="logical_page_changed",
                proof=_TRUSTED_PROOF,
                before=before,
                after=after,
                corroborating_signals=(*changed_groups, "page_tab_activation", "route_change"),
                reason_code="trusted_page_tab_route_change",
                page_tab_proof=normalized_page_tab_proof,
            )
        if before_identity == after_identity:
            if not _trusted_page_tab_state_change_is_bound(
                before=before,
                after=after,
                page_tab_proof=normalized_page_tab_proof,
            ):
                return _transition_payload(
                    transition_kind="uncertain",
                    proof="insufficient",
                    before=before,
                    after=after,
                    corroborating_signals=("surface_state",),
                    reason_code="page_tab_state_change_unbound",
                    page_tab_proof=normalized_page_tab_proof,
                )
            return _transition_payload(
                transition_kind="surface_state_changed",
                proof=_TRUSTED_PROOF,
                before=before,
                after=after,
                corroborating_signals=("surface_state", "page_tab_activation"),
                reason_code="trusted_page_tab_state_change",
                page_tab_proof=normalized_page_tab_proof,
            )
        if not _trusted_page_tab_transition_is_bound(
            after=after,
            changed_groups=changed_groups,
        ):
            return _transition_payload(
                transition_kind="uncertain",
                proof="insufficient",
                before=before,
                after=after,
                corroborating_signals=changed_groups,
                reason_code="page_tab_transition_identity_unbound",
                page_tab_proof=normalized_page_tab_proof,
            )
        return _transition_payload(
            transition_kind="logical_page_changed",
            proof=_TRUSTED_PROOF,
            before=before,
            after=after,
            corroborating_signals=changed_groups,
            reason_code="trusted_page_tab_activation_change",
            page_tab_proof=normalized_page_tab_proof,
        )

    normalized_bottom_navigation_proof = _safe_bottom_navigation_proof(
        bottom_navigation_proof,
        before=before,
        after=after,
    )
    if (
        bottom_navigation_proof is not None
        and normalized_bottom_navigation_proof is None
    ):
        return _transition_payload(
            transition_kind="uncertain",
            proof="insufficient",
            before=before,
            after=after,
            corroborating_signals=(),
            reason_code="bottom_navigation_transition_proof_invalid",
        )
    if normalized_bottom_navigation_proof is not None:
        changed_groups = _changed_identity_signal_groups(before=before, after=after)
        if not _trusted_bottom_navigation_transition_is_bound(
            after=after,
            changed_groups=changed_groups,
        ):
            return _transition_payload(
                transition_kind="uncertain",
                proof="insufficient",
                before=before,
                after=after,
                corroborating_signals=changed_groups,
                reason_code="bottom_navigation_transition_identity_unbound",
                bottom_navigation_proof=normalized_bottom_navigation_proof,
            )
        return _transition_payload(
            transition_kind="logical_page_changed",
            proof=_TRUSTED_PROOF,
            before=before,
            after=after,
            corroborating_signals=changed_groups,
            reason_code=(
                "trusted_bottom_navigation_activation_change"
                if normalized_bottom_navigation_proof["effect"]
                == "activation_state_change"
                else "trusted_bottom_navigation_url_change"
            ),
            bottom_navigation_proof=normalized_bottom_navigation_proof,
        )

    if before_identity == after_identity:
        return _transition_payload(
            transition_kind=(
                "unchanged" if before_state == after_state else "surface_state_changed"
            ),
            proof=_TRUSTED_PROOF,
            before=before,
            after=after,
            corroborating_signals=(
                () if before_state == after_state else ("surface_state",)
            ),
        )

    changed_groups = _changed_identity_signal_groups(before=before, after=after)
    profile_kind, profile_confirmed = _confirmed_profile(after)
    if not profile_confirmed:
        if before.get("document_identity") != after.get("document_identity"):
            return _transition_payload(
                transition_kind="logical_page_changed",
                proof=_TRUSTED_PROOF,
                before=before,
                after=after,
                corroborating_signals=("document_identity", *changed_groups),
            )
        return _transition_payload(
            transition_kind="uncertain",
            proof="uncertain",
            before=before,
            after=after,
            corroborating_signals=changed_groups,
            reason_code="render_profile_unconfirmed",
        )

    has_anchor = bool(_LOGICAL_IDENTITY_ANCHORS.intersection(changed_groups))
    has_structure = bool(_LOGICAL_STRUCTURE_CORROBORATORS.intersection(changed_groups))
    if _trusted_obstruction_clearance(
        before=before,
        after=after,
        changed_groups=changed_groups,
    ):
        return _transition_payload(
            transition_kind="surface_state_changed",
            proof=_TRUSTED_PROOF,
            before=before,
            after=after,
            corroborating_signals=(*changed_groups, "obstruction_clearance"),
            reason_code="trusted_obstruction_clearance",
        )
    if has_anchor and has_structure:
        return _transition_payload(
            transition_kind="logical_page_changed",
            proof=_TRUSTED_PROOF,
            before=before,
            after=after,
            corroborating_signals=changed_groups,
        )
    return _transition_payload(
        transition_kind="uncertain",
        proof="uncertain",
        before=before,
        after=after,
        corroborating_signals=changed_groups,
        reason_code=f"{profile_kind}_identity_change_uncorroborated",
    )


def _trusted_obstruction_clearance(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    changed_groups: Sequence[str],
) -> bool:
    """把遮挡退场伴随的非锚点布局抖动限定为可信同页状态变化。"""
    before_state = before.get("state_signals")
    after_state = after.get("state_signals")
    allowed_identity_drift = {*_LOGICAL_STRUCTURE_CORROBORATORS, "cardinality"}
    return bool(
        before.get("document_identity") == after.get("document_identity")
        and changed_groups
        and set(changed_groups).issubset(allowed_identity_drift)
        and isinstance(before_state, Mapping)
        and isinstance(after_state, Mapping)
        and before_state.get("obstruction_count") in {"1", "2-5", "6-20", "20+"}
        and after_state.get("obstruction_count") == "0"
        and after_state.get("loading") is False
        and after.get("evidence_quality") == "high"
    )


def bind_h5_page_type(
    *,
    logical_surface: Mapping[str, Any] | None,
    page_type: str | None,
    detection_status: str,
    page_type_confidence: float,
    confidence_source: str,
) -> dict[str, Any] | None:
    """把页型绑定到逻辑身份，同时保持四类信任域互不串升。"""
    if not _valid_surface(logical_surface):
        return None
    surface = deepcopy(dict(logical_surface))
    profile = surface.get("render_profile")
    render_profile_confidence = (
        _bounded_confidence(profile.get("confidence"))
        if isinstance(profile, Mapping)
        else 0.0
    )
    normalized_page_confidence = _bounded_confidence(page_type_confidence)
    identity_fingerprint = _required_fingerprint(surface.get("identity_fingerprint"))
    profile_kind, profile_confirmed = _confirmed_profile(surface)
    proof = str(surface.get("logical_transition_proof") or "insufficient")

    reason_code: str | None = None
    if str(detection_status or "") != "recognized" or not str(page_type or "").strip():
        reason_code = "page_type_unrecognized"
    elif normalized_page_confidence < 0.7:
        reason_code = "page_type_low_confidence"
    elif not identity_fingerprint:
        reason_code = "logical_identity_unbound"
    elif profile_confirmed and proof != _TRUSTED_PROOF:
        reason_code = "logical_transition_untrusted"

    allowed = reason_code is None
    surface["confidence_domains"] = {
        "page_type_confidence": normalized_page_confidence,
        "render_profile_confidence": render_profile_confidence,
    }
    surface["page_type_binding"] = {
        "page_type": str(page_type).strip() if page_type is not None else None,
        "identity_fingerprint": identity_fingerprint,
        "page_type_confidence": normalized_page_confidence,
        "confidence_source": str(confidence_source or "unavailable"),
        "render_profile": profile_kind,
        "coverage_allowed": allowed,
        "rule_matching_allowed": allowed,
        "reason_code": reason_code,
    }
    return surface


def logical_surface_is_effective(surface: Mapping[str, Any] | None) -> bool:
    """仅确认的 SPA、Flutter Web 与 Hybrid 可影响探索身份。"""
    _kind, confirmed = _confirmed_profile(surface)
    return confirmed


def _transition_payload(
    *,
    transition_kind: str,
    proof: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    corroborating_signals: Sequence[str],
    reason_code: str | None = None,
    visual_proof: Mapping[str, Any] | None = None,
    bottom_navigation_proof: Mapping[str, Any] | None = None,
    page_tab_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "protocol_version": LOGICAL_SURFACE_TRANSITION_PROTOCOL_VERSION,
        "transition_kind": transition_kind,
        "logical_transition_proof": proof,
        "before_document_identity": _mapping_text(before, "document_identity"),
        "after_document_identity": _mapping_text(after, "document_identity"),
        "before_identity_fingerprint": _mapping_text(before, "identity_fingerprint"),
        "after_identity_fingerprint": _mapping_text(after, "identity_fingerprint"),
        "before_state_fingerprint": _mapping_text(before, "state_fingerprint"),
        "after_state_fingerprint": _mapping_text(after, "state_fingerprint"),
        "corroborating_signals": _unique_text_list(corroborating_signals),
    }
    if reason_code is not None:
        payload["reason_code"] = reason_code
    if visual_proof is not None:
        payload["visual_proof"] = deepcopy(dict(visual_proof))
    if bottom_navigation_proof is not None:
        payload["bottom_navigation_proof"] = deepcopy(
            dict(bottom_navigation_proof)
        )
    if page_tab_proof is not None:
        payload["page_tab_proof"] = deepcopy(dict(page_tab_proof))
    return payload


def _safe_page_tab_proof(
    value: Mapping[str, Any] | None,
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any] | None:
    """验签精确页内 Tab 的激活变化，并只持久化目标摘要。"""
    required_keys = {
        "protocol_version",
        "status",
        "effect",
        "before_location",
        "after_location",
        "target_selector_sha256",
        "target_label_sha256",
        "activation_before_sha256",
        "activation_after_sha256",
        "before_identity_fingerprint",
        "after_identity_fingerprint",
    }
    content_keys = {
        "content_before_sha256",
        "content_after_sha256",
        "content_change_count",
    }
    if not isinstance(value, Mapping) or frozenset(value) not in {
        frozenset(required_keys),
        frozenset(required_keys | content_keys),
    }:
        return None
    if (
        value.get("protocol_version") != "page_tab_transition_proof.v1"
        or value.get("status") != _TRUSTED_PROOF
        or value.get("effect") not in {"activation_state_change", "route_activation_change"}
    ):
        return None
    hashes = (
        value.get("target_selector_sha256"),
        value.get("target_label_sha256"),
        value.get("activation_before_sha256"),
        value.get("activation_after_sha256"),
    )
    if any(
        not isinstance(item, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None
        for item in hashes
    ) or hashes[2] == hashes[3]:
        return None
    before_identity = _required_fingerprint(
        value.get("before_identity_fingerprint")
    )
    after_identity = _required_fingerprint(value.get("after_identity_fingerprint"))
    if (
        before_identity != _required_fingerprint(before.get("identity_fingerprint"))
        or after_identity != _required_fingerprint(after.get("identity_fingerprint"))
    ):
        return None
    before_location = _normalized_http_location(value.get("before_location"))
    after_location = _normalized_http_location(value.get("after_location"))
    if before_location is None or after_location is None:
        return None
    before_url, before_origin = before_location
    after_url, after_origin = after_location
    if before_origin != after_origin or (
        (before_url == after_url) != (value["effect"] == "activation_state_change")
    ):
        return None
    sanitized = {
        "protocol_version": "page_tab_transition_proof.v1",
        "status": _TRUSTED_PROOF,
        "effect": value["effect"],
        "before_location_hash": stable_h5_surface_hash(before_url),
        "after_location_hash": stable_h5_surface_hash(after_url),
        "origin_hash": stable_h5_surface_hash(before_origin),
        "target_selector_sha256": hashes[0],
        "target_label_sha256": hashes[1],
        "activation_before_sha256": hashes[2],
        "activation_after_sha256": hashes[3],
        "before_identity_fingerprint": before_identity,
        "after_identity_fingerprint": after_identity,
    }
    if content_keys <= set(value):
        content_hashes = (
            value.get("content_before_sha256"),
            value.get("content_after_sha256"),
        )
        content_change_count = value.get("content_change_count")
        if (
            any(
                not isinstance(item, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None
                for item in content_hashes
            )
            or content_hashes[0] == content_hashes[1]
            or not isinstance(content_change_count, int)
            or isinstance(content_change_count, bool)
            or content_change_count < 2
        ):
            return None
        sanitized.update(
            {
                "content_before_sha256": content_hashes[0],
                "content_after_sha256": content_hashes[1],
                "content_change_count": content_change_count,
            }
        )
    return sanitized


def _trusted_page_tab_transition_is_bound(
    *,
    after: Mapping[str, Any],
    changed_groups: Sequence[str],
) -> bool:
    """页签激活只能证明已确认 DOM 页面中的主业务结构变化。"""
    profile_kind, profile_confirmed = _confirmed_profile(after)
    return bool(
        profile_confirmed
        and profile_kind in {"dom_document", "dom_spa", "hybrid"}
        and after.get("evidence_quality") == "high"
        and "main_structure" in changed_groups
    )


def _trusted_page_tab_route_change_is_bound(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """同源路由切换仍须由同一文档和高质量的激活后快照共同证明。"""
    profile_kind, profile_confirmed = _confirmed_profile(after)
    before_document = _required_fingerprint(before.get("document_identity"))
    return bool(
        profile_confirmed
        and profile_kind in {"dom_document", "dom_spa", "hybrid"}
        and before_document
        and before_document == _required_fingerprint(after.get("document_identity"))
        and after.get("evidence_quality") == "high"
    )


def _trusted_page_tab_state_change_is_bound(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    page_tab_proof: Mapping[str, Any],
) -> bool:
    """同页筛选只接受高质量 DOM 证据中的确定性激活与状态变化。"""
    profile_kind, profile_confirmed = _confirmed_profile(after)
    before_state = _required_fingerprint(before.get("state_fingerprint"))
    after_state = _required_fingerprint(after.get("state_fingerprint"))
    return bool(
        profile_confirmed
        and profile_kind in {"dom_document", "dom_spa", "hybrid"}
        and after.get("evidence_quality") == "high"
        and before.get("document_identity") == after.get("document_identity")
        and before_state
        and after_state
        and (
            before_state != after_state
            or (
                page_tab_proof.get("content_before_sha256")
                != page_tab_proof.get("content_after_sha256")
                and isinstance(page_tab_proof.get("content_change_count"), int)
                and page_tab_proof.get("content_change_count", 0) >= 2
            )
        )
    )


def _safe_bottom_navigation_proof(
    value: Mapping[str, Any] | None,
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any] | None:
    """验签精确 DOM 底栏效果，并只持久化不可逆摘要。"""
    if not isinstance(value, Mapping) or value.get("protocol_version") != (
        "bottom_navigation_transition_proof.v1"
    ):
        return None
    allowed_keys = {
        "protocol_version",
        "status",
        "effect",
        "before_location",
        "after_location",
        "navigation_family",
        "navigation_index",
        "before_identity_fingerprint",
        "after_identity_fingerprint",
    }
    navigation_family = str(value.get("navigation_family") or "")
    if navigation_family == "adaptive_snapshot":
        allowed_keys.add("interaction_pattern_signature")
    effect = value.get("effect")
    if effect == "activation_state_change":
        allowed_keys.update(
            {"activation_before_sha256", "activation_after_sha256"}
        )
    if set(value) != allowed_keys:
        return None
    if value.get("status") != _TRUSTED_PROOF or effect not in {
        "same_origin_url_change",
        "activation_state_change",
    }:
        return None
    if navigation_family not in {
        "vant",
        "pages_foot",
        "footer_wrap",
        "adaptive_snapshot",
    }:
        return None
    pattern_signature = value.get("interaction_pattern_signature")
    if navigation_family == "adaptive_snapshot" and (
        not isinstance(pattern_signature, str)
        or re.fullmatch(r"[0-9a-f]{64}", pattern_signature) is None
    ):
        return None
    navigation_index = value.get("navigation_index")
    if (
        isinstance(navigation_index, bool)
        or not isinstance(navigation_index, int)
        or not 1 <= navigation_index <= 50
    ):
        return None
    before_identity = _required_fingerprint(
        value.get("before_identity_fingerprint")
    )
    after_identity = _required_fingerprint(value.get("after_identity_fingerprint"))
    if (
        before_identity != _required_fingerprint(before.get("identity_fingerprint"))
        or after_identity != _required_fingerprint(after.get("identity_fingerprint"))
        or before_identity == after_identity
    ):
        return None
    before_location = _normalized_http_location(value.get("before_location"))
    after_location = _normalized_http_location(value.get("after_location"))
    if before_location is None or after_location is None:
        return None
    before_url, before_origin = before_location
    after_url, after_origin = after_location
    if before_origin != after_origin:
        return None
    activation_before_hash = value.get("activation_before_sha256")
    activation_after_hash = value.get("activation_after_sha256")
    if effect == "activation_state_change":
        if (
            before_url != after_url
            or not isinstance(activation_before_hash, str)
            or not isinstance(activation_after_hash, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", activation_before_hash) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", activation_after_hash) is None
            or activation_before_hash == activation_after_hash
        ):
            return None
    elif before_url == after_url:
        return None
    before_route = (before.get("identity_signals") or {}).get("route")
    after_route = (after.get("identity_signals") or {}).get("route")
    if before_route != after_route and (
        before_route != h5_logical_route_hash(before_url)
        or after_route != h5_logical_route_hash(after_url)
    ):
        return None
    return {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": _TRUSTED_PROOF,
        "effect": effect,
        "before_location_hash": stable_h5_surface_hash(before_url),
        "after_location_hash": stable_h5_surface_hash(after_url),
        "origin_hash": stable_h5_surface_hash(before_origin),
        "navigation_family": navigation_family,
        "navigation_index": navigation_index,
        **(
            {"interaction_pattern_signature": pattern_signature}
            if navigation_family == "adaptive_snapshot"
            else {}
        ),
        **(
            {
                "activation_before_sha256": activation_before_hash,
                "activation_after_sha256": activation_after_hash,
            }
            if effect == "activation_state_change"
            else {}
        ),
        "before_identity_fingerprint": before_identity,
        "after_identity_fingerprint": after_identity,
    }


def _trusted_bottom_navigation_transition_is_bound(
    *,
    after: Mapping[str, Any],
    changed_groups: Sequence[str],
) -> bool:
    """绑定已确认的业务身份变化，布局不变时须同时有导航激活项和页标题变化。"""
    profile_kind, profile_confirmed = _confirmed_profile(after)
    return bool(
        profile_confirmed
        and profile_kind in {"dom_document", "dom_spa", "hybrid"}
        and after.get("evidence_quality") == "high"
        and (
            "main_structure" in changed_groups
            or "route" in changed_groups
            or {"active_navigation", "page_heading"}.issubset(changed_groups)
        )
    )


def _normalized_http_location(value: Any) -> tuple[str, str] | None:
    """规范化 HTTP(S) 地址；URL 只参与校验与摘要，不进入持久化证明。"""
    text = str(value or "").strip()
    if not text or len(text) > 4096:
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    host = str(parsed.hostname or "").casefold()
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    host_for_netloc = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    netloc = (
        host_for_netloc
        if effective_port == default_port
        else f"{host_for_netloc}:{effective_port}"
    )
    normalized_url = urlunsplit(
        (
            scheme,
            netloc,
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
    )
    origin = f"{scheme}://{host_for_netloc}:{effective_port}"
    return normalized_url, origin


def _safe_visual_proof(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """只接收内部固定量化摘要，拒绝图片、坐标、OCR 与签名类扩展。"""
    if not isinstance(value, Mapping) or value.get("protocol_version") != (
        "visual_navigation_transition_proof.v1"
    ):
        return None
    allowed_keys = {
        "protocol_version",
        "status",
        "after_frame_count",
        "after_frames_stable",
        "main_structure_changed",
        "main_change_bucket",
        "destination_structure",
        "after_identity_bound",
        "reason_codes",
    }
    if not set(value).issubset(allowed_keys):
        return None
    status = str(value.get("status") or "")
    if status not in {"trusted", "insufficient", "no_effect", "stale"}:
        return None
    if value.get("after_frame_count") != 2:
        return None
    for key in (
        "after_frames_stable",
        "main_structure_changed",
        "after_identity_bound",
    ):
        if not isinstance(value.get(key), bool):
            return None
    destination = str(value.get("destination_structure") or "unknown")
    if destination not in {
        "unknown",
        "sparse",
        "grid",
        "list",
        "detail",
        "profile_menu",
        "settings_form",
        "payment_panel",
        "activity_dashboard",
        "dashboard",
    }:
        return None
    reasons = value.get("reason_codes")
    if not isinstance(reasons, list) or len(reasons) > 8 or not all(
        isinstance(item, str) and 0 < len(item) <= 80 for item in reasons
    ):
        return None
    result: dict[str, Any] = {
        "protocol_version": "visual_navigation_transition_proof.v1",
        "status": status,
        "after_frame_count": 2,
        "after_frames_stable": value["after_frames_stable"],
        "main_structure_changed": value["main_structure_changed"],
        "after_identity_bound": value["after_identity_bound"],
        "reason_codes": list(dict.fromkeys(reasons)),
    }
    if "destination_structure" in value:
        result["destination_structure"] = destination
    change_bucket = value.get("main_change_bucket")
    if change_bucket in {"none_or_local", "substantial"}:
        result["main_change_bucket"] = change_bucket
    return result


def _trusted_visual_transition_is_bound(
    *,
    after: Mapping[str, Any],
    changed_groups: Sequence[str],
    visual_proof: Mapping[str, Any],
) -> bool:
    profile_kind, profile_confirmed = _confirmed_profile(after)
    coverage = after.get("semantics_coverage")
    return bool(
        profile_confirmed
        and profile_kind in {"flutter_web", "hybrid"}
        and isinstance(coverage, Mapping)
        and coverage.get("level") == "semantics_sparse"
        and "visual_layout" in changed_groups
        and visual_proof.get("after_frames_stable") is True
        and visual_proof.get("main_structure_changed") is True
        and visual_proof.get("after_identity_bound") is True
    )


def _changed_identity_signal_groups(
    *, before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[str, ...]:
    before_signals = before.get("identity_signals")
    after_signals = after.get("identity_signals")
    left = before_signals if isinstance(before_signals, Mapping) else {}
    right = after_signals if isinstance(after_signals, Mapping) else {}
    return tuple(
        key
        for key in sorted(set(left) | set(right))
        if left.get(key) != right.get(key)
        and (
            key != "main_structure"
            or not any(
                isinstance(row, Mapping) and row.get("basis") == "native_main_shell"
                for rows in (left.get(key), right.get(key))
                if isinstance(rows, list) for row in rows
            )
            # 新旧或语义/原生投影的差异不能冒充真实主体结构变化。
            or (
                isinstance(before.get("native_main_structure"), list)
                and bool(before["native_main_structure"])
                and isinstance(after.get("native_main_structure"), list)
                and bool(after["native_main_structure"])
                and before["native_main_structure"] != after["native_main_structure"]
            )
        )
    )


def _confirmed_profile(surface: Mapping[str, Any] | None) -> tuple[str, bool]:
    if not isinstance(surface, Mapping):
        return "unknown", False
    profile = surface.get("render_profile")
    if not isinstance(profile, Mapping):
        return "unknown", False
    kind = str(profile.get("kind") or "unknown")
    return kind, bool(
        kind in CONFIRMED_LOGICAL_RENDER_PROFILES
        and profile.get("status") == "confirmed"
    )


def _valid_surface(value: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("protocol_version") == LOGICAL_SURFACE_PROTOCOL_VERSION
    )


def _bounded_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return round(min(max(float(value), 0.0), 1.0), 4)


def _required_fingerprint(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized if normalized.startswith("sha256:") and len(normalized) == 71 else None


def _mapping_text(value: Mapping[str, Any] | None, key: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    normalized = str(value.get(key) or "").strip()
    return normalized or None


def _unique_text_list(values: Sequence[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = json.loads(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    if not isinstance(normalized, dict):
        raise TypeError("逻辑页面信号必须为 JSON 对象")
    return normalized
