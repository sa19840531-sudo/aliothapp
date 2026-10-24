"""验证证据白名单、可恢复增量游标和分层验收门槛。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

TERMINAL = frozenset({"completed", "done", "failed", "aborted", "canceled", "cancelled", "review"})
STAGE_TERMINAL = frozenset({"succeeded", "gapped", "cancelled"})
FIELDS = frozenset(
    {
        "task_id",
        "run_id",
        "requested_url",
        "final_url",
        "url",
        "canonical_url",
        "normalized_url",
        "raw_href",
        "raw_href_gap",
        "status",
        "session_closed",
        "report_finalized",
        "root_navigation",
        "root_gap",
        "candidate_filter_observed",
        "capture_id",
        "parent_capture_id",
        "role",
        "capture_purpose",
        "page_type",
        "confidence",
        "artifacts",
        "artifact_id",
        "artifact_type",
        "local_path",
        "storage_path",
        "screenshot_artifact_id",
        "dom_artifact_id",
        "full_page_screenshot_artifact_id",
        "artifact_ids",
        "obstruction_chain",
        "enabled",
        "detected",
        "dismissal_count",
        "outcome",
        "remaining_obstruction",
        "action_execution_id",
        "intent_id",
        "command_sequence",
        "result_capture_id",
        "action",
        "menu_entry_id",
        "error_code",
        "response_received",
        "current_stage",
        "stage_status",
        "logical_page_key",
        "row_version",
        "attempt_count",
        "next_attempt_at",
        "gap_codes",
        "last_error_code",
        "current_url",
        "cursor_payload",
        "checkpoints",
        "captures",
        "frontier",
        "candidates",
        "actions",
        "failures",
        "capabilities",
        "list_structure_clusters",
        "detail_structure_clusters",
        "age_gate",
        "popup",
        "cross_origin_recovery",
        "ad_isolation",
        "long_page",
        "structure_cluster_key",
        "structure_fingerprint",
        "menu_limit",
        "policy_menu_limit",
        "execution_menu_limit",
        "menu_limit_consistent",
        "execution_budgets",
        "site_id",
        "executed_unique_menu_count",
        "menu_limit_skipped_count",
        "rejected_count",
        "alias_count",
        "budget_reason",
        "budget_state",
        "execution_slot",
        "source_menu_execution_slot",
        "executed",
        "action_status",
        "candidate_id",
        "candidate_kind",
        "candidate_source_kind",
        "frontier_status",
        "source_capture_id",
        "session_id",
        "finalization_epoch",
        "candidate_href_index",
        "root_failure",
        "category",
        "blocks_code_acceptance",
        "minimum_witness",
        "complete",
        "missing",
    }
)
FACT_FIELDS = frozenset(
    {
        "primary_subject_count",
        "primary_article_body_count",
        "primary_article_text_length",
        "primary_player_count",
        "inline_article_media_count",
        "repeated_primary_card_count",
        "secondary_recommendation_card_count",
        "breadcrumb_count",
        "detail_navigation_source_count",
    }
)


def safe_projection(value, *, secrets=(), key=""):
    """只写结构化事实；URL 去掉用户信息、查询值和 fragment。"""
    if key == "subject_region_facts" and isinstance(value, dict):
        return {
            name: item
            for name, item in value.items()
            if name in FACT_FIELDS and type(item) is int and item >= 0
        }
    if isinstance(value, dict):
        return {
            name: safe_projection(item, secrets=secrets, key=name)
            for name, item in value.items()
            if name in FIELDS or name == "subject_region_facts"
        }
    if isinstance(value, (list, tuple)):
        return [safe_projection(item, secrets=secrets, key=key) for item in value]
    if isinstance(value, str):
        if key == "raw_href":
            try:
                parts = urlsplit(value)
                if parts.scheme or parts.netloc:
                    if parts.scheme not in {"http", "https"} or not parts.hostname:
                        return None
                    return safe_projection(value, secrets=secrets, key="url")
                value = parts.path
            except ValueError:
                return None
        if key.endswith("url"):
            try:
                parts = urlsplit(value)
                if parts.scheme not in {"http", "https"} or not parts.hostname:
                    return None
                host = parts.hostname
                if ":" in host:
                    host = f"[{host}]"
                value = urlunsplit(
                    (
                        parts.scheme,
                        host + (f":{parts.port}" if parts.port else ""),
                        parts.path,
                        "",
                        "",
                    )
                )
            except ValueError:
                return None
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        if any(mark in value for mark in ("<", ">", "\n", "\r")):
            return "[REDACTED]"
        return re.sub(
            r"(?i)(bearer\s+|(?:password|token|secret|cookie)\s*[:=]\s*)\S+", "[REDACTED]", value
        )[:4096]
    return value if value is None or isinstance(value, (bool, int, float)) else str(value)


@dataclass(frozen=True)
class ValidationFailure:
    error_code: str
    category: str
    blocks_code_acceptance: bool


def is_nonblocking_supplemental_failure(error_code: str) -> bool:
    """长图失败只降低补充证据能力，不否定已经落盘的主页面证据。"""
    return error_code.startswith("WEB_FULL_PAGE_SCREENSHOT_")


def classify_validation_failure(
    error_code: str, *, response_received: bool | None
) -> ValidationFailure:
    if is_nonblocking_supplemental_failure(error_code):
        return ValidationFailure(error_code, "supplemental_evidence", False)
    if error_code.startswith("WEB_AUXILIARY_"):
        return ValidationFailure(error_code, "optional_auxiliary", False)
    external = response_received is False and error_code in {
        "WEB_GOTO_TLS_FAILED",
        "WEB_GOTO_CONNECTION_FAILED",
        "WEB_GOTO_TIMEOUT",
        "WEB_NAVIGATION_TLS_FAILED",
        "WEB_NAVIGATION_CONNECTION_RESET",
    }
    return ValidationFailure(
        error_code, "external_transport" if external else "code_or_evidence", not external
    )


@dataclass(frozen=True)
class MinimumWitness:
    complete: bool
    missing: tuple[str, ...]


class SiteMinimumWitnessPolicy:
    """每站只要求运行和证据完整性，不要求多种内容模板。"""

    def evaluate(self, state: dict) -> MinimumWitness:
        missing = []
        failure = state.get("root_failure")
        external = (
            isinstance(failure, dict)
            and failure.get("category") == "external_transport"
            and failure.get("blocks_code_acceptance") is False
        )
        if state.get("root_navigation") != "succeeded" and not external:
            missing.append("root_navigation")
        home_ids = {
            row.get("cursor_payload", {}).get("capture_id")
            for row in state.get("checkpoints", [])
            if row.get("logical_page_key") == "home"
        }
        home = any(
            (row.get("role") == "bootstrap" or row.get("capture_id") in home_ids)
            and {"dom", "screenshot"}.issubset(
                item.get("artifact_type")
                for item in row.get("artifacts", [])
                if item.get("artifact_id")
            )
            for row in state.get("captures", [])
        )
        if not home and not state.get("root_gap"):
            missing.append("home_evidence_or_gap")
        if state.get("candidate_filter_observed") is not True:
            missing.append("candidate_filter_observed")
        checkpoints = state.get("checkpoints", [])
        if not checkpoints:
            missing.append("checkpoint_evidence")
        if any(row.get("stage_status") not in STAGE_TERMINAL for row in checkpoints):
            missing.append("nonterminal_checkpoint")
        if state.get("status") not in TERMINAL:
            missing.append("run_terminal")
        for name in ("session_closed", "report_finalized"):
            if state.get(name) is not True:
                missing.append(name)
        return MinimumWitness(not missing, tuple(missing))


@dataclass(frozen=True)
class SuiteCapabilityMatrix:
    list_structure_clusters: int
    detail_structure_clusters: int
    witnessed: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def complete(self):
        return not self.missing

    @classmethod
    def from_sites(cls, sites):
        lists, details, witnessed = set(), set(), set()
        flags = {"age_gate", "popup", "cross_origin_recovery", "ad_isolation", "long_page"}
        for site in sites:
            cap = site.get("capabilities", {})
            lists.update(cap.get("list_structure_clusters", []))
            details.update(cap.get("detail_structure_clusters", []))
            witnessed.update(name for name in flags if cap.get(name) is True)
        missing = flags - witnessed
        if len(lists) < 2:
            missing.add("list_structure_clusters")
        if len(details) < 4:
            missing.add("detail_structure_clusters")
        return cls(len(lists), len(details), tuple(sorted(witnessed)), tuple(sorted(missing)))


@dataclass(frozen=True)
class ValidationDelta:
    events: tuple[dict, ...]
    next_cursor: dict[str, str]
    summary: dict

    @property
    def event_ids(self):
        return tuple(row["event_id"] for row in self.events)


class AcceptanceReporter:
    """游标保存实体版本摘要；JSONL 为事实日志，重启后可重建 Markdown。"""

    def __init__(self, source, output: Path, *, secrets=()):
        self.source, self.output, self.secrets = source, Path(output), secrets

    def collect_delta(self, cursor=None) -> ValidationDelta:
        state = safe_projection(self.source(), secrets=self.secrets)
        previous = dict(cursor or {})
        events = []
        groups = {
            "capture": "captures",
            "checkpoint": "checkpoints",
            "frontier": "frontier",
            "candidate": "candidates",
            "action": "actions",
            "failure": "failures",
        }
        identity_fields = (
            "capture_id",
            "logical_page_key",
            "candidate_id",
            "menu_entry_id",
            "action_execution_id",
            "error_code",
        )
        run = {
            name: state.get(name)
            for name in FIELDS
            if name in state and name not in groups.values()
        }
        records = [("run", state.get("run_id", ""), run)]
        for kind, group in groups.items():
            for index, row in enumerate(state.get(group, [])):
                identity = next(
                    (str(row[name]) for name in identity_fields if row.get(name)), str(index)
                )
                if kind == "capture":
                    for artifact in row.get("artifacts", []):
                        records.append(
                            (
                                "artifact",
                                artifact.get("artifact_id", ""),
                                {**artifact, "capture_id": row.get("capture_id")},
                            )
                        )
                    row = {name: value for name, value in row.items() if name != "artifacts"}
                records.append((kind, identity, row))
        for kind, identity, payload in records:
            key = f"{state.get('run_id')}:{kind}:{identity}"
            digest = sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            if previous.get(key) != digest:
                events.append(
                    {
                        "event_id": sha256(f"{key}:{digest}".encode()).hexdigest(),
                        "kind": kind,
                        "task_id": state.get("task_id"),
                        "run_id": state.get("run_id"),
                        **payload,
                    }
                )
                previous[key] = digest
        state["minimum_witness"] = asdict(SiteMinimumWitnessPolicy().evaluate(state))
        return ValidationDelta(tuple(events), previous, state)

    def write_delta(self, delta: ValidationDelta):
        self.output.mkdir(parents=True, exist_ok=True)
        path = self.output / "events.jsonl"
        rows = (
            [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            if path.exists()
            else []
        )
        seen = {row["event_id"] for row in rows}
        new = [row for row in delta.events if row["event_id"] not in seen]
        with path.open("a", encoding="utf-8") as stream:
            for row in new:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        rows.extend(new)
        lines = ["# Web V2 验收增量", ""]
        for row in rows:
            lines.extend(
                [
                    f"## {row['kind']} / {row['event_id'][:12]}",
                    "",
                    "```json",
                    json.dumps(row, ensure_ascii=False, sort_keys=True),
                    "```",
                    "",
                ]
            )
        (self.output / "report.md").write_text("\n".join(lines), encoding="utf-8")
        (self.output / "site-summary.json").write_text(
            json.dumps(delta.summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.output / "cursor.json").write_text(json.dumps(delta.next_cursor), encoding="utf-8")


def dispatch_capacity(worker_count: int, *, active_count: int) -> int:
    if worker_count < 1 or active_count < 0:
        raise ValueError("需要至少一个可用 Worker，活动数量不得为负")
    return max(0, min(worker_count, 3) - active_count)


def extend_observation(state: dict, *, minutes: float):
    if state.get("extension_count", 0) or not 0 < minutes <= 30:
        raise ValueError("观察窗口最多延长一次且不超过30分钟")
    state.update(extension_count=1, deadline=state["deadline"] + minutes * 60)
    state.pop("observation_timeout", None)
