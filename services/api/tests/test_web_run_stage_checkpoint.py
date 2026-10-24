"""Web 阶段推进、恢复身份和重试边界。"""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.domain.exploration.web_run_stage_checkpoint import (
    WebRunStage,
    WebStageCursor,
    WebStageGap,
    WebStageRetryPolicy,
    WebStageStatus,
)

NOW = datetime(2026, 9, 15, tzinfo=UTC)
RUN_ID = "50000000-0000-0000-0000-000000000114"


def start(stage=WebRunStage.BOOTSTRAP_NAVIGATION, **kwargs):
    return WebStageCursor.start(run_id=RUN_ID, logical_page_key="url:1", stage=stage, **kwargs)


def test_success_requires_claim_and_advances_only_to_successor():
    cursor = start()
    with pytest.raises(ValueError):
        cursor.mark_succeeded()
    claimed = cursor.claim(worker_id="worker-1", now=NOW)
    result = claimed.mark_succeeded()
    assert result.next_stage is WebRunStage.OBSTRUCTION_CLEAR
    with pytest.raises(ValueError):
        result.advance(WebRunStage.CLASSIFICATION)
    advanced = result.advance(WebRunStage.OBSTRUCTION_CLEAR)
    assert advanced.attempt_count == 0
    with pytest.raises(ValueError):
        advanced.advance(WebRunStage.BOOTSTRAP_NAVIGATION)
    with pytest.raises(FrozenInstanceError):
        cursor.attempt_count = 5


def test_transient_navigation_exhaustion_records_gap_and_advances():
    cursor = start()
    for _ in range(3):
        cursor = cursor.fail_transient("WEB_GOTO_TLS_FAILED", max_attempts=3)
    assert cursor.status is WebStageStatus.GAPPED
    assert cursor.gap_codes == ("WEB_NAVIGATION_RETRY_EXHAUSTED",)
    assert cursor.next_stage is WebRunStage.FRONTIER_DRAIN
    assert cursor.gaps[0].error_code == "WEB_GOTO_TLS_FAILED"


def test_retry_is_due_bounded_and_preserves_stage_identity():
    first = start().claim(worker_id="worker-1", now=NOW)
    retry = first.schedule_retry(error_code="WEB_GOTO_TLS_FAILED", now=NOW)
    assert retry.next_attempt_at == NOW + timedelta(seconds=1)
    assert retry.idempotency_key == first.idempotency_key
    with pytest.raises(ValueError):
        retry.claim(worker_id="worker-2", now=NOW)
    second = retry.claim(worker_id="worker-2", now=NOW + timedelta(seconds=1))
    assert second.attempt_count == 2
    assert second.claim_token != first.claim_token
    retry = second.schedule_retry(error_code="WEB_GOTO_TLS_FAILED", now=NOW)
    assert retry.next_attempt_at == NOW + timedelta(seconds=2)


@pytest.mark.parametrize(
    ("stage", "error", "attempts", "gap", "next_stage"),
    [
        (
            "obstruction_clear",
            "WEB_OBSTRUCTION_CLOSE_FAILED",
            2,
            "WEB_OBSTRUCTION_UNRESOLVED",
            "readiness",
        ),
        (
            "primary_capture",
            "WEB_CAPTURE_TRANSIENT_FAILED",
            2,
            "WEB_PRIMARY_CAPTURE_FAILED",
            "frontier_drain",
        ),
        (
            "readiness",
            "WEB_READINESS_TIMEOUT",
            2,
            "WEB_READINESS_TIMEOUT",
            "frontier_drain",
        ),
        (
            "full_page_capture",
            "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
            2,
            "WEB_FULL_PAGE_SCREENSHOT_FAILED",
            "classification",
        ),
        (
            "full_page_capture",
            "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT",
            1,
            "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT",
            "classification",
        ),
        (
            "classification",
            "WEB_CLASSIFICATION_EVIDENCE_CONFLICT",
            1,
            "WEB_CLASSIFICATION_EVIDENCE_CONFLICT",
            "frontier_drain",
        ),
        (
            "ad_isolated_navigation",
            "WEB_GOTO_TLS_FAILED",
            2,
            "WEB_AD_PAGE_OPEN_FAILED",
            "ad_closeout",
        ),
    ],
)
def test_retry_matrix_exhaustion(stage, error, attempts, gap, next_stage):
    cursor = start(WebRunStage(stage))
    for attempt in range(attempts):
        cursor = cursor.claim(worker_id="worker", now=NOW + timedelta(seconds=attempt * 10))
        cursor = cursor.schedule_retry(error_code=error, now=NOW)
    assert cursor.status is WebStageStatus.GAPPED
    assert cursor.gap_codes == (gap,)
    assert cursor.next_stage.value == next_stage


