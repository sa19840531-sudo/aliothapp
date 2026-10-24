"""检查点观测类型必须贯通采集、需求词汇和数据库，不允许未知类型混入。"""

import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.api.app.domain.evidence import normalize_artifact_type
from services.api.app.domain.evidence.vocabulary import EvidenceVocabularyError


def test_checkpoint_observation_is_a_registered_evidence_requirement():
    assert normalize_artifact_type("checkpoint_observation") == "checkpoint_observation"
    with pytest.raises(EvidenceVocabularyError):
        normalize_artifact_type("invented_observation")


def test_migration_keeps_legacy_types_and_refuses_removing_persisted_samples():
    migration = importlib.import_module(
        "services.api.migrations.versions.20261016_0138_checkpoint_artifact_type"
    )
    legacy = importlib.import_module(
        "services.api.migrations.versions.20260820_0073_app_evidence_artifact_types"
    )
    assert migration.LEGACY_TYPES == legacy._ARTIFACT_TYPES
    statements = []
    with patch.object(migration, "op", SimpleNamespace(execute=statements.append)):
        migration.downgrade()
    assert "RAISE EXCEPTION" in statements[0]
    assert "checkpoint_observation" in statements[0]
    assert "DELETE" not in "".join(statements)
