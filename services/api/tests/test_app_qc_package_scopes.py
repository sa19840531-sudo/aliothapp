"""三包 App 质检覆盖分母配置。"""

from services.api.app.domain.exploration.app_qc_package_scopes import (
    exploration_target_for_package,
    resolve_package_scope_code,
    task_remark_for_package,
)
from services.api.app.domain.exploration.hjll_coverage_scope import (
    HJLL_EXCLUDED_FROM_DENOMINATOR,
    HJLL_REQUIRED_PAGE_TYPES,
    hjll_exploration_target,
)
from services.api.app.domain.exploration.hlapp_coverage_scope import (
    HLAPP_EXCLUDED_FROM_DENOMINATOR,
    HLAPP_REQUIRED_PAGE_TYPES,
    hlapp_exploration_target,
)
from services.api.app.domain.exploration.pc51_coverage_scope import (
    PC51_EXCLUDED_FROM_DENOMINATOR,
    PC51_REQUIRED_PAGE_TYPES,
    pc51_exploration_target,
)
from services.api.app.domain.page.registry import get_default_page_type_registry


def _assert_scope(
    required: tuple[str, ...],
    excluded: tuple[str, ...],
    target: dict[str, object],
) -> None:
    allowed = set(get_default_page_type_registry().coverage_required_codes)
    assert set(required) <= allowed
    assert set(excluded) <= allowed
    assert not (set(required) & set(excluded))
    assert target["required_page_types"] == list(required)
    assert target["minimum_business_pages"] == len(required)
    assert target["device_capability_template"] == "android_visual_readonly_v1"


def test_hlapp_scope_catalog_and_payload() -> None:
    _assert_scope(
        HLAPP_REQUIRED_PAGE_TYPES,
        HLAPP_EXCLUDED_FROM_DENOMINATOR,
        hlapp_exploration_target(),
    )


def test_hjll_scope_catalog_and_payload() -> None:
    _assert_scope(
        HJLL_REQUIRED_PAGE_TYPES,
        HJLL_EXCLUDED_FROM_DENOMINATOR,
        hjll_exploration_target(),
    )


def test_pc51_scope_catalog_and_payload() -> None:
    _assert_scope(
        PC51_REQUIRED_PAGE_TYPES,
        PC51_EXCLUDED_FROM_DENOMINATOR,
        pc51_exploration_target(),
    )


def test_resolve_by_package_name() -> None:
    assert resolve_package_scope_code("app.p6228f.pa3") == "hjll"
    assert resolve_package_scope_code("app.p12b04.p51") == "hjll"
    assert resolve_package_scope_code("vip.dibwo.txbazn") == "pc51"
    assert resolve_package_scope_code("top.qkjzg.zfiufm") == "hlapp"
    assert resolve_package_scope_code("com.edxuf.euloop") == "hlapp"
    assert resolve_package_scope_code("uk.frtju.nnxsaf") == "hlapp"
    assert resolve_package_scope_code("app.paf4c0.pede1e12de8c2f952a9aa") == "generic5"
    assert resolve_package_scope_code("app.p8b938.p1084bf711f98") == "generic5"
    assert resolve_package_scope_code("unknown.pkg") is None

    hjll = exploration_target_for_package("app.p6228f.pa3")
    assert hjll["minimum_business_pages"] == 6
    pc51 = exploration_target_for_package("vip.dibwo.txbazn")
    assert pc51["minimum_business_pages"] == 5
    hlapp = exploration_target_for_package("top.qkjzg.zfiufm")
    assert hlapp["minimum_business_pages"] == 7
    g5 = exploration_target_for_package("app.paf4c0.pede1e12de8c2f952a9aa")
    assert g5["minimum_business_pages"] == 5

    remark = task_remark_for_package(
        "app.p6228f.pa3",
        profile="qa-b",
        apk_name="hjll3.1.0.apk",
    )
    assert "coverage_scope=hjll_6_types" in remark
    assert "hard_coverage=6/6" in remark