def test_permanent_failure_never_retries_and_keeps_gap_after_advance():
    cursor = start(WebRunStage.PRIMARY_CAPTURE).claim(worker_id="worker", now=NOW)
    cursor = cursor.schedule_retry(error_code="WEB_CAPTURE_FORBIDDEN", now=NOW, transient=False)
    assert cursor.next_attempt_at is None
    advanced = cursor.advance(WebRunStage.FRONTIER_DRAIN)
    assert advanced.gap_codes == ("WEB_PRIMARY_CAPTURE_FAILED",)
    assert advanced.gaps[0].attempt_count == 1


def test_ad_branch_terminates_without_classification_or_business_frontier():
    cursor = start(WebRunStage.AD_ISOLATED_NAVIGATION)
    for stage in (WebRunStage.AD_CAPTURE, WebRunStage.AD_CLOSEOUT):
        cursor = cursor.claim(worker_id="worker", now=NOW).mark_succeeded().advance(stage)
    cursor = cursor.claim(worker_id="worker", now=NOW).mark_succeeded()
    assert cursor.is_terminal
    assert cursor.next_stage is None
    with pytest.raises(ValueError):
        cursor.claim(worker_id="worker", now=NOW)


def test_running_lease_can_only_be_reclaimed_after_expiry():
    cursor = start().claim(worker_id="first", now=NOW, lease_seconds=10)
    with pytest.raises(ValueError):
        cursor.claim(worker_id="second", now=NOW + timedelta(seconds=9))
    recovered = cursor.claim(worker_id="second", now=NOW + timedelta(seconds=10))
    assert recovered.idempotency_key == cursor.idempotency_key
    assert recovered.claim_token != cursor.claim_token
    assert recovered.attempt_count == 2


def test_cancelled_cursor_and_invalid_policy_are_rejected():
    with pytest.raises(ValueError):
        replace(start(), status=WebStageStatus.CANCELLED).claim(worker_id="worker", now=NOW)
    with pytest.raises(ValueError):
        WebStageRetryPolicy(max_attempts=0)


@pytest.mark.parametrize(
    "payload",
    [
        {"dom": "<html>private</html>"},
        {"token": "secret"},
        {"current_url": "https://user:password@example.test/"},
        {"current_url": "https://example.test/?token=secret"},
        {"parent_capture_id": "<html>raw</html>"},
    ],
)
def test_checkpoint_rejects_raw_content_and_credentials(payload):
    with pytest.raises(ValueError):
        start(cursor_payload=payload)


def test_payload_is_deeply_immutable_and_artifacts_are_references():
    payload = {"current_url": "https://example.test/", "candidate_href_index": 2}
    cursor = start(cursor_payload=payload)
    payload["candidate_href_index"] = 9
    assert cursor.cursor_payload["candidate_href_index"] == 2
    with pytest.raises(TypeError):
        cursor.cursor_payload["candidate_href_index"] = 3
    with pytest.raises(ValueError):
        cursor.claim(worker_id="worker", now=NOW).mark_succeeded(artifact_ids=("<binary>",))


