import pytest

from services.api.app.domain import interaction as interaction_domain


def test_install_request_builds_stable_checksum_scoped_idempotency_key() -> None:
    request = interaction_domain.AppInstallRequest(
        task_id="task-1",
        session_id="session-1",
        platform_build_ref="app-build:01JABC",
        sha256="a" * 64,
        package_name="com.example.target",
    )

    assert request.idempotency_key == f"session-1:{'a' * 64}"
    assert request.to_dict() == {
        "protocol_version": "app-bootstrap.v1",
        "task_id": "task-1",
        "session_id": "session-1",
        "platform_build_ref": "app-build:01JABC",
        "sha256": "a" * 64,
        "package_name": "com.example.target",
        "replace_policy": "same_checksum_only",
        "idempotency_key": f"session-1:{'a' * 64}",
    }


def test_install_request_rejects_non_sha256_digest() -> None:
    with pytest.raises(interaction_domain.InteractionValidationError, match="sha256"):
        interaction_domain.AppInstallRequest(
            task_id="task-1",
            session_id="session-1",
            platform_build_ref="app-build:01JABC",
            sha256="not-a-checksum",
        )


def test_open_request_idempotency_distinguishes_cold_start_policy() -> None:
    cold = interaction_domain.AppOpenRequest(
        task_id="task-1",
        session_id="session-1",
        package_name="com.example.target",
        cold_start=True,
    )
    warm = interaction_domain.AppOpenRequest(
        task_id="task-1",
        session_id="session-1",
        package_name="com.example.target",
        cold_start=False,
    )

    assert cold.idempotency_key == "session-1:com.example.target:cold"
    assert warm.idempotency_key == "session-1:com.example.target:warm"


def test_bootstrap_result_does_not_allow_success_with_error_code() -> None:
    with pytest.raises(interaction_domain.InteractionValidationError, match="error_code"):
        interaction_domain.AppOpenResult(
            request_id="request-1",
            session_id="session-1",
            succeeded=True,
            retryable=False,
            package_name="com.example.target",
            foreground_verified=True,
            error_code="OPEN_FAILED",
        )


def test_capture_ref_round_trip_uses_common_capture_identity() -> None:
    reference = interaction_domain.CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        surface_kind=interaction_domain.SurfaceKind.ANDROID,
        capture_kind=interaction_domain.CaptureKind.APP,
        role="bootstrap",
        artifact_ids=("artifact-1",),
    )

    payload = reference.to_dict()

    assert payload["version"] == "capture-ref.v1"
    assert payload["capture_id"] == "capture-1"
    assert "page_capture_id" not in payload
    assert interaction_domain.CaptureRef.from_dict(payload) == reference
