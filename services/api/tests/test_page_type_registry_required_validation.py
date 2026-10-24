"""Registry 必须执行设计约定的完整失败关闭校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services.api.app.domain.page.registry import PageTypeRegistry


def _default_payload() -> dict[str, Any]:
    path = Path(__file__).parents[1] / "app" / "domain" / "page" / "page_type_catalog.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_catalog(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_registry_rejects_unknown_schema_version(tmp_path: Path) -> None:
    payload = _default_payload()
    payload["schema_version"] = "page-type-catalog.v999"

    with pytest.raises(ValueError, match="schema_version"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


def test_registry_rejects_invalid_code_format(tmp_path: Path) -> None:
    payload = _default_payload()
    payload["page_types"][1]["code"] = "Invalid-Code"

    with pytest.raises(ValueError, match="code"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


def test_registry_rejects_unknown_category(tmp_path: Path) -> None:
    payload = _default_payload()
    payload["page_types"][1]["category"] = "unknown"

    with pytest.raises(ValueError, match="category"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


def test_registry_rejects_non_boolean_coverage_flag(tmp_path: Path) -> None:
    payload = _default_payload()
    payload["page_types"][1]["coverage_required"] = "false"

    with pytest.raises(ValueError, match="coverage_required"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


def test_registry_rejects_invalid_category_and_coverage_pair(tmp_path: Path) -> None:
    payload = _default_payload()
    payload["page_types"][1]["category"] = "lifecycle"

    with pytest.raises(ValueError, match="coverage_required"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


def test_registry_rejects_transient_coverage_requirement(tmp_path: Path) -> None:
    payload = _default_payload()
    payload["page_types"][0]["category"] = "business"
    payload["page_types"][0]["coverage_required"] = True

    with pytest.raises(ValueError, match="transient"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


def test_registry_rejects_unknown_entry_hint_reference(tmp_path: Path) -> None:
    payload = _default_payload()
    payload["page_types"][1]["entry_hints"][0]["from_page_type"] = "missing_source"

    with pytest.raises(ValueError, match="missing_source"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


def test_registry_rejects_unknown_next_page_reference(tmp_path: Path) -> None:
    payload = _default_payload()
    payload["page_types"][1]["next_page_candidates"][0]["page_type"] = "missing_target"

    with pytest.raises(ValueError, match="missing_target"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


@pytest.mark.parametrize(
    "field_name",
    (
        "purpose",
        "required_positive_signals",
        "supporting_signals",
        "strong_negative_signals",
        "confusable_with",
        "missing_evidence_actions",
    ),
)
def test_registry_rejects_missing_recognition_guide(
    tmp_path: Path,
    field_name: str,
) -> None:
    payload = _default_payload()
    payload["page_types"][1][field_name] = "" if field_name == "purpose" else []

    with pytest.raises(ValueError, match=field_name):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


def test_registry_rejects_conflicting_recognition_signals(tmp_path: Path) -> None:
    payload = _default_payload()
    signal = payload["page_types"][1]["required_positive_signals"][0]
    payload["page_types"][1]["strong_negative_signals"].append(signal)

    with pytest.raises(ValueError, match="正证与强反证冲突"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


def test_registry_rejects_unknown_confusable_page_type(tmp_path: Path) -> None:
    payload = _default_payload()
    payload["page_types"][1]["confusable_with"].append("missing_page_type")

    with pytest.raises(ValueError, match="confusable_with"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))


def test_registry_rejects_duplicate_recognition_guide_rule(tmp_path: Path) -> None:
    payload = _default_payload()
    signal = payload["page_types"][1]["supporting_signals"][0]
    payload["page_types"][1]["supporting_signals"].append(signal)

    with pytest.raises(ValueError, match="存在重复规则"):
        PageTypeRegistry.from_path(_write_catalog(tmp_path, payload))
