from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest

MODULE_NAME = "services.api.app.domain.page.web_site_menu_registry"
EXPECTED_SITE_COUNTS = {
    "heiliao_com": 30,
    "hl365_com": 43,
    "www_91jav1_com": 8,
    "91porna_com": 65,
    "huangguoai_com": 9,
    "avhub360_com": 11,
}


def _load_registry_module():
    spec = importlib.util.find_spec(MODULE_NAME)
    assert spec is not None, "Web 站点菜单 Registry 尚未实现"
    return importlib.import_module(MODULE_NAME)


def test_catalog_keeps_all_audited_active_anchors() -> None:
    module = _load_registry_module()
    registry = module.WebSiteMenuRegistry()

    counts = {site.site_id: len(site.entries) for site in registry.sites}
    decisions = [entry.decision for site in registry.sites for entry in site.entries]

    assert counts == EXPECTED_SITE_COUNTS
    assert sum(counts.values()) == 166
    assert decisions.count(module.WebMenuDecision.HOME_BASELINE) == 6
    assert decisions.count(module.WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY) == 157
    assert decisions.count(module.WebMenuDecision.BLOCKED_CROSS_ORIGIN) == 3


def test_registry_exposes_verified_source_and_unique_summary() -> None:
    module = _load_registry_module()
    registry = module.WebSiteMenuRegistry()

    assert registry.source_attachment_sha256 == (
        "2B9DB035750126C905C1C87829BE47C5C4C34389618FA5091C6C0CF29E82DDF0"
    )
    assert registry.summary["unique_same_origin_target_count"] == 162
    assert registry.summary["ignored_commented_anchor_count"] == 4


def test_registry_rejects_weakened_host_wildcard_policy(tmp_path: Path) -> None:
    module = _load_registry_module()
    catalog_path = Path(module.__file__).with_name("web_site_menu_catalog.json")
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    payload["policy"]["host_wildcards_allowed"] = True
    invalid_path = tmp_path / "web_site_menu_catalog.json"
    invalid_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(module.WebSiteMenuCatalogError, match="policy"):
        module.WebSiteMenuRegistry(invalid_path)


def test_registry_only_returns_target_site_catalog() -> None:
    module = _load_registry_module()
    registry = module.WebSiteMenuRegistry()

    site = registry.resolve_site("https://heiliao.com/hlcg/?from=entry#latest")

    assert site.site_id == "heiliao_com"
    assert site.origin == "https://heiliao.com"
    assert len(site.entries) == 30


def test_unknown_site_and_https_downgrade_fail_closed() -> None:
    module = _load_registry_module()
    registry = module.WebSiteMenuRegistry()

    with pytest.raises(module.WebSiteMenuCatalogNotFoundError):
        registry.resolve_site("https://unknown.example/")
    with pytest.raises(module.WebSiteMenuCatalogNotFoundError):
        registry.resolve_site("http://heiliao.com/")


def test_unknown_https_origin_uses_runtime_home_without_catalog_gate() -> None:
    module = _load_registry_module()

    context = module.WebSiteMenuRegistry().resolve_site_context("https://both.pnsmxatxp.cc/")

    assert context.source_kind == "runtime"
    assert context.site.origin == "https://both.pnsmxatxp.cc"
    assert context.site.entries[0].decision is module.WebMenuDecision.HOME_BASELINE


def test_http_catalog_accepts_same_host_https_upgrade() -> None:
    module = _load_registry_module()

    site = module.WebSiteMenuRegistry().resolve_site("https://91porna.com/")

    assert site.site_id == "91porna_com"
    assert site.origin == "http://91porna.com"


def test_duplicate_url_menu_paths_keep_distinct_entry_ids() -> None:
    module = _load_registry_module()
    site = module.WebSiteMenuRegistry().resolve_site("http://91porna.com/")

    moviesets = [entry for entry in site.entries if entry.canonical_url.endswith("/moviesets")]

    assert len(moviesets) == 2
    assert len({entry.entry_id for entry in moviesets}) == 2
    assert len({entry.menu_path for entry in moviesets}) == 2


def test_catalog_v3_exposes_non_authoritative_classification_hints() -> None:
    module = _load_registry_module()
    registry = module.WebSiteMenuRegistry()
    entries = {
        entry.entry_id: entry
        for site in registry.sites
        for entry in site.entries
    }

    assert registry.schema_version == "web-menu-allowlist.v4"
    assert entries["heiliao.hlcg"].classification_hint.expected_page_type == (
        "content_list"
    )
    assert entries["heiliao.ysdj"].classification_hint.expected_page_type == (
        "video_list"
    )
    assert entries["91porna.video.gossip"].classification_hint.expected_page_type == (
        "video_list"
    )
    assert entries["91porna.gossip"].classification_hint.expected_page_type == (
        "content_list"
    )
    assert entries["huangguo.home"].classification_hint.mixed_content is True
    assert entries["91jav.official_app"].collection_eligibility.eligible is False
    assert entries["91jav.official_app"].decision is module.WebMenuDecision.BLOCKED_CROSS_ORIGIN
    assert all(
        entry.classification_hint is None
        or entry.classification_hint.authority == "advisory"
        for entry in entries.values()
    )


@pytest.mark.parametrize(
    "page_type",
    [
        "home",
        "content_list",
        "content_detail",
        "video_list",
        "video_detail",
        "activity",
        "search_page",
    ],
)
def test_advisory_classification_hint_accepts_seven_business_types(page_type: str) -> None:
    module = _load_registry_module()

    hint = module._classification_hint(
        {
            "classification_hint": {
                "expected_page_type": page_type,
                "confidence": 0.7,
                "source": "fixture",
            }
        },
        entry_id="fixture.entry",
    )

    assert hint.expected_page_type == page_type
    assert hint.authority == "advisory"


