"""Web 就绪判定领域合同测试。"""

from __future__ import annotations

import unittest

from services.api.app.application.web_readiness_contracts import (
    WebEvidenceRole,
    WebNetworkCapability,
    WebReadinessSignals,
    WebReadinessStatus,
    assess_web_readiness,
    read_web_readiness_assessment,
)


def _stable_signals(**overrides: object) -> WebReadinessSignals:
    values: dict[str, object] = {
        "identity_before": "https://example.test/list",
        "identity_after": "https://example.test/list",
        "artifacts_same_epoch": True,
        "capture_failed": False,
        "timed_out": False,
        "core_present": True,
        "visible_loader": False,
        "transition_active": False,
        "necessary_media_ready": True,
        "dom_quiet_ms": 1_200.0,
        "minimum_dom_quiet_ms": 800.0,
        "network_capability": WebNetworkCapability.OBSERVABLE,
        "network_required": False,
        "critical_network_pending": 0,
        "settle_complete": True,
        "quorum_met": True,
        "capture_consistent": True,
        "profile_version": "web-structure.v1",
    }
    values.update(overrides)
    return WebReadinessSignals(**values)  # type: ignore[arg-type]


class WebReadinessContractsTest(unittest.TestCase):
    def test_stable_capture_can_serve_classification(self) -> None:
        result = assess_web_readiness(
            _stable_signals(),
            role=WebEvidenceRole.CLASSIFICATION,
        )

        self.assertEqual(result.status, WebReadinessStatus.STABLE)
        self.assertTrue(result.can_serve_evidence)
        self.assertEqual(result.blocking_reasons, ())

    def test_fixed_quiet_time_does_not_override_visible_loader(self) -> None:
        result = assess_web_readiness(
            _stable_signals(visible_loader=True, dom_quiet_ms=30_000.0),
            role=WebEvidenceRole.INSPECTION,
        )

        self.assertEqual(result.status, WebReadinessStatus.LOADING)
        self.assertFalse(result.can_serve_evidence)
        self.assertIn("visible_loader", result.blocking_reasons)

    def test_identity_change_is_reported_as_drift(self) -> None:
        result = assess_web_readiness(
            _stable_signals(identity_after="https://example.test/detail/1"),
            role=WebEvidenceRole.INTERACTION_AFTER,
        )

        self.assertEqual(result.status, WebReadinessStatus.IDENTITY_DRIFT)
        self.assertEqual(result.blocking_reasons, ("identity_changed_during_capture",))

    def test_cross_epoch_artifacts_are_rejected(self) -> None:
        result = assess_web_readiness(
            _stable_signals(artifacts_same_epoch=False),
            role=WebEvidenceRole.REPORT_PREVIEW,
        )

        self.assertEqual(result.status, WebReadinessStatus.ARTIFACT_SKEW)
        self.assertEqual(result.blocking_reasons, ("artifacts_cross_epoch",))

    def test_unknown_network_is_not_treated_as_zero_when_required(self) -> None:
        result = assess_web_readiness(
            _stable_signals(
                network_capability=WebNetworkCapability.UNKNOWN,
                network_required=True,
                critical_network_pending=None,
            ),
            role=WebEvidenceRole.INSPECTION,
        )

        self.assertEqual(result.status, WebReadinessStatus.CAPTURE_FAILED)
        self.assertEqual(result.blocking_reasons, ("required_network_unobservable",))

    def test_background_requests_do_not_block_when_network_is_not_required(self) -> None:
        result = assess_web_readiness(
            _stable_signals(
                network_required=False,
                critical_network_pending=6,
            ),
            role=WebEvidenceRole.CLASSIFICATION,
        )

        self.assertEqual(result.status, WebReadinessStatus.STABLE)
        self.assertTrue(result.can_serve_evidence)
        self.assertEqual(result.blocking_reasons, ())

    def test_pending_critical_request_blocks_when_network_is_required(self) -> None:
        result = assess_web_readiness(
            _stable_signals(
                network_required=True,
                critical_network_pending=1,
            ),
            role=WebEvidenceRole.INSPECTION,
        )

        self.assertEqual(result.status, WebReadinessStatus.LOADING)
        self.assertFalse(result.can_serve_evidence)
        self.assertEqual(result.blocking_reasons, ("critical_network_pending",))

    def test_active_transition_has_distinct_terminal_status(self) -> None:
        result = assess_web_readiness(
            _stable_signals(transition_active=True),
            role=WebEvidenceRole.INTERACTION_AFTER,
        )

        self.assertEqual(result.status, WebReadinessStatus.TRANSITIONAL)
        self.assertIn("transition_active", result.blocking_reasons)

    def test_timeout_is_not_reported_as_loading(self) -> None:
        result = assess_web_readiness(
            _stable_signals(timed_out=True, core_present=False),
            role=WebEvidenceRole.CLASSIFICATION,
        )

        self.assertEqual(result.status, WebReadinessStatus.TIMEOUT)
        self.assertIn("readiness_deadline_exceeded", result.blocking_reasons)

    def test_invalid_persisted_metadata_fails_closed_without_raising(self) -> None:
        result = read_web_readiness_assessment(
            {
                "web_readiness": {
                    "role": "classification",
                    "network_capability": "observable",
                }
            }
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, WebReadinessStatus.CAPTURE_FAILED)
        self.assertEqual(result.blocking_reasons, ("invalid_web_readiness_metadata",))


if __name__ == "__main__":
    unittest.main()
