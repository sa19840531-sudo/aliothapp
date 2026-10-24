"""兼容入口：正式定义见 domain.exploration.app_qc_package_scopes。"""

from services.api.app.domain.exploration.app_qc_package_scopes import (  # noqa: F401
    exploration_target_for_package,
    resolve_package_scope_code,
    task_remark_for_package,
)
from services.api.app.domain.exploration.hjll_coverage_scope import (  # noqa: F401
    HJLL_EXCLUDED_FROM_DENOMINATOR,
    HJLL_MINIMUM_BUSINESS_PAGES,
    HJLL_PACKAGE_NAME,
    HJLL_REQUIRED_PAGE_TYPES,
    hjll_exploration_target,
    hjll_task_remark,
)
from services.api.app.domain.exploration.hlapp_coverage_scope import (  # noqa: F401
    HLAPP_EXCLUDED_FROM_DENOMINATOR,
    HLAPP_MINIMUM_BUSINESS_PAGES,
    HLAPP_PACKAGE_NAME,
    HLAPP_REQUIRED_PAGE_TYPES,
    hlapp_exploration_target,
    hlapp_task_remark,
)
from services.api.app.domain.exploration.pc51_coverage_scope import (  # noqa: F401
    PC51_EXCLUDED_FROM_DENOMINATOR,
    PC51_MINIMUM_BUSINESS_PAGES,
    PC51_PACKAGE_NAME,
    PC51_REQUIRED_PAGE_TYPES,
    pc51_exploration_target,
    pc51_task_remark,
)
