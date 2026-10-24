"""Web assessment v4 在生产 SQL 联动路径中的冻结合同。"""

from inspect import getsource

from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    _GATE_FACTS_SQL,
)
from services.api.app.infrastructure.db.web_capture_identity import (
    read_web_capture_identities,
)
from services.api.app.infrastructure.db.web_completion_gate_sql import (
    WEB_COMPLETION_GATE_INVENTORY_CTES_SQL,
)
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    _FINALIZE_SQL,
)
from services.api.app.infrastructure.db.web_logical_target_repository import (
    PostgresWebLogicalTargetRepository,
)

ASSESSMENT_VERSIONS = tuple(
    f"web-page-type-assessment.v{version}" for version in range(1, 5)
)


def _assert_all_web_assessment_versions(source: str, *, minimum_occurrences: int = 1) -> None:
    for version in ASSESSMENT_VERSIONS:
        assert source.count(version) >= minimum_occurrences


def test_logical_target_materialization_accepts_v4_and_retains_v1_v3() -> None:
    source = getsource(PostgresWebLogicalTargetRepository)
    _assert_all_web_assessment_versions(source, minimum_occurrences=2)


def test_completion_assessment_and_inventory_accept_v4_and_retains_v1_v3() -> None:
    _assert_all_web_assessment_versions(
        WEB_COMPLETION_GATE_INVENTORY_CTES_SQL,
        minimum_occurrences=2,
    )


def test_strict_web_publication_gate_accepts_v4_without_changing_default_branch() -> None:
    source = getsource(
        PostgresH5InspectionPublicationRepository._validate_strict_web_incremental_gate
    )
    _assert_all_web_assessment_versions(source)


def test_web_capture_identity_material_accepts_v4_for_target_and_assessment() -> None:
    source = getsource(read_web_capture_identities)
    _assert_all_web_assessment_versions(source, minimum_occurrences=2)


def test_task_finalization_web_coverage_gate_accepts_v4_and_retains_history() -> None:
    normalized = " ".join(_GATE_FACTS_SQL.split())
    assert (
        "snapshot.normalization_version IN "
        "('web-page-type-normalization.v1', 'web-page-type-assessment.v2', "
        "'web-page-type-assessment.v3', 'web-page-type-assessment.v4')"
    ) in normalized


def test_web_home_budget_gate_accepts_v4_and_retains_v3_and_legacy_branch() -> None:
    assert "home_assessment.schema_version IN (" in _FINALIZE_SQL
    assert "'web-page-type-assessment.v3'" in _FINALIZE_SQL
    assert "'web-page-type-assessment.v4'" in _FINALIZE_SQL
    for version in (
        "web-page-type-normalization.v1",
        "web-page-type-assessment.v1",
        "web-page-type-assessment.v2",
    ):
        assert version in _FINALIZE_SQL