def test_login_policy_result_keeps_only_safe_origin_and_method():
    cursor = start(cursor_payload={
        "login_source_capture_id": "00000000-0000-0000-0000-000000000008",
        "login_branch_status": "authenticated",
        "login_branch_reason": "WEB_LOGIN_COOKIE_CONFIRMED",
        "login_cross_origin_status": "blocked",
        "login_blocked_requests": (("https://mc.yandex.ru", "GET"),),
        "login_account_status": "recharge_opened",
        "login_account_artifact_ids": ("artifact-1",),
        "login_account_storage_paths": ("task/capture/artifact-1.png",),
        "login_account_page_type": "recharge_page",
        "login_account_page_url": "https://example.test/recharge",
        "login_account_avatar_opened": True,
        "login_account_avatar_dom_artifact_id": "artifact-dom",
    })
    assert cursor.cursor_payload["login_blocked_requests"] == (
        ("https://mc.yandex.ru", "GET"),
    )
    assert cursor.cursor_payload["login_account_page_type"] == "recharge_page"
    assert cursor.cursor_payload["login_account_page_url"] == "https://example.test/recharge"
    assert cursor.cursor_payload["login_account_avatar_opened"] is True
    assert cursor.cursor_payload["login_account_avatar_dom_artifact_id"] == "artifact-dom"
    assert cursor.cursor_payload["login_source_capture_id"] == (
        "00000000-0000-0000-0000-000000000008"
    )
    assert len(start(cursor_payload={
        "login_account_artifact_ids": tuple(f"artifact-{i}" for i in range(7)),
    }).cursor_payload["login_account_artifact_ids"]) == 7
    assert len(start(cursor_payload={
        "login_account_artifact_ids": tuple(f"artifact-{i}" for i in range(24)),
    }).cursor_payload["login_account_artifact_ids"]) == 24
    with pytest.raises(ValueError):
        start(cursor_payload={
            "login_account_artifact_ids": tuple(f"artifact-{i}" for i in range(25)),
        })
    with pytest.raises(ValueError):
        start(cursor_payload={"login_account_avatar_opened": "true"})
    with pytest.raises(ValueError):
        start(cursor_payload={
            "login_account_page_url": "https://example.test/recharge?token=private",
        })
    ambiguous = start(cursor_payload={"login_account_status": "entry_ambiguous"})
    assert ambiguous.cursor_payload["login_account_status"] == "entry_ambiguous"
    guest = start(cursor_payload={"login_account_status": "guest_after_fresh_navigation"})
    assert guest.cursor_payload["login_account_status"] == "guest_after_fresh_navigation"
    unverified = start(cursor_payload={"login_account_status": "account_auth_unverified"})
    assert unverified.cursor_payload["login_account_status"] == "account_auth_unverified"
    missing_avatar = start(cursor_payload={"login_account_status": "avatar_missing"})
    assert missing_avatar.cursor_payload["login_account_status"] == "avatar_missing"
    obstructed_entry = start(cursor_payload={"login_account_status": "entry_obstructed"})
    assert obstructed_entry.cursor_payload["login_account_status"] == "entry_obstructed"
    blocked_http = start(cursor_payload={
        "login_blocked_requests": (("http://unknown-auth.test", "POST"),),
    })
    assert blocked_http.cursor_payload["login_blocked_requests"] == (
        ("http://unknown-auth.test", "POST"),
    )
    with pytest.raises(ValueError):
        start(cursor_payload={
            "login_blocked_requests": (("https://other.test/path?password=secret", "POST"),),
        })


def test_login_wallet_and_topup_visits_keep_both_evidence_references():
    visits = (
        ("recharge_page", "https://example.test/wallet", "wallet-shot", "wallet-dom"),
        ("topup_page", "https://example.test/deposit", "topup-shot", "topup-dom"),
    )
    cursor = start(cursor_payload={
        "login_account_status": "recharge_opened",
        "login_account_page_type": "topup_page",
        "login_account_page_url": "https://example.test/deposit",
        "login_account_page_visits": visits,
        "login_account_artifact_ids": tuple(f"artifact-{i}" for i in range(11)),
    })
    assert cursor.cursor_payload["login_account_page_visits"] == visits
    assert len(cursor.cursor_payload["login_account_artifact_ids"]) == 11
    with pytest.raises(ValueError):
        start(cursor_payload={"login_account_page_visits": (
            ("topup_page", "https://example.test/deposit?secret=1", "a", "b"),
        )})


def test_obstruction_retry_budget_is_per_fingerprint():
    cursor = start(WebRunStage.OBSTRUCTION_CLEAR, cursor_payload={"obstruction_fingerprint": "a"})
    cursor = cursor.claim(worker_id="worker", now=NOW).schedule_retry(
        error_code="WEB_OBSTRUCTION_CLOSE_FAILED", now=NOW
    )
    cursor = replace(cursor, cursor_payload={"obstruction_fingerprint": "b"})
    cursor = cursor.claim(worker_id="worker", now=NOW + timedelta(seconds=5)).schedule_retry(
        error_code="WEB_OBSTRUCTION_CLOSE_FAILED", now=NOW + timedelta(seconds=5)
    )
    assert cursor.status is WebStageStatus.RETRY_WAIT
    assert dict(cursor.obstruction_attempts) == {"a": 1, "b": 1}
    cursor = cursor.claim(worker_id="worker", now=NOW + timedelta(seconds=10)).schedule_retry(
        error_code="WEB_OBSTRUCTION_CLOSE_FAILED", now=NOW + timedelta(seconds=10)
    )
    assert cursor.gap_codes == ("WEB_OBSTRUCTION_UNRESOLVED",)


