from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.workstation_dashboard_repository import (
    PostgresWorkstationDashboardRepository,
)


class _AcquireContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self):
        return _AcquireContext(self.connection)


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.readonly = False
        self.settings: list[str] = []

    def transaction(self, *, readonly: bool):
        self.readonly = readonly
        return _AcquireContext(self)

    async def execute(self, sql: str):
        self.settings.append(sql)

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        return []

    async def fetchrow(self, sql: str, *args):
        self.calls.append((sql, args))
        return {"today": 0, "yesterday": 0}


async def test_dashboard_all_aggregations_use_the_same_owner_scope() -> None:
    connection = _Connection()
    scope = AccessScope(
        actor_user_id="00000000-0000-0000-0000-000000000010",
        tenant_id="00000000-0000-0000-0000-000000000020",
        permissions=frozenset({"taskObjects:read:tenant"}),
    )
    repository = PostgresWorkstationDashboardRepository(pool=_Pool(connection))

    result = await repository.get_dashboard_overview(access_scope=scope)

    assert result["summary_cards"][0]["value"] == 0
    assert len(connection.calls) == 4
    assert connection.readonly is True
    assert connection.settings == ["SET LOCAL jit = off"]
    for sql, args in connection.calls:
        assert "task.tenant_id" in sql
        assert "task.submitter_id" in sql
        assert "task.department_name" in sql
        assert args == (False, scope.tenant_id, True, scope.actor_user_id, None)
