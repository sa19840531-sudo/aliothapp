"""不依赖 PostgreSQL 的集成装配前置校验。"""

from services.api.tests.test_web_discovery_runtime_postgres import (
    test_public_request_rejects_historical_discovery_override,
    test_runtime_fixture_constructor_is_valid_without_postgres,
    test_runtime_fixture_uses_current_public_request,
)

__all__ = [
    "test_public_request_rejects_historical_discovery_override",
    "test_runtime_fixture_constructor_is_valid_without_postgres",
    "test_runtime_fixture_uses_current_public_request",
]