@pytest.mark.parametrize("page_type", ["age_gate", "unknown"])
def test_advisory_classification_hint_rejects_nonbusiness_types(page_type: str) -> None:
    module = _load_registry_module()

    with pytest.raises(module.WebSiteMenuCatalogError, match="七类业务闭集"):
        module._classification_hint(
            {
                "classification_hint": {
                    "expected_page_type": page_type,
                    "confidence": 0.7,
                    "source": "fixture",
                }
            },
            entry_id="fixture.entry",
        )


def test_allowlist_url_match_is_advisory_and_accepts_http_to_https_upgrade() -> None:
    module = _load_registry_module()
    site = module.WebSiteMenuRegistry().resolve_site("http://91porna.com/")
    entry = next(
        item
        for item in site.entries
        if item.decision is module.WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    https_url = entry.canonical_url.replace("http://", "https://", 1)

    matches = site.advisory_entries_for_url(https_url)

    assert entry in matches
    assert entry.classification_hint is not None


def test_catalog_exposes_versioned_lifecycle_and_deprecates_confirmed_old_paths() -> None:
    module = _load_registry_module()
    registry = module.WebSiteMenuRegistry()
    entries = {
        entry.entry_id: entry
        for site in registry.sites
        for entry in site.entries
    }

    assert all(site.catalog_version == registry.version for site in registry.sites)
    assert all(site.status is module.WebCatalogStatus.ACTIVE for site in registry.sites)
    assert all(site.verification["evidence_ref"] for site in registry.sites)
    assert all(entry.catalog_version == registry.version for entry in entries.values())
    assert all(entry.verification["evidence_ref"] for entry in entries.values())
    deprecated_ids = {
        entry_id
        for entry_id, entry in entries.items()
        if entry.status is module.WebCatalogStatus.DEPRECATED
    }
    assert deprecated_ids == {
        "hl365.sjb_hl",
        "hl365.sjb_ttt",
        "hl365.sjb_rs",
        "hl365.sjb_bc",
    }
    assert all(not entries[entry_id].collection_eligibility.eligible for entry_id in deprecated_ids)
    assert registry.summary["active_entry_count"] == 6
    assert registry.summary["observe_only_entry_count"] == 156
    assert registry.summary["deprecated_entry_count"] == 4

    catalog_path = Path(module.__file__).with_name("web_site_menu_catalog.json")
    raw_payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    raw_entries = {
        entry["entry_id"]: entry
        for site in raw_payload["sites"]
        for entry in site["entries"]
    }
    for entry_id in deprecated_ids:
        assert raw_entries[entry_id]["collection_eligibility"] == {
            "eligible": False,
            "reason": "catalog_entry_deprecated",
        }


def test_registry_rejects_unknown_entry_lifecycle_override(tmp_path: Path) -> None:
    module = _load_registry_module()
    catalog_path = Path(module.__file__).with_name("web_site_menu_catalog.json")
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    payload["sites"][0]["entry_overrides"]["missing.entry"] = {
        "status": "deprecated",
        "verification": {
            "verified_at": "2026-09-04",
            "evidence_ref": "docs/reports/missing.md",
            "result": "runtime_absent_confirmed",
        },
    }
    invalid_path = tmp_path / "web_site_menu_catalog.json"
    invalid_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(module.WebSiteMenuCatalogError, match="entry_overrides"):
        module.WebSiteMenuRegistry(invalid_path)


def test_production_and_docs_catalogs_are_semantically_identical() -> None:
    module = _load_registry_module()
    production = Path(module.__file__).with_name("web_site_menu_catalog.json")
    audit_copy = Path(__file__).parents[3] / "docs/data/web_v2_six_site_menu_allowlist.json"

    assert json.loads(production.read_text(encoding="utf-8")) == json.loads(
        audit_copy.read_text(encoding="utf-8")
    )


def test_cross_origin_entries_are_terminally_blocked() -> None:
    module = _load_registry_module()
    registry = module.WebSiteMenuRegistry()

    cross_origin_entries = [
        entry
        for site in registry.sites
        for entry in site.entries
        if entry.decision is module.WebMenuDecision.BLOCKED_CROSS_ORIGIN
    ]

    assert len(cross_origin_entries) == 3
    assert all(entry.is_same_origin is False for entry in cross_origin_entries)
    assert all(
        entry.canonical_url.startswith(("http://", "https://")) for entry in cross_origin_entries
    )


def test_unknown_query_is_identity_and_fragment_is_removed() -> None:
    module = _load_registry_module()

    first = module.canonical_menu_url(
        "https://example.test/path?object=one#section",
        site_origin="https://example.test",
    )
    second = module.canonical_menu_url(
        "https://example.test/path?object=two#section",
        site_origin="https://example.test",
    )

    assert first == "https://example.test/path?object=one"
    assert second == "https://example.test/path?object=two"
    assert first != second


def test_catalog_content_sha_changes_with_normalized_menu_content(tmp_path: Path) -> None:
    module = _load_registry_module()
    catalog_path = Path(module.__file__).with_name("web_site_menu_catalog.json")
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    original = module.WebSiteMenuRegistry(catalog_path)
    payload["sites"][0]["entries"][0]["menu_path"][0] += "变更"
    changed_path = tmp_path / "web_site_menu_catalog.json"
    changed_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    changed = module.WebSiteMenuRegistry(changed_path)

    assert len(original.catalog_content_sha256) == 64
    assert original.catalog_content_sha256 == original.catalog_content_sha256.casefold()
    assert original.catalog_content_sha256 != changed.catalog_content_sha256
    assert original.catalog_content_sha256 != original.source_attachment_sha256.casefold()
