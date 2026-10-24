from datetime import UTC, datetime

from services.api.app.infrastructure.db.rule_repository import PostgresRuleRepository


class FakeAcquire:
    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection

    async def __aenter__(self) -> "FakeConnection":
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, row: dict | None = None, *, rows: list[dict] | None = None) -> None:
        self.connection = FakeConnection(row=row, rows=rows or [])

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeConnection:
    def __init__(self, *, row: dict | None, rows: list[dict]) -> None:
        self.row = row
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.row

    async def fetch(self, query: str, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.rows


async def test_postgres_rule_repository_lists_rulesets_with_filters() -> None:
    created_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    rows = [
        {
            "id": "ruleset-1",
            "ruleset_code": "alioth_first_ruleset",
            "name": "首版规则集",
            "version": 1,
            "status": "draft",
            "description": None,
            "source_type": "manual",
            "source_ref": None,
            "snapshot_hash": None,
            "rule_count": 2,
            "page_sampling_policy": {},
            "loop_guard": {},
            "created_by": None,
            "published_at": None,
            "archived_at": None,
            "created_at": created_at,
            "updated_at": created_at,
            "total": 1,
        }
    ]
    pool = FakePool(rows=rows)
    repository = PostgresRuleRepository(pool=pool)

    items, total = await repository.list_rulesets(
        keyword="首版",
        status="draft",
        page=2,
        page_size=10,
    )

    query, args = pool.connection.calls[0]
    assert "WITH filtered AS" in query
    assert "ruleset_code ILIKE" in query
    assert "COUNT(*) OVER() AS total" in query
    assert args == ("首版", "draft", 10, 10)
    assert total == 1
    assert items[0]["id"] == "ruleset-1"


async def test_postgres_rule_repository_list_rulesets_normalizes_json_string_fields() -> None:
    created_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    rows = [
        {
            "id": "ruleset-1",
            "ruleset_code": "alioth_first_ruleset",
            "name": "首版规则集",
            "version": 1,
            "status": "draft",
            "description": None,
            "source_type": "manual",
            "source_ref": None,
            "snapshot_hash": None,
            "rule_count": 2,
            "page_sampling_policy": '{"mode": "by_page_type", "include_page_types": []}',
            "loop_guard": '{"max_steps": 80, "max_duration_seconds": 1800}',
            "created_by": None,
            "published_at": None,
            "archived_at": None,
            "created_at": created_at,
            "updated_at": created_at,
            "total": 1,
        }
    ]
    pool = FakePool(rows=rows)
    repository = PostgresRuleRepository(pool=pool)

    items, total = await repository.list_rulesets(keyword=None, status=None, page=1, page_size=8)

    assert total == 1
    assert items[0]["page_sampling_policy"] == {"mode": "by_page_type", "include_page_types": []}
    assert items[0]["loop_guard"] == {"max_steps": 80, "max_duration_seconds": 1800}


async def test_postgres_rule_repository_creates_and_updates_ruleset() -> None:
    created_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    pool = FakePool(
        row={
            "id": "ruleset-1",
            "ruleset_code": "alioth_first_ruleset",
            "name": "首版规则集",
            "version": 1,
            "status": "draft",
            "description": None,
            "source_type": "manual",
            "source_ref": None,
            "snapshot_hash": None,
            "rule_count": 0,
            "page_sampling_policy": {},
            "loop_guard": {},
            "created_by": None,
            "published_at": None,
            "archived_at": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
    )
    repository = PostgresRuleRepository(pool=pool)

    created = await repository.create_ruleset(
        {
            "ruleset_code": "alioth_first_ruleset",
            "name": "首版规则集",
            "version": 1,
            "status": "draft",
            "description": "首版",
            "source_type": "manual",
            "source_ref": "",
            "page_sampling_policy": {"mode": "first_n"},
            "loop_guard": {"max_steps": 30},
        }
    )
    assert created["ruleset_code"] == "alioth_first_ruleset"

    updated = await repository.update_ruleset(
        "ruleset-1",
        {
            "ruleset_code": "alioth_first_ruleset",
            "name": "首版规则集 v2",
            "version": 2,
            "status": "enabled",
            "description": "更新后",
            "source_type": "manual",
            "source_ref": "ref",
            "page_sampling_policy": {},
            "loop_guard": {},
        },
    )
    assert updated is not None

    query, args = pool.connection.calls[1]
    assert "UPDATE qc_ruleset" in query
    assert args[0] == "ruleset-1"
    assert args[4] == "enabled"


async def test_postgres_rule_repository_gets_rule_and_updates_sort_order_zero() -> None:
    created_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    pool = FakePool(
        row={
            "id": "rule-1",
            "ruleset_id": "ruleset-1",
            "rule_code": "alioth_rule_001",
            "name": "规则 1",
            "description": None,
            "category": "text",
            "executor_type": "hybrid",
            "severity": "medium",
            "status": "draft",
            "scope": None,
            "channel": None,
            "page_type": None,
            "applicable_page_types": ["home"],
            "url_pattern": None,
            "steps": [],
            "input_contract": {},
            "check_point": "检查首页文案",
            "judge_config": {},
            "manual_review_policy": {},
            "sort_order": 0,
            "created_by": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
    )
    repository = PostgresRuleRepository(pool=pool)

    row = await repository.get_rule("rule-1")
    assert row is not None
    assert row["sort_order"] == 0

    created = await repository.create_rule(
        {
            "ruleset_id": "ruleset-1",
            "rule_code": "alioth_rule_001",
            "name": "规则 1",
            "description": "",
            "category": "text",
            "executor_type": "hybrid",
            "severity": "medium",
            "status": "draft",
            "scope": "",
            "channel": "workbench",
            "page_type": "",
            "applicable_page_types": ["home"],
            "url_pattern": "",
            "steps": [],
            "input_contract": {},
            "check_point": "检查首页文案",
            "judge_config": {},
            "manual_review_policy": {},
            "sort_order": 0,
        }
    )
    assert created["sort_order"] == 0

    updated = await repository.update_rule(
        "rule-1",
        {
            "ruleset_id": "ruleset-1",
            "rule_code": "alioth_rule_001",
            "name": "规则 1-更新",
            "description": "",
            "category": "text",
            "executor_type": "hybrid",
            "severity": "medium",
            "status": "enabled",
            "scope": "",
            "channel": "workbench",
            "page_type": "",
            "applicable_page_types": ["home"],
            "url_pattern": "",
            "steps": [],
            "input_contract": {},
            "check_point": "检查首页文案",
            "judge_config": {},
            "manual_review_policy": {},
            "sort_order": 0,
        },
    )
    assert updated is not None
    assert updated["sort_order"] == 0


async def test_postgres_rule_repository_list_rules_normalizes_json_string_fields() -> None:
    created_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    rows = [
        {
            "id": "rule-1",
            "ruleset_id": "ruleset-1",
            "rule_code": "alioth_rule_001",
            "name": "规则 1",
            "description": None,
            "category": "text",
            "executor_type": "hybrid",
            "severity": "medium",
            "status": "draft",
            "scope": None,
            "channel": None,
            "page_type": None,
            "applicable_page_types": '["home", "detail"]',
            "url_pattern": None,
            "steps": '[{"step": "检查首页文案"}]',
            "input_contract": '{"type": "object"}',
            "check_point": "检查首页文案",
            "judge_config": '{"strategy": "hybrid"}',
            "manual_review_policy": '{"mode": "auto"}',
            "sort_order": 100,
            "created_by": None,
            "created_at": created_at,
            "updated_at": created_at,
            "total": 1,
        }
    ]
    pool = FakePool(rows=rows)
    repository = PostgresRuleRepository(pool=pool)

    items, total = await repository.list_rules(
        ruleset_id="ruleset-1",
        keyword=None,
        category=None,
        status=None,
        page=1,
        page_size=10,
    )

    assert total == 1
    assert items[0]["applicable_page_types"] == ["home", "detail"]
    assert items[0]["steps"] == [{"step": "检查首页文案"}]
    assert items[0]["input_contract"] == {"type": "object"}
    assert items[0]["judge_config"] == {"strategy": "hybrid"}
    assert items[0]["manual_review_policy"] == {"mode": "auto"}


async def test_postgres_rule_repository_delete_paths_return_boolean() -> None:
    pool = FakePool(row={"id": "rule-1"})
    repository = PostgresRuleRepository(pool=pool)

    assert await repository.delete_rule("rule-1") is True
    assert await repository.delete_ruleset("ruleset-1") is True
