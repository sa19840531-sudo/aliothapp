"""E26：敏感字段泄露与载荷上限测试。

验收：登录凭证、Cookie、令牌、原始截图 base64、完整 DOM
不进入清单元数据与判定审计载荷。
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime

import pytest

from services.api.app.application.evidence_manifest_writer import (
    build_pending_page_capture_manifest,
)
from services.api.app.application.evidence_payload_safety import (
    MAX_AUDIT_PAYLOAD_BYTES,
    MAX_AUDIT_STRING_CHARACTERS,
    REDACTED,
    is_bulk_content_key,
    is_sensitive_key,
    redact_audit_payload,
)
from services.api.app.application.manifest_backed_observation import (
    ManifestArtifactReadRecord,
    ManifestReadDecision,
    ManifestReadOutcome,
)
from services.api.app.application.manifest_read_guard import (
    InMemoryManifestReadAlertSink,
    ManifestReadGuard,
)
from services.api.app.domain.evidence import EvidenceQualityStatus

NOW = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "surface-session-1"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"

# 各类敏感样本，作为"绝不允许出现在投影里"的探针
COOKIE = "sessionid=abc123; HttpOnly"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.super-secret-token"
PASSWORD = "P@ssw0rd-should-never-leak"
SCREENSHOT_B64 = base64.b64encode(b"\x89PNG" + b"x" * 4000).decode()
FULL_DOM = "<html><body>" + "<div>x</div>" * 2000 + "</body></html>"

_PROBES = (COOKIE, TOKEN, PASSWORD, SCREENSHOT_B64, FULL_DOM)


def _assert_no_leak(payload: object) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    for probe in _PROBES:
        assert probe not in serialized, f"敏感样本泄露：{probe[:40]}"


# ---------- 键名识别 ----------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passwd",
        "user_password",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "cookies",
        "set_cookie",
        "api_key",
        "private_key",
        "credential",
        "bearer",
        "jwt",
    ],
)
def test_sensitive_keys_are_recognized(key: str) -> None:
    assert is_sensitive_key(key) is True


@pytest.mark.parametrize("key", ["author", "tokenizer", "page_type", "capture_role"])
def test_ordinary_keys_are_not_over_redacted(key: str) -> None:
    """避免误伤：author/tokenizer 这类不是凭证。"""
    assert is_sensitive_key(key) is False


@pytest.mark.parametrize(
    "key", ["dom", "html", "page_source", "screenshot_base64", "image_base64"]
)
def test_bulk_content_keys_are_recognized(key: str) -> None:
    assert is_bulk_content_key(key) is True


# ---------- 脱敏投影 ----------


def test_credentials_are_redacted_not_dropped() -> None:
    payload = redact_audit_payload(
        {"authorization": TOKEN, "cookie": COOKIE, "password": PASSWORD}
    )

    assert payload == {
        "authorization": REDACTED,
        "cookie": REDACTED,
        "password": REDACTED,
    }
    _assert_no_leak(payload)


def test_bulk_content_keys_are_dropped_entirely() -> None:
    payload = redact_audit_payload(
        {"dom": FULL_DOM, "screenshot_base64": SCREENSHOT_B64, "page_type": "home"}
    )

    assert payload == {"page_type": "home"}
    _assert_no_leak(payload)


def test_oversized_string_under_innocent_key_is_truncated() -> None:
    """兜底：原文藏在自定义键下，键名黑名单挡不住，靠长度截断。"""
    payload = redact_audit_payload({"note": FULL_DOM})

    assert payload["note"].startswith("[oversized_text:len=")
    assert str(len(FULL_DOM)) in payload["note"]
    _assert_no_leak(payload)


def test_base64_screenshot_under_innocent_key_is_truncated() -> None:
    payload = redact_audit_payload({"blob": SCREENSHOT_B64})

    assert payload["blob"].startswith("[oversized_text:len=")
    _assert_no_leak(payload)


def test_data_uri_is_compacted() -> None:
    payload = redact_audit_payload({"icon": f"data:image/png;base64,{SCREENSHOT_B64}"})

    assert payload["icon"].startswith("[data_uri:len=")
    _assert_no_leak(payload)


def test_nested_structures_are_redacted() -> None:
    payload = redact_audit_payload(
        {
            "request": {
                "headers": {"Cookie": COOKIE, "Authorization": TOKEN},
                "items": [{"api_key": "ak-live-123"}, {"dom": FULL_DOM}],
            }
        }
    )

    headers = payload["request"]["headers"]
    assert headers["Cookie"] == REDACTED
    assert headers["Authorization"] == REDACTED
    assert payload["request"]["items"][0]["api_key"] == REDACTED
    assert payload["request"]["items"][1] == {}
    _assert_no_leak(payload)


def test_short_strings_pass_through_unchanged() -> None:
    payload = redact_audit_payload({"page_type": "home", "url": "https://example.com"})

    assert payload == {"page_type": "home", "url": "https://example.com"}


def test_payload_over_budget_collapses_to_summary() -> None:
    """载荷上限：超限只留键名与体积，不留内容。"""
    payload = redact_audit_payload(
        {f"key_{index}": "x" * 400 for index in range(200)}
    )

    assert payload["audit_payload_truncated"] is True
    assert payload["byte_count"] > MAX_AUDIT_PAYLOAD_BYTES
    assert payload["maximum_bytes"] == MAX_AUDIT_PAYLOAD_BYTES
    assert len(payload["keys"]) == 200


def test_string_limit_boundary() -> None:
    exactly = "x" * MAX_AUDIT_STRING_CHARACTERS
    over = "x" * (MAX_AUDIT_STRING_CHARACTERS + 1)

    assert redact_audit_payload({"a": exactly})["a"] == exactly
    assert redact_audit_payload({"a": over})["a"].startswith("[oversized_text:")


def test_deeply_nested_structure_is_bounded() -> None:
    value: object = "leaf"
    for _ in range(30):
        value = {"next": value}

    payload = redact_audit_payload(value)

    assert "[depth_limit]" in json.dumps(payload, ensure_ascii=False)


# ---------- 清单元数据 ----------


class _StubArtifact:
    def __init__(self, artifact_id: str) -> None:
        payload = b"evidence-bytes"
        self.artifact_id = artifact_id
        self.artifact_type = "screenshot"
        self.storage_path = f"{TASK_ID}/{CAPTURE_ID}/{artifact_id}.png"
        self.checksum = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        self.size_bytes = len(payload)
        self.content_type = "image/png"
        # 附件行的 metadata 里带着敏感信息：清单绝不能把它抄进去
        self.metadata = {
            "cookie": COOKIE,
            "authorization": TOKEN,
            "screenshot_base64": SCREENSHOT_B64,
            "dom": FULL_DOM,
        }


def test_manifest_metadata_never_carries_artifact_secrets() -> None:
    """E26 验收：清单元数据不含凭证、Cookie、令牌、截图 base64 与完整 DOM。"""
    manifest = build_pending_page_capture_manifest(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        capture_role="primary",
        artifacts=(_StubArtifact("artifact-1"), _StubArtifact("artifact-2")),
        created_at=NOW,
    )

    # 清单只搬运可校验身份，不搬运附件 metadata
    assert all(item.metadata == {} for item in manifest.artifacts)
    _assert_no_leak(manifest.to_dict())


def test_manifest_payload_only_contains_verifiable_identity() -> None:
    manifest = build_pending_page_capture_manifest(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        capture_role="primary",
        artifacts=(_StubArtifact("artifact-1"),),
        created_at=NOW,
    )

    artifact = manifest.to_dict()["artifacts"][0]

    assert set(artifact) == {
        "protocol_version",
        "artifact_id",
        "artifact_type",
        "role",
        "storage_path",
        "checksum",
        "size_bytes",
        "content_type",
        "metadata",
    }
    assert artifact["metadata"] == {}


# ---------- 判定审计载荷 ----------


def _decision(outcome: ManifestReadOutcome) -> ManifestReadDecision:
    return ManifestReadDecision(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        outcome=outcome,
        read_at=NOW,
        manifest_id="evidence-manifest:abc",
        manifest_version=2,
        manifest_hash="hash-abc",
        quality_status=EvidenceQualityStatus.PASS,
        artifacts=(
            ManifestArtifactReadRecord(
                artifact_id="artifact-1",
                artifact_type="screenshot",
                role="primary",
                checksum="sha256:" + "a" * 64,
                size_bytes=4096,
                verified=True,
            ),
        ),
        reason=None
        if outcome is ManifestReadOutcome.VERIFIED
        else "证据与清单校验和不一致",
    )


def test_read_decision_audit_payload_has_no_evidence_content() -> None:
    payload = _decision(ManifestReadOutcome.VERIFIED).to_audit_payload()

    _assert_no_leak(payload)
    assert "payload" not in json.dumps(payload, ensure_ascii=False)


def test_rejection_audit_payload_has_no_evidence_content() -> None:
    guard = ManifestReadGuard(
        alert_sink=InMemoryManifestReadAlertSink(), now=lambda: NOW
    )

    rejection = guard.evaluate(_decision(ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED))

    assert rejection is not None
    _assert_no_leak(rejection.to_audit_payload())
    _assert_no_leak(rejection.to_judge_output())


def test_integrity_alert_carries_no_evidence_content() -> None:
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    guard.evaluate(_decision(ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED))

    assert len(sink.alerts) == 1
    _assert_no_leak(sink.alerts[0].to_audit_payload())


def test_judge_input_summary_scrubs_caller_supplied_secrets() -> None:
    """judge_input 由调用方自由构造并原样落库，必须先脱敏。"""
    from services.api.app.application.rule_judge import _summarize_judge_input

    summarized = _summarize_judge_input(
        {
            "visible_text": "页面文案",
            "authorization": TOKEN,
            "cookie": COOKIE,
            "dom": FULL_DOM,
            "screenshot_base64": SCREENSHOT_B64,
            "custom_blob": FULL_DOM,
        }
    )

    assert summarized["authorization"] == REDACTED
    assert summarized["cookie"] == REDACTED
    assert "dom" not in summarized
    assert "screenshot_base64" not in summarized
    assert summarized["custom_blob"].startswith("[oversized_text:")
    assert summarized["visible_text"] == "页面文案"
    _assert_no_leak(summarized)


def test_candidate_input_scrubs_secrets_in_packet_payload() -> None:
    """candidate_input 会随 packet 进模型提示词，必须同样脱敏。"""
    from services.api.app.application.observation_packet import _sanitize_agent_input

    sanitized = _sanitize_agent_input(
        {
            "target": "商品详情",
            "authorization": TOKEN,
            "cookie": COOKIE,
            "dom": FULL_DOM,
            "note": FULL_DOM,
        }
    )

    assert sanitized["target"] == "商品详情"
    assert sanitized["authorization"] == REDACTED
    assert sanitized["cookie"] == REDACTED
    assert "dom" not in sanitized
    _assert_no_leak(sanitized)
