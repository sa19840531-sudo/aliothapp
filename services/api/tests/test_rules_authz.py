from dataclasses import dataclass

from fastapi.testclient import TestClient

from services.api.app.api.v1 import rules as rules_module
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth


@dataclass
class FakeRuleRepository:
    async def list_rulesets(self, *, keyword, status, page, page_size):
        return [], 0


def test_rules_api_requires_authentication() -> None:
    app = create_app()
    pop_test_auth(app)
    app.dependency_overrides[rules_module._get_rule_repository] = lambda: FakeRuleRepository()
    client = TestClient(app)

    response = client.get('/api/v1/rulesets')

    assert response.status_code == 401
    assert response.json()['error']['message'] == '缺少登录令牌'