def test_full_business_path_finishes_and_preserves_artifact_references():
    cursor = start()
    artifact = "60000000-0000-0000-0000-000000000114"
    seen = []
    while not cursor.is_terminal:
        cursor = cursor.claim(worker_id="worker", now=NOW)
        seen.append(cursor.current_stage.value)
        cursor = cursor.mark_succeeded(artifact_ids=(artifact,))
    assert seen == [
        "bootstrap_navigation",
        "obstruction_clear",
        "readiness",
        "primary_capture",
        "full_page_capture",
        "classification",
        "href_discovery",
        "candidate_navigation",
        "frontier_drain",
        "evidence_reconciliation",
        "coverage_snapshot",
        "report_finalization",
        "session_close",
    ]
    assert cursor.artifact_ids == (artifact,)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": WebStageStatus.RUNNING},
        {"status": WebStageStatus.RETRY_WAIT},
        {"status": WebStageStatus.PENDING, "next_attempt_at": NOW},
    ],
)
def test_malformed_persisted_state_is_rejected(changes):
    with pytest.raises(ValueError):
        replace(start(), **changes)


@pytest.mark.parametrize(
    ("stage", "maximum", "gap"),
    [
        (WebRunStage.BOOTSTRAP_NAVIGATION, 3, "WEB_NAVIGATION_RETRY_EXHAUSTED"),
        (WebRunStage.PRIMARY_CAPTURE, 2, "WEB_PRIMARY_CAPTURE_FAILED"),
        (WebRunStage.AD_ISOLATED_NAVIGATION, 2, "WEB_AD_PAGE_OPEN_FAILED"),
        (WebRunStage.CLASSIFICATION, 1, "WEB_STAGE_LEASE_EXPIRED"),
    ],
)
def test_repeated_crash_reclaims_stop_before_authorizing_extra_attempt(stage, maximum, gap):
    cursor = start(stage)
    for attempt in range(maximum):
        cursor = cursor.claim(worker_id="worker", now=NOW + timedelta(seconds=61 * attempt))
        assert cursor.status is WebStageStatus.RUNNING
        assert cursor.attempt_count == attempt + 1
    identity = cursor.idempotency_key
    exhausted = cursor.claim(worker_id="recovery", now=NOW + timedelta(seconds=61 * maximum))
    assert exhausted.status is WebStageStatus.GAPPED
    assert exhausted.attempt_count == maximum
    assert exhausted.gap_codes == (gap,)
    assert exhausted.idempotency_key == identity
    assert exhausted.next_stage is not None


@pytest.mark.parametrize("field", ["cursor_payload", "last_error_detail"])
@pytest.mark.parametrize("key", ["auth", "sig", "access_key", "X-Amz-Signature", "a%75th", "token"])
def test_sensitive_query_aliases_are_rejected_consistently(field, key):
    with pytest.raises(ValueError):
        start(**{field: {"final_url": f"https://example.test/?{key}=private"}})


@pytest.mark.parametrize("field", ["cursor_payload", "last_error_detail"])
def test_normal_query_words_are_not_confused_with_credentials(field):
    url = "https://example.test/?author=1&signal=2&tokenizer=text&signature_count=3"
    cursor = start(**{field: {"final_url": url}})
    assert getattr(cursor, field)["final_url"] == url


@pytest.mark.parametrize(
    "changes",
    [
        {"stage": "bootstrap_navigation"},
        {"stage_generation": -1},
        {"stage_generation": True},
        {"attempt_count": 0},
        {"attempt_count": -1},
        {"attempt_count": True},
    ],
)
def test_gap_rejects_invalid_stage_generation_and_attempts(changes):
    values = dict(
        stage=WebRunStage.BOOTSTRAP_NAVIGATION,
        stage_generation=0,
        code="WEB_NAVIGATION_RETRY_EXHAUSTED",
        error_code="WEB_GOTO_TLS_FAILED",
        attempt_count=1,
    )
    with pytest.raises(ValueError):
        WebStageGap(**{**values, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"gaps": ()},
        {"attempt_count": 0},
        {"current_stage": WebRunStage.READINESS},
        {"stage_generation": 1},
        {"last_error_code": "WEB_OTHER"},
    ],
)
def test_gapped_cursor_requires_matching_stage_audit(changes):
    cursor = (
        start()
        .claim(worker_id="worker", now=NOW)
        .record_gap_and_advance(error_code="WEB_GOTO_TLS_FAILED")
    )
    with pytest.raises(ValueError):
        replace(cursor, **changes)
