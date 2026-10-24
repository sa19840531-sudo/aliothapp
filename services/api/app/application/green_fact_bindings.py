"""从服务器冻结绑定和已核验原件提取共享事实，不读取模型提交的事实声明。"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO

from services.api.app.application.green_evidence_contracts import GreenEvidenceSet

_FIELDS = {
    "temporal": {"temporal_trace"},
    "navigation": {"ui_snapshot", "temporal_trace"},
    "layout_frames": {"ui_snapshot", "temporal_trace"},
    "layout_window": {"temporal_trace"},
    "repeat_actions": {"action_trace", "temporal_trace"},
    "response_actions": {"action_trace"},
    "repeat_safety": {"action_trace", "temporal_trace"},
    "external_link_action": {"action_trace"},
    "handoff_window": {"temporal_trace", "action_trace"},
    "handoff_observations": {"temporal_trace", "action_trace"},
    "content_scope": {"ui_snapshot", "temporal_trace"},
}


@dataclass(frozen=True)
class GreenFactView:
    """一个业务对象的内部原始事实；只有正式读取器能提供运行时绑定。"""

    subject_id: str
    issues: tuple[str, ...]
    values: Mapping[str, object] = field(default_factory=dict)
    capture_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()

    def get(self, name, default=None):
        return self.values.get(name, default)


def validate_green_shared_bindings(config):
    """绑定属于服务器冻结规则配置；原件自身不能新增规则或改变读取路径。"""
    if config is None:
        return None
    if (
        not isinstance(config, Mapping)
        or set(config) != {"schema_version", "subjects"}
        or config["schema_version"] != "green-shared-bindings.v1"
        or not isinstance(config["subjects"], list)
        or not 1 <= len(config["subjects"]) <= 32
    ):
        raise ValueError("共享事实绑定合同不完整或版本不受支持")
    seen = set()
    for subject in config["subjects"]:
        if not isinstance(subject, Mapping) or set(subject) != {"subject_id", "fields"}:
            raise ValueError("共享事实对象绑定字段无效")
        identity, fields = subject["subject_id"], subject["fields"]
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise ValueError("共享事实对象身份为空或重复")
        seen.add(identity)
        if not isinstance(fields, Mapping) or not fields or set(fields) - set(_FIELDS):
            raise ValueError("共享事实含未声明分项或为空")
        for name, binding in fields.items():
            if not isinstance(binding, Mapping) or set(binding) != {
                "artifact_type",
                "capture_role",
                "schema_version",
                "path",
            }:
                raise ValueError("共享事实来源必须声明类型、角色、原生版本和精确路径")
            if (
                not isinstance(binding["artifact_type"], str)
                or binding["artifact_type"] not in _FIELDS[name]
            ):
                raise ValueError("共享事实来源类型与分项不匹配")
            if not all(
                isinstance(binding[key], str) and binding[key].strip()
                for key in ("capture_role", "schema_version")
            ):
                raise ValueError("共享事实来源角色或版本缺失")
            path = binding["path"]
            if (
                not isinstance(path, list)
                or len(path) > 12
                or any(not isinstance(key, str) or not key for key in path)
            ):
                raise ValueError("共享事实只接受有界对象路径，不执行表达式或网页代码")
    return copy.deepcopy(dict(config))


def read_green_fact_views(evidence, frozen_config, *, target_platform=None):
    """从正式TASK原件消费绑定；缺绑定、缺成员或来源歧义不能借模型补造。"""
    if not isinstance(evidence, GreenEvidenceSet) or not evidence.verified_read:
        raise ValueError("共享事实只接受已核验的正式过程原件")
    sources = tuple(
        (
            capture.capture_id,
            capture.role,
            original.artifact_id,
            original.artifact_type,
            original.payload,
        )
        for capture in evidence.captures
        for original in capture.originals
    )
    formal_actions, action_issues = {}, []
    for action in evidence.actions:
        try:
            intent = json.loads(action.intent_payload)
            if not isinstance(intent, Mapping):
                raise ValueError("正式动作意图必须为对象")
            formal_actions[action.action_id] = {
                **intent,
                "_source_capture_id": action.source_capture_id,
                "_result_capture_id": action.result_capture_id,
                "_session_id": action.session_id,
                "_action_type": intent.get("action_type"),
                "_command_sequence": action.command_sequence,
                "_started_at": _iso_timestamp(action.started_at),
                "_finished_at": _iso_timestamp(action.finished_at),
            }
        except (UnicodeError, ValueError, TypeError, AttributeError, RecursionError):
            action_issues.append("green_formal_action_payload_invalid")
    return bind_green_fact_views(
        sources,
        frozen_config,
        issues=(*evidence.issues, *action_issues),
        action_ids=tuple(action.action_id for action in evidence.actions),
        target_platform=evidence.target_platform if target_platform is None else target_platform,
        formal_actions=formal_actions,
    )


def bind_green_fact_views(
    sources,
    frozen_config,
    *,
    issues=(),
    action_ids=(),
    target_platform=None,
    formal_actions=None,
):
    """sources只能由共享原件读取端口装配；不作为请求字段或模型Tool暴露。"""
    config = validate_green_shared_bindings(frozen_config)
    if config is None:
        return ()
    sources, source_issues = _safe_sources(sources)
    issues = (*issues, *source_issues)
    if not isinstance(formal_actions, Mapping):
        formal_actions = {}
    if not isinstance(action_ids, (list, tuple)) or not all(
        isinstance(identity, str) and identity for identity in action_ids
    ):
        action_ids = ()
        issues = (*issues, "green_formal_action_ids_invalid")
    decoded = {}
    for _capture, _role, artifact, kind, payload in sources:
        if kind in {"screenshot", "screenshot_png"}:
            continue
        try:
            decoded[artifact] = json.loads(payload)
        except (UnicodeError, ValueError, TypeError, RecursionError):
            decoded[artifact] = None
    views = []
    for subject in config["subjects"]:
        values, selected, gaps = {}, set(), list(issues)
        for name, binding in subject["fields"].items():
            matches = []
            for capture, role, artifact, kind, _payload in sources:
                if kind != binding["artifact_type"] or role != binding["capture_role"]:
                    continue
                payload = decoded.get(artifact)
                if not isinstance(payload, Mapping):
                    gaps.append("green_fact_original_unreadable:" + name)
                    continue
                if payload.get("schema_version") != binding["schema_version"]:
                    continue
                value = payload
                for key in binding["path"]:
                    value = value.get(key) if isinstance(value, Mapping) else None
                if value is not None:
                    matches.append((capture, artifact, value))
            if len(matches) != 1:
                gaps.append("green_fact_source_missing_or_ambiguous:" + name)
                continue
            capture, artifact, value = matches[0]
            try:
                values[name] = copy.deepcopy(value)
            except (TypeError, ValueError, RecursionError):
                gaps.append("green_fact_value_not_readable:" + name)
                continue
            selected.add(capture)
        allowed_captures = {capture for capture, *_ in sources}
        allowed_artifacts = {
            artifact: capture for capture, _role, artifact, _kind, _data in sources
        }
        action_captures = {
            value
            for action in (formal_actions or {}).values()
            for key in ("_source_capture_id", "_result_capture_id")
            if isinstance(action, Mapping) and isinstance(value := action.get(key), str)
        }
        try:
            references = tuple(_references(values))
        except ValueError:
            references = ()
            gaps.append("green_fact_nesting_limit_exceeded")
        for name, reference in references:
            if name == "capture":
                if reference not in allowed_captures:
                    gaps.append("green_fact_capture_outside_input")
                elif reference in action_captures:
                    selected.add(reference)
            elif name == "artifact":
                if reference not in allowed_artifacts:
                    gaps.append("green_fact_artifact_outside_input")
            elif reference not in action_ids:
                gaps.append("green_fact_action_outside_formal_ledger")
        _validate_action_expectations(values, formal_actions, gaps)
        captures = tuple(dict.fromkeys(capture for capture, *_ in sources if capture in selected))
        artifacts = tuple(
            dict.fromkeys(
                artifact
                for capture, _role, artifact, _kind, _data in sources
                if capture in selected
            )
        )
        values["png_artifact_ids"] = tuple(
            artifact
            for capture, _role, artifact, kind, data in sources
            if capture in selected and kind in {"screenshot", "screenshot_png"} and _is_png(data)
        )
        values["artifact_capture_ids"] = {
            artifact: capture
            for artifact, capture in allowed_artifacts.items()
            if capture in selected
        }
        values["capture_roles"] = {
            capture: role for capture, role, *_ in sources if capture in selected
        }
        values["png_capture_ids"] = {
            artifact: allowed_artifacts[artifact] for artifact in values["png_artifact_ids"]
        }
        values["presented_frame_events"] = tuple(
            {**decoded[artifact], "event_artifact_id": artifact}
            for capture, _role, artifact, kind, _data in sources
            if kind == "temporal_trace"
            and capture in selected
            and isinstance(decoded.get(artifact), Mapping)
            and decoded[artifact].get("schema_version") == "native-presented-frame.v1"
            and isinstance(decoded[artifact].get("source"), str)
            and decoded[artifact].get("source")
            in {
                "browser_video_frame_callback",
                "android_surface_frame",
            }
            and decoded[artifact].get("capture_id") == capture
            and isinstance(decoded[artifact].get("artifact_id"), str)
            and values["png_capture_ids"].get(decoded[artifact].get("artifact_id")) == capture
        )
        values["target_platform"] = target_platform
        views.append(
            GreenFactView(
                subject["subject_id"],
                tuple(dict.fromkeys(gaps)),
                values,
                captures,
                tuple(action_ids),
                artifacts,
            )
        )
    return tuple(views)


def _references(value, depth=0):
    if depth > 40:
        raise ValueError("共享事实嵌套超出读取边界")
    if isinstance(value, Mapping):
        for name, child in value.items():
            if not isinstance(name, str):
                raise ValueError("共享事实键必须为字符串")
            if isinstance(child, str):
                if name.endswith("capture_id"):
                    yield "capture", child
                elif name.endswith("artifact_id"):
                    yield "artifact", child
                elif name.endswith("action_id"):
                    yield "action", child
            yield from _references(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child, depth + 1)


def _validate_action_expectations(values, formal_actions, gaps):
    for name in ("response_actions", "repeat_actions"):
        if name not in values:
            continue
        actions = values[name]
        if (
            not isinstance(actions, list)
            or not actions
            or not all(isinstance(action, Mapping) for action in actions)
        ):
            gaps.append("green_fact_action_shape_invalid:" + name)
            continue
        for action in actions:
            formal = _validate_action_identity(action, formal_actions, gaps)
            parameters = formal.get("parameters") if formal is not None else None
            if "expected_response" in action and (
                not isinstance(parameters, Mapping)
                or parameters.get("expected_response") != action["expected_response"]
            ):
                gaps.append("green_expected_response_not_in_formal_intent")
            if "reset" in action:
                reset = action["reset"]
                if not isinstance(reset, Mapping):
                    gaps.append("green_reset_action_shape_invalid")
                else:
                    _validate_action_identity(reset, formal_actions, gaps)
    action = values.get("external_link_action")
    if isinstance(action, Mapping):
        formal = _validate_action_identity(action, formal_actions, gaps)
        parameters = formal.get("parameters") if formal is not None else None
        if not isinstance(parameters, Mapping) or parameters.get("target_url") != action.get(
            "target_url"
        ):
            gaps.append("green_external_target_not_in_formal_intent")
    elif "external_link_action" in values:
        gaps.append("green_external_action_shape_invalid")


def _validate_action_identity(record, formal_actions, gaps):
    identity = record.get("action_id")
    formal = formal_actions.get(identity) if isinstance(identity, str) else None
    if not isinstance(formal, Mapping):
        gaps.append("green_action_missing_from_formal_ledger")
        return None
    comparisons = (
        ("session_id", "_session_id"),
        ("operation", "_action_type"),
        ("command_sequence", "_command_sequence"),
        ("before_capture_id", "_source_capture_id"),
        ("after_capture_id", "_result_capture_id"),
    )
    if any(
        record.get(name) is None or record.get(name) != formal.get(source)
        for name, source in comparisons
    ):
        gaps.append("green_action_identity_not_in_formal_ledger")
    if (
        not all(
            isinstance(formal.get(key), str) and formal[key]
            for key in (
                "_session_id",
                "_action_type",
                "_source_capture_id",
                "_result_capture_id",
            )
        )
        or formal.get("_source_capture_id") == formal.get("_result_capture_id")
        or formal.get("session_id") != formal.get("_session_id")
        or formal.get("action_type") != formal.get("_action_type")
        or type(formal.get("_command_sequence")) is not int
        or formal["_command_sequence"] <= 0
        or type(record.get("command_sequence")) is not int
    ):
        gaps.append("green_formal_action_metadata_invalid")
    started, finished = (
        _aware_timestamp(formal.get("_started_at")),
        _aware_timestamp(formal.get("_finished_at")),
    )
    if (
        started is None
        or finished is None
        or started > finished
        or _aware_timestamp(record.get("formal_started_at")) != started
        or _aware_timestamp(record.get("formal_finished_at")) != finished
    ):
        gaps.append("green_action_formal_timestamp_mismatch")
    return formal


def _aware_timestamp(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return parsed if parsed.utcoffset() is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _iso_timestamp(value):
    parsed = _aware_timestamp(value)
    if parsed is None:
        raise ValueError("正式动作时间缺少时区")
    return parsed.isoformat()


def _safe_sources(sources):
    valid, gaps, seen, duplicate = [], [], set(), set()
    if not isinstance(sources, (list, tuple)):
        return (), ("green_fact_sources_shape_invalid",)
    for row in sources:
        if (
            not isinstance(row, (list, tuple))
            or len(row) != 5
            or not all(isinstance(value, str) and value for value in row[:4])
            or not isinstance(row[4], (bytes, str))
        ):
            gaps.append("green_fact_source_identity_invalid")
            continue
        if row[2] in seen:
            duplicate.add(row[2])
            gaps.append("green_fact_duplicate_artifact_identity")
        seen.add(row[2])
        valid.append(tuple(row))
    return tuple(row for row in valid if row[2] not in duplicate), tuple(dict.fromkeys(gaps))


def _is_png(data):
    if not isinstance(data, bytes) or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or min(image.size) <= 0:
                return False
            image.verify()
        return True
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return False
