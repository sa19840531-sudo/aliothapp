"""App 广告 Landing 合同：occurrence 盘点 → HTTP 隔离 Playwright → 原生授权 sidecar。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

APP_AD_LANDING_CONTRACT_VERSION = "app-ad-landing-contract.v1"
LANDING_OBSERVATION_ENTITY = "LandingObservation"

_HARD_BLOCK_SCHEMES = frozenset({"market", "tel", "sms", "smsto", "mailto", "itms", "itms-apps"})


def build_ad_occurrence(
    *,
    occurrence_id: object,
    source_capture_id: object,
    bounds: Mapping[str, Any] | None = None,
    creative_text: object = None,
    link: object = None,
    intent_uri: object = None,
) -> dict[str, Any]:
    return {
        "protocol_version": APP_AD_LANDING_CONTRACT_VERSION,
        "entity": "AdOccurrence",
        "occurrence_id": str(occurrence_id or "").strip() or None,
        "source_capture_id": str(source_capture_id or "").strip() or None,
        "bounds": dict(bounds) if isinstance(bounds, Mapping) else None,
        "creative_text": str(creative_text or "").strip() or None,
        "link": str(link or "").strip() or None,
        "intent_uri": str(intent_uri or "").strip() or None,
        "layers": ("placement", "creative", "landing"),
    }


def classify_ad_landing_route(uri: object) -> dict[str, Any]:
    text = str(uri or "").strip()
    parsed = urlsplit(text)
    scheme = (parsed.scheme or "").casefold()
    if not text:
        return {
            "protocol_version": APP_AD_LANDING_CONTRACT_VERSION,
            "route": "unresolved",
            "hard_blocked": False,
            "executor": None,
        }
    if scheme in _HARD_BLOCK_SCHEMES or any(
        token in text.casefold()
        for token in ("play.google.com/store", "packageinstaller", "market://")
    ):
        return {
            "protocol_version": APP_AD_LANDING_CONTRACT_VERSION,
            "route": "hard_blocked",
            "hard_blocked": True,
            "executor": None,
            "scheme": scheme or None,
        }
    if scheme in {"http", "https"}:
        return {
            "protocol_version": APP_AD_LANDING_CONTRACT_VERSION,
            "route": "http_isolated_playwright",
            "hard_blocked": False,
            "executor": "isolated_playwright",
            "scheme": scheme,
        }
    return {
        "protocol_version": APP_AD_LANDING_CONTRACT_VERSION,
        "route": "native_authorized_sidecar",
        "hard_blocked": False,
        "executor": "authorized_sidecar",
        "scheme": scheme or "intent",
    }


def build_landing_observation(
    *,
    occurrence_id: object,
    uri: object,
    authorized: bool = False,
    artifact_ids: Sequence[str] | None = None,
    final_package: object = None,
    final_activity: object = None,
) -> dict[str, Any]:
    route = classify_ad_landing_route(uri)
    if route["hard_blocked"]:
        status = "blocked"
    elif route["route"] == "http_isolated_playwright":
        status = "observed" if artifact_ids else "pending_playwright"
    elif route["route"] == "native_authorized_sidecar":
        status = "observed" if authorized and artifact_ids else "pending_sidecar_authorization"
    else:
        status = "unresolved"
    return {
        "protocol_version": APP_AD_LANDING_CONTRACT_VERSION,
        "entity": LANDING_OBSERVATION_ENTITY,
        "occurrence_id": str(occurrence_id or "").strip() or None,
        "uri": str(uri or "").strip() or None,
        "route": route,
        "authorized": bool(authorized),
        "artifact_ids": tuple(str(x) for x in (artifact_ids or ()) if str(x).strip()),
        "final_package": str(final_package or "").strip() or None,
        "final_activity": str(final_activity or "").strip() or None,
        "status": status,
        "enters_business_coverage": False,
        "enters_business_page_tree": False,
    }


def inventory_ad_occurrences(
    raw_items: Sequence[Mapping[str, Any]] | None,
    *,
    source_capture_id: object,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items or ()):
        if not isinstance(item, Mapping):
            continue
        out.append(
            build_ad_occurrence(
                occurrence_id=item.get("occurrence_id") or f"ad-{index}",
                source_capture_id=source_capture_id,
                bounds=item.get("bounds") if isinstance(item.get("bounds"), Mapping) else None,
                creative_text=item.get("creative_text") or item.get("text"),
                link=item.get("link") or item.get("url") or item.get("href"),
                intent_uri=item.get("intent_uri") or item.get("deep_link"),
            )
        )
    return out
