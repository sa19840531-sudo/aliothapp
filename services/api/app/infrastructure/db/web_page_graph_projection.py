"""Web V2 图输入认证；复用冻结身份读取，不让共享 URL 规则重编码业务键。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from urllib.parse import urlsplit

from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
from services.api.app.application.web_readiness_contracts import read_web_readiness_assessment
from services.api.app.domain.evidence.presentation import (
    PRESENTATION_METADATA_KEY,
    PresentationAssessment,
    TopmostLayerKind,
)
from services.api.app.domain.exploration.page_graph import (
    WEB_URL_AFTER_PROTOCOL_VERSION,
    PageIdentity,
    WebPageGraphFacts,
    canonical_url_identity,
)
from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicyError
from services.api.app.domain.page.web_site_menu_registry import _target_matches_site
from services.api.app.infrastructure.db.web_capture_identity import read_web_capture_identities
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)


async def read_web_page_graph_facts(
    connection, *, task_id, capture_id, agent_run_id, session_id, surface_kind
):
    """None 仅表示非 Web 任务；Web 认证失败必须返回显式 unknown，禁止旧规则兜底。"""
    if surface_kind != "web":
        return None
    row = await connection.fetchrow(
        """
        SELECT task.target_platform, task.target_url,
               task.task_config->>'workflow_version' AS workflow_version,
               capture.surface_session_id, capture.capture_role, capture.capture_status,
               capture.agent_run_id::text AS agent_run_id, capture.metadata,
               page.url, page.page_state, page.page_type, page.detection_status,
               page.detection_confidence, page.metadata AS page_metadata,
               session.session_owner, session.session_id, session.finalization_epoch,
               run.agent_type
        FROM qc_task task
        LEFT JOIN qc_capture capture ON capture.task_id=task.id AND capture.id=$2::uuid
        LEFT JOIN qc_page_capture page ON page.id=capture.id AND page.task_id=task.id
          AND page.agent_run_id=capture.agent_run_id
        LEFT JOIN qc_surface_session session ON session.task_id=task.id
          AND session.session_id=capture.surface_session_id AND session.surface_kind='web'
        LEFT JOIN qc_agent_run run ON run.id=capture.agent_run_id AND run.task_id=task.id
        WHERE task.id=$1::uuid
        """,
        task_id,
        capture_id,
    )
    if row is None or row["target_platform"] != "web":
        return None
    facts = WebPageGraphFacts(
        identity=PageIdentity(key=f"unknown:{capture_id}", status="unknown", source="unconfirmed"),
        page_state=row["page_state"],
        business_visible=False,
        is_overlay=False,
        is_external=False,
        audit={"reason_code": "WEB_GRAPH_SCOPE_UNTRUSTED"},
    )
    metadata, page_metadata = _mapping(row["metadata"]), _mapping(row["page_metadata"])
    if (
        row["workflow_version"] != "v2"
        or row["agent_type"] != "main"
        or row["agent_run_id"] != agent_run_id
        or row["session_owner"] != "web-v2-workflow"
        or row["session_id"] != session_id
        or session_id != f"web:{agent_run_id}"
        or row["capture_status"] != "captured"
        or str(metadata.get("finalization_epoch")) != str(row["finalization_epoch"])
        or page_metadata.get("finalization_epoch") != metadata.get("finalization_epoch")
    ):
        return facts
    try:
        snapshot = await read_web_policy_on_connection(connection, task_id, agent_run_id)
        if snapshot is None:
            raise WebRunPolicyError("WEB_POLICY_HISTORY_MISSING")
        policy = LegacyWebRunAuditAdapter.load(snapshot)
        identities = await read_web_capture_identities(
            connection, task_id=task_id, agent_run_id=agent_run_id
        )
    except WebRunPolicyError as error:
        return replace(facts, audit={"reason_code": str(error)})
    readiness, overlay, external, business_visible = _visible_context(metadata, policy)
    # 外域标记缺失不是同源证明；复用既有站点范围规则，保留合法 HTTPS 升级。
    external = external or not _same_web_site(row["url"], row["target_url"], policy)
    business_visible = business_visible and not external
    identity = identities.get(capture_id, {})
    audit = {
        "identity_policy_version": policy.identity_policy.version,
        "policy_sha256": snapshot.payload_sha256,
        "logical_page_id": identity.get("logical_page_id"),
        "canonical_url": identity.get("canonical_url"),
        "reason_code": identity.get("logical_page_identity_status", "WEB_LOGICAL_ID_UNTRUSTED"),
    }
    facts = replace(
        facts,
        page_state=readiness.status.value if readiness is not None else "unknown",
        is_overlay=overlay,
        is_external=external,
        audit=audit,
    )
    if not business_visible:
        return facts
    marker = await _verified_after_marker(
        connection,
        task_id=task_id,
        agent_run_id=agent_run_id,
        session_id=session_id,
        capture_id=capture_id,
        epoch=row["finalization_epoch"],
        policy=policy,
        identities=identities,
    )
    logical_page_id = identity.get("logical_page_id")
    if identity.get("logical_page_identity_status") != "verified":
        # 新鲜 before 不复制父页分类或 ID；仅采用完整动作父链签发的图内派生身份。
        if (
            marker is None
            or identity.get("logical_page_identity_status") != "WEB_LOGICAL_ID_UNTRUSTED"
        ):
            return facts
        logical_page_id = marker["logical_page_id"]
    confidence = row["detection_confidence"]
    recognized = (
        row["detection_status"] == "recognized"
        and confidence is not None
        and 0.70 <= confidence <= 1.0
    )
    if not recognized and marker is None:
        return facts
    return replace(
        facts,
        identity=PageIdentity(
            key=logical_page_id,
            status="confirmed",
            source="trusted_url_after" if marker else "trusted_url_observation",
        ),
        business_visible=True,
        after_marker=marker,
        audit={
            **audit,
            "logical_page_id": logical_page_id,
            "reason_code": (
                "verified"
                if identity.get("logical_page_identity_status") == "verified"
                else "WEB_GRAPH_IDENTITY_FROM_ACTION_PARENT"
            ),
        },
    )


async def _verified_after_marker(
    connection, *, task_id, agent_run_id, session_id, capture_id, epoch, policy, identities
):
    """按严格递增命令单次传播图内证明，不查旧图、不递归、不补写 Capture。"""
    rows = await connection.fetch(
        """
        SELECT action.command_sequence,
               action.intent_payload->>'action_type' AS action_type,
               action.result_payload AS result_payload,
               source.id::text AS source_capture_id, source.metadata AS source_metadata,
               source_page.url AS source_url, source_page.metadata AS source_page_metadata,
               target.id::text AS target_capture_id, target.metadata AS target_metadata,
               target_page.url AS target_url, target_page.metadata AS target_page_metadata,
               requested.id::text AS requested_capture_id, requested.metadata AS requested_metadata,
               requested_page.url AS requested_url
        FROM qc_action_execution action
        JOIN qc_capture source ON source.id=action.source_capture_id
          AND source.task_id=action.task_id AND source.agent_run_id=$2::uuid
          AND source.surface_session_id=action.session_id AND source.capture_status='captured'
          AND source.action_execution_id=action.action_execution_id
          AND source.session_command_sequence=action.command_sequence
          AND source.capture_role='before_action' AND source.surface_kind='web'
        JOIN qc_page_capture source_page ON source_page.id=source.id
          AND source_page.task_id=source.task_id AND source_page.agent_run_id=source.agent_run_id
          AND source_page.surface_session_id=source.surface_session_id
          AND source_page.capture_status='captured'
          AND source_page.metadata->>'finalization_epoch'=$5::integer::text
        JOIN qc_capture target ON target.id=action.result_capture_id
          AND target.task_id=action.task_id AND target.agent_run_id=source.agent_run_id
          AND target.surface_session_id=source.surface_session_id
          AND target.capture_status='captured'
          AND target.parent_capture_id=source.id AND target.capture_role='after_action'
          AND target.action_execution_id=action.action_execution_id
          AND target.session_command_sequence=action.command_sequence
          AND target.surface_kind='web'
        JOIN qc_page_capture target_page ON target_page.id=target.id
          AND target_page.task_id=target.task_id AND target_page.agent_run_id=target.agent_run_id
          AND target_page.parent_page_capture_id=source.id
          AND target_page.surface_session_id=target.surface_session_id
          AND target_page.capture_status='captured'
          AND target_page.metadata->>'finalization_epoch'=$5::integer::text
        JOIN qc_capture requested ON requested.id=action.requested_source_capture_id
          AND requested.task_id=source.task_id AND requested.agent_run_id=source.agent_run_id
          AND requested.surface_session_id=source.surface_session_id
          AND requested.surface_kind='web' AND requested.capture_status='captured'
          AND requested.metadata->>'finalization_epoch'=$5::integer::text
          AND source.parent_capture_id=requested.id
          AND source_page.parent_page_capture_id=requested.id
        JOIN qc_page_capture requested_page ON requested_page.id=requested.id
          AND requested_page.task_id=requested.task_id
          AND requested_page.agent_run_id=requested.agent_run_id
          AND requested_page.surface_session_id=requested.surface_session_id
          AND requested_page.capture_status='captured'
          AND requested_page.metadata->>'finalization_epoch'=$5::integer::text
        WHERE action.task_id=$1::uuid AND action.session_id=$3
          AND requested.session_command_sequence < action.command_sequence
          AND action.command_sequence <= (
              SELECT session_command_sequence FROM qc_capture
              WHERE id=$4::uuid AND task_id=$1::uuid AND agent_run_id=$2::uuid
                AND surface_session_id=$3)
          AND source.id<>target.id AND requested.id<>target.id
          AND action.finalization_epoch=$5
          AND source.metadata->>'finalization_epoch'=$5::text
          AND target.metadata->>'finalization_epoch'=$5::text
          AND action.status='completed'
          AND action.error_code IS NULL AND action.error_message IS NULL
        ORDER BY action.command_sequence, action.action_execution_id
        """,
        task_id,
        agent_run_id,
        session_id,
        capture_id,
        epoch,
    )
    proofs = {}
    for row in rows:
        if any(
            not _visible_context(_mapping(row[key]), policy)[3]
            for key in ("requested_metadata", "source_metadata", "target_metadata")
        ):
            continue
        requested_id = row["requested_capture_id"]
        requested = identities.get(requested_id, {})
        requested_key = (
            requested.get("logical_page_id")
            if requested.get("logical_page_identity_status") == "verified"
            else proofs.get(requested_id, {}).get("logical_page_id")
        )
        if requested_key is None:
            continue
        source_key = _capture_graph_key(row, "source", identities, policy)
        target_key = _capture_graph_key(row, "target", identities, policy)
        if source_key != requested_key or target_key is None:
            continue
        source_url, target_url = row["source_url"], row["target_url"]
        if not _same_web_site(target_url, source_url, policy):
            continue
        state_diff = _mapping(_mapping(row["result_payload"]).get("state_diff"))
        source_location = canonical_url_identity(source_url)
        target_location = canonical_url_identity(target_url)
        navigation = bool(
            row["action_type"] not in {None, "observe", "timed_capture"}
            and state_diff.get("location_changed") is True
            and source_location is not None
            and target_location is not None
            and source_location != target_location
        )
        observation = bool(
            row["action_type"] == "observe"
            and _same_page_observe_receipt(_mapping(row["result_payload"]))
            and source_location is not None
            and source_location == target_location == canonical_url_identity(row["requested_url"])
            and source_key == target_key
        )
        if not (navigation or observation):
            continue
        for prefix, key in (("source", source_key), ("target", target_key)):
            endpoint_id = row[f"{prefix}_capture_id"]
            proofs[endpoint_id] = {
                "protocol_version": WEB_URL_AFTER_PROTOCOL_VERSION,
                "verified": True,
                "verified_by": "action_after_capture",
                "capture_id": endpoint_id,
                "source_capture_id": row["source_capture_id"],
                "logical_page_id": key,
                "proof_kind": "navigation" if navigation else "same_page_observation",
            }
    return proofs.get(capture_id)


def _same_page_observe_receipt(result):
    """仅接纳成功 OBSERVE 的无导航回执；null 不是全局的 URL 未变证明。"""
    if (
        result.get("status") != "succeeded"
        or result.get("execution_outcome") != "succeeded"
        or result.get("error_code") is not None
        or result.get("error_message") is not None
        or result.get("retryable") is not False
    ):
        return False
    diff = _mapping(result.get("state_diff"))
    changed = diff.get("location_changed")
    if "location_changed" not in diff or (changed is not None and changed is not False):
        return False
    # 真实 collector 的空 action_trace 经公共映射产生全 null；有任何额外动作事实则关闭。
    return all(value is None for key, value in diff.items() if key != "location_changed")


def _capture_graph_key(row, prefix, identities, policy):
    """已有身份必须经 T5 认证；空白新帧只从冻结 URL 派生，冲突绝不覆盖。"""
    identity = identities.get(row[f"{prefix}_capture_id"], {})
    if identity.get("logical_page_identity_status") == "verified":
        return identity["logical_page_id"]
    if identity.get("logical_page_identity_status") != "WEB_LOGICAL_ID_UNTRUSTED" or any(
        values.get("logical_page_id") or values.get("identity_policy_version")
        for values in (
            _mapping(row[f"{prefix}_metadata"]),
            _mapping(row[f"{prefix}_page_metadata"]),
        )
    ):
        return None
    try:
        return policy.identity_policy.logical_id(row[f"{prefix}_url"])
    except ValueError:
        return None


def _same_web_site(target_url, source_url, policy):
    """先由冻结 URL 策略验证绝对地址，再沿用菜单领域的域、端口与升级边界。"""
    try:
        target = urlsplit(policy.identity_policy.canonical(target_url))
        source = urlsplit(policy.identity_policy.canonical(source_url))
    except ValueError:
        return False
    return _target_matches_site(target, source)


def _visible_context(metadata, policy):
    """稳定窗口、可见层和自然来源是独立门禁，采用既有 Presentation 解析。"""
    metrics = _mapping(metadata.get("pipeline_metrics"))
    raw = _mapping(metrics.get("web_readiness"))
    readiness = read_web_readiness_assessment(metrics, profile=policy.readiness_profile)
    overlay = bool(
        raw.get("blocking_layer")
        or metadata.get("is_overlay")
        or metadata.get("popup_visible")
        or metadata.get("overlay_kind")
    )
    external = bool(
        metrics.get("external_ad_landing")
        or metadata.get("is_external")
        or metadata.get("external_navigation")
    )
    diagnostic = bool(metrics.get("diagnostic_evidence") or metrics.get("diagnostic_capture"))
    presentation = PresentationAssessment.from_dict(metadata.get(PRESENTATION_METADATA_KEY))
    visible = True
    if PRESENTATION_METADATA_KEY in metadata:
        visible = presentation is not None and presentation.base_page_exposed
    if presentation is not None:
        overlay = overlay or presentation.occluding_layer_present
        external = external or presentation.topmost_layer_kind is TopmostLayerKind.EXTERNAL
        diagnostic = diagnostic or presentation.is_diagnostic
    business_visible = bool(
        readiness is not None
        and readiness.can_serve_evidence
        and visible
        and metadata.get("reachable") is True
        and not (overlay or external or diagnostic)
    )
    return readiness, overlay, external, business_visible


def decode_web_graph_payloads(states, transitions):
    """仅供实际 Web V2 任务读侧解码 JSONB；不改变 H5/App 原有返回协议。"""
    return (
        [{**row, "state_payload": _mapping(row.get("state_payload"))} for row in states],
        [{**row, "outcome_payload": _mapping(row.get("outcome_payload"))} for row in transitions],
    )


def _mapping(value):
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, Mapping) else {}
