"""Web V2 页面类型适用性冻结合同。"""

import pytest

from services.api.app.application.web_page_applicability import (
    finalize_web_page_applicability,
    infer_runtime_entry_page_type,
    resolve_web_page_applicability,
    revise_web_page_applicability,
)
from services.api.app.application.web_product_capability_assessment import (
    WebProductCapability,
)


def test_known_site_does_not_read_site_catalog_for_new_run() -> None:
    snapshot = resolve_web_page_applicability(
        target_url="https://avhub360.com/",
        requested_mode=None,
        requested_required_page_types=(),
    )

    assert snapshot.applicability_mode == "runtime_dynamic"
    assert snapshot.required_page_types == ("home",)
    assert snapshot.optional_page_types == (
        "activity",
        "collection_page",
        "content_detail",
        "content_list",
        "search_page",
        "video_detail",
        "video_list",
    )
    assert snapshot.not_applicable_page_types == ()
    assert snapshot.page_type_statuses["home"] == "required"
    assert snapshot.page_type_statuses["video_list"] == "not_observed"
    assert snapshot.catalog_version
    assert len(snapshot.catalog_hash) == 64


def test_unknown_site_also_requires_only_home() -> None:
    snapshot = resolve_web_page_applicability(
        target_url="https://unknown.example/",
        requested_mode=None,
        requested_required_page_types=(),
    )

    assert snapshot.applicability_mode == "runtime_dynamic"
    assert snapshot.required_page_types == ("home",)
    assert "content_list" in snapshot.optional_page_types
    assert snapshot.not_applicable_page_types == ()
    assert snapshot.resolution_reason == "runtime_evidence_initial"
    assert snapshot.catalog_version
    assert len(snapshot.catalog_hash) == 64


@pytest.mark.parametrize("applicable,covered,required,status,missing", [
    ((), ("home",), ("home",), "not_observed", ()),
    (("collection_page",), ("home",), ("collection_page", "home"),
     "applicable_uncovered", ("collection_page",)),
    (("collection_page",), ("home", "collection_page"), ("collection_page", "home"), "covered", ()),
])
def test_collection_catalog_entry_is_not_automatic_coverage(
    applicable, covered, required, status, missing,
):
    """目录新增可选页型与真正发现缺口必须分开，不能靠删目录完成收口。"""
    from services.api.app.domain.exploration.web_coverage_snapshot import WebCoverageSnapshot

    initial = resolve_web_page_applicability(
        target_url="https://unknown.example/", requested_mode=None,
        requested_required_page_types=(),
    )
    assert initial.required_page_types == ("home",)
    assert "collection_page" in initial.optional_page_types
    assert initial.page_type_statuses["collection_page"] == "not_observed"
    final = finalize_web_page_applicability(
        initial, applicable_page_types=applicable, covered_page_types=covered,
    )
    assert final.required_page_types == required
    assert final.page_type_statuses["collection_page"] == status
    frozen = WebCoverageSnapshot.create(
        task_id="test-task", agent_run_id="test-run", finalization_epoch=0,
        required_page_types=final.required_page_types,
        optional_page_types=final.optional_page_types,
        observed_page_types=covered, unrecognized_page_types=(),
        catalog_version=final.catalog_version, catalog_hash=final.catalog_hash,
    )
    assert frozen.missing_page_types == missing
    assert frozen.coverage_status == ("completed_with_gaps" if missing else "complete")


def test_legacy_mode_names_are_accepted_only_as_dynamic_aliases() -> None:
    snapshot = resolve_web_page_applicability(
        target_url="https://avhub360.com/",
        requested_mode="strict_four",
        requested_required_page_types=("video_list",),
    )

    assert snapshot.applicability_mode == "runtime_dynamic"
    # 选了页面范围时只按所选算，首页没选就不强制必检
    assert snapshot.required_page_types == ("video_list",)
    assert snapshot.resolution_reason == "legacy_request_mode_dynamic_alias"


def test_explicit_runtime_targets_start_as_applicable_uncovered() -> None:
    snapshot = resolve_web_page_applicability(
        target_url="https://avhub360.com/",
        requested_mode="strict_four",
        requested_required_page_types=(
            "content_list",
            "content_detail",
            "video_list",
            "video_detail",
        ),
    )

    assert snapshot.required_page_types == (
        "content_detail",
        "content_list",
        "video_detail",
        "video_list",
    )
    assert snapshot.page_type_statuses["content_detail"] == "applicable_uncovered"
    assert snapshot.page_type_statuses["home"] == "not_observed"


def test_runtime_evidence_promotes_applicable_types_into_denominator() -> None:
    snapshot = resolve_web_page_applicability(
        target_url="https://avhub360.com/",
        requested_mode=None,
        requested_required_page_types=("video_list",),
    )

    finalized = finalize_web_page_applicability(
        snapshot,
        applicable_page_types=("content_list", "content_detail", "age_gate", "ad_page"),
        covered_page_types=("home", "content_list", "unknown"),
    )

    assert finalized.required_page_types == ("content_detail", "content_list", "home", "video_list")
    assert finalized.page_type_statuses["home"] == "covered"
    assert finalized.page_type_statuses["content_list"] == "covered"
    assert finalized.page_type_statuses["content_detail"] == "applicable_uncovered"
    assert "age_gate" not in finalized.page_type_statuses
    assert "ad_page" not in finalized.page_type_statuses


def test_inspection_scope_keeps_passed_through_pages_out_of_denominator() -> None:
    """只选了视频详情时，经过的首页和视频列表既不进分母，也不算已覆盖。"""
    snapshot = resolve_web_page_applicability(
        target_url="https://avhub360.com/",
        requested_mode=None,
        requested_required_page_types=("video_detail",),
    )

    finalized = finalize_web_page_applicability(
        snapshot,
        applicable_page_types=("video_list", "content_list"),
        covered_page_types=("home", "video_list", "video_detail"),
        inspection_scope=frozenset({"video_detail"}),
    )

    assert snapshot.required_page_types == ("video_detail",)
    assert finalized.required_page_types == ("video_detail",)
    assert finalized.page_type_statuses["video_detail"] == "covered"
    assert finalized.page_type_statuses["home"] == "not_observed"
    assert finalized.page_type_statuses["video_list"] == "not_observed"
    assert finalized.page_type_statuses["content_list"] == "not_observed"


@pytest.mark.parametrize(
    ("label", "url", "expected"),
    [
        ("搜索", "https://example.com/search", "search_page"),
        ("专题活动", "https://example.com/events", "activity"),
        ("视频", "https://example.com/videos", "video_list"),
        ("新闻资讯", "https://example.com/news", "content_list"),
        ("首页", "https://example.com/", "home"),
        ("关于我们", "https://example.com/about", None),
    ],
)
def test_runtime_entry_type_inference_is_generic_and_conservative(
    label: str, url: str, expected: str | None
) -> None:
    assert infer_runtime_entry_page_type(label=label, url=url) == expected


def test_product_capability_revision_never_shrinks_frozen_denominator() -> None:
    frozen = resolve_web_page_applicability(
        target_url="https://heiliao.com/",
        requested_mode=None,
        requested_required_page_types=(),
    )
    frozen_required_page_types = frozen.required_page_types

    revised = revise_web_page_applicability(
        frozen,
        capabilities=(WebProductCapability.CONTENT,),
    )

    assert frozen.required_page_types == frozen_required_page_types
    assert revised.required_page_types == frozen_required_page_types
    assert revised.product_capabilities == ("content",)
    assert revised.resolution_reason == "product_capability_revision_no_denominator_shrink"
