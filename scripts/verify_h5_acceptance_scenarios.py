"""校验 W46/W47 H5 验收场景清单是否满足执行前合同。"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from services.api.app.domain.page.registry import get_default_page_type_registry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO_PATH = ROOT / "docs/data/h5_acceptance_scenarios.csv"

REQUIRED_COLUMNS = (
    "scenario_id",
    "scenario_name",
    "priority",
    "target_category",
    "product_code",
    "target_url",
    "ruleset_code",
    "ruleset_version",
    "expected_surface_phases",
    "expected_page_types",
    "expected_rule_scope",
    "required_artifacts",
    "pass_criteria",
    "risk_notes",
)
ALLOWED_PRIORITIES = {"P0", "P1", "P2"}
ALLOWED_TARGET_CATEGORIES = {"mainline", "exploration", "safety", "recovery"}
ALLOWED_PHASE_ONLY_TYPES = {"auth_gate", "popup_chain", "system_dialog"}
ALLOWED_ARTIFACT_KINDS = {
    "after_action_screenshot",
    "auth_gate_screenshot",
    "before_action_screenshot",
    "crash_trace",
    "dialog_after_handle",
    "dialog_before_handle",
    "dom_summary",
    "evidence_preflight_trace",
    "external_landing_screenshot",
    "external_return_trace",
    "external_source_screenshot",
    "js_navigation_trace",
    "llm_contract_error_trace",
    "long_page_segment",
    "popup_after_close",
    "popup_before_close",
    "recapture_manifest",
    "recovery_trace",
    "retry_trace",
    "safety_block_screenshot",
    "screenshot",
    "scroll_position",
    "stability_trace",
    "startup_ad_after_close",
    "startup_ad_before_close",
    "task_cancellation_trace",
    "viewport_trace",
    "visible_text",
}
REQUIRED_SCENARIOS = {
    "W46-H5-001",
    "W46-H5-002",
    "W46-H5-003",
    "W46-H5-004",
    "W46-H5-005",
    "W46-H5-011",
    "W46-H5-006",
    "W46-H5-007",
    "W46-H5-008",
    "W46-H5-009",
    "W46-H5-010",
    "W46-H5-012",
    "W46-H5-013",
    "W46-H5-014",
    "W46-H5-015",
    "W46-H5-016",
    "W46-H5-017",
    "W46-H5-101",
    "W46-H5-102",
    "W46-H5-103",
    "W46-H5-104",
    "W46-H5-105",
}
SCENARIO_ID_PATTERN = re.compile(r"^W46-H5-(?:0[0-9]{2}|1[0-9]{2})$")


@dataclass(frozen=True)
class ScenarioValidationIssue:
    """单条场景清单校验问题。"""

    code: str
    message: str


def load_scenarios(path: str | Path = DEFAULT_SCENARIO_PATH) -> list[dict[str, str]]:
    """读取 CSV 场景清单，并保留原始字符串字段用于合同校验。"""
    with Path(path).open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise ValueError("H5 验收场景清单缺少表头")
        rows = list(reader)
    return rows


def validate_scenarios(rows: Iterable[dict[str, str]]) -> list[ScenarioValidationIssue]:
    """返回全部场景清单问题；空列表表示通过。"""
    materialized = list(rows)
    issues: list[ScenarioValidationIssue] = []
    _validate_columns(materialized, issues)
    _validate_required_scenarios(materialized, issues)
    _validate_unique_fields(materialized, issues)

    registry = get_default_page_type_registry()
    allowed_page_types = set(registry.all_codes)
    allowed_phases = allowed_page_types | ALLOWED_PHASE_ONLY_TYPES

    for index, row in enumerate(materialized, start=2):
        scenario_id = _text(row.get("scenario_id"))
        row_label = scenario_id or f"第 {index} 行"
        _validate_non_empty_required_fields(row, row_label, issues)
        if scenario_id and not SCENARIO_ID_PATTERN.fullmatch(scenario_id):
            issues.append(
                ScenarioValidationIssue(
                    "INVALID_SCENARIO_ID",
                    f"{row_label} scenario_id 格式非法",
                )
            )
        if _text(row.get("priority")) not in ALLOWED_PRIORITIES:
            issues.append(
                ScenarioValidationIssue("INVALID_PRIORITY", f"{row_label} priority 非法")
            )
        if _text(row.get("target_category")) not in ALLOWED_TARGET_CATEGORIES:
            issues.append(
                ScenarioValidationIssue(
                    "INVALID_TARGET_CATEGORY",
                    f"{row_label} target_category 非法",
                )
            )
        _validate_csv_values(
            row,
            row_label,
            field="expected_surface_phases",
            allowed_values=allowed_phases,
            issues=issues,
        )
        _validate_csv_values(
            row,
            row_label,
            field="expected_page_types",
            allowed_values=allowed_page_types,
            issues=issues,
        )
        _validate_csv_values(
            row,
            row_label,
            field="expected_rule_scope",
            allowed_values=allowed_page_types,
            issues=issues,
        )
        _validate_csv_values(
            row,
            row_label,
            field="required_artifacts",
            allowed_values=ALLOWED_ARTIFACT_KINDS,
            issues=issues,
        )
    return issues


def _validate_columns(
    rows: list[dict[str, str]],
    issues: list[ScenarioValidationIssue],
) -> None:
    if not rows:
        issues.append(ScenarioValidationIssue("EMPTY_SCENARIOS", "H5 验收场景清单为空"))
        return
    actual_columns = tuple(rows[0].keys())
    if actual_columns != REQUIRED_COLUMNS:
        issues.append(
            ScenarioValidationIssue(
                "INVALID_COLUMNS",
                "H5 验收场景清单表头必须与合同一致",
            )
        )


def _validate_required_scenarios(
    rows: list[dict[str, str]],
    issues: list[ScenarioValidationIssue],
) -> None:
    scenario_ids = {_text(row.get("scenario_id")) for row in rows}
    missing = sorted(REQUIRED_SCENARIOS - scenario_ids)
    if missing:
        issues.append(
            ScenarioValidationIssue(
                "MISSING_REQUIRED_SCENARIOS",
                f"缺少必选验收场景：{', '.join(missing)}",
            )
        )


def _validate_unique_fields(
    rows: list[dict[str, str]],
    issues: list[ScenarioValidationIssue],
) -> None:
    for field in ("scenario_id", "product_code"):
        values = [_text(row.get(field)) for row in rows if _text(row.get(field))]
        duplicated = sorted({value for value in values if values.count(value) > 1})
        if duplicated:
            issues.append(
                ScenarioValidationIssue(
                    "DUPLICATED_FIELD",
                    f"{field} 重复：{', '.join(duplicated)}",
                )
            )


def _validate_non_empty_required_fields(
    row: dict[str, str],
    row_label: str,
    issues: list[ScenarioValidationIssue],
) -> None:
    for field in REQUIRED_COLUMNS:
        if not _text(row.get(field)):
            issues.append(
                ScenarioValidationIssue(
                    "EMPTY_FIELD",
                    f"{row_label} 字段 {field} 不能为空",
                )
            )


def _validate_csv_values(
    row: dict[str, str],
    row_label: str,
    *,
    field: str,
    allowed_values: set[str],
    issues: list[ScenarioValidationIssue],
) -> None:
    values = _split_csv_field(row.get(field))
    if not values:
        issues.append(
            ScenarioValidationIssue(
                "EMPTY_LIST_FIELD",
                f"{row_label} 字段 {field} 至少声明一个值",
            )
        )
        return
    unknown_values = [value for value in values if value not in allowed_values]
    if unknown_values:
        issues.append(
            ScenarioValidationIssue(
                "UNKNOWN_LIST_VALUE",
                f"{row_label} 字段 {field} 存在未知值：{', '.join(unknown_values)}",
            )
        )


def _split_csv_field(value: str | None) -> list[str]:
    return [item.strip() for item in _text(value).split(",") if item.strip()]


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario-csv",
        default=str(DEFAULT_SCENARIO_PATH),
        help="H5 验收场景 CSV 文件路径",
    )
    args = parser.parse_args(argv)

    try:
        rows = load_scenarios(args.scenario_csv)
    except OSError as exc:
        print(f"读取 H5 验收场景失败：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"H5 验收场景格式错误：{exc}", file=sys.stderr)
        return 2

    issues = validate_scenarios(rows)
    if issues:
        print(f"H5 acceptance scenarios: {len(issues)} issue(s)")
        for issue in issues:
            print(f"[FAIL] {issue.code}: {issue.message}")
        return 1
    print(f"H5 acceptance scenarios: {len(rows)} scenario(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
