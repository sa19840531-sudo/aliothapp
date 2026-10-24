"""黑料不打烊业务侧覆盖分母配置。"""

from services.api.app.domain.exploration.hlapp_coverage_scope import (
    HLAPP_EXCLUDED_FROM_DENOMINATOR,
    HLAPP_REQUIRED_PAGE_TYPES,
    hlapp_exploration_target,
    hlapp_task_remark,
)
from services.api.app.domain.page.registry import get_default_page_type_registry


def test_hlapp_scope_only_uses_coverage_required_catalog_codes() -> None:
    allowed = set(get_default_page_type_registry().coverage_required_codes)
    assert set(HLAPP_REQUIRED_PAGE_TYPES) <= allowed
    assert set(HLAPP_EXCLUDED_FROM_DENOMINATOR) <= allowed
    assert not (set(HLAPP_REQUIRED_PAGE_TYPES) & set(HLAPP_EXCLUDED_FROM_DENOMINATOR))


def test_hlapp_exploration_target_payload() -> None:
    target = hlapp_exploration_target()
    assert target["required_page_types"] == list(HLAPP_REQUIRED_PAGE_TYPES)
    assert target["minimum_business_pages"] == 7
    assert target["minimum_business_pages"] == len(HLAPP_REQUIRED_PAGE_TYPES)
    assert target["device_capability_template"] == "android_visual_readonly_v1"
    remark = hlapp_task_remark(
        profile="qa-b",
        apk_name="hlapp_1.8.9.apk",
        script_path="untitled1.py",
    )
    assert "age_confirm=我年满18岁-进入" in remark
    assert "hard_coverage=7/7" in remark
    assert "no_test_account" in remark
    assert "exclude_denominator=profile/settings/payment/post_publish" in remark
