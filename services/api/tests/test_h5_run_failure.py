"""H5 完全技术失败与真实部分成果的共享判读。"""

import pytest

from services.api.app.application.h5_run_failure import classify_h5_run_failure

CONTEXT = {"task_id": "task-1", "run_id": "run-1", "target_url": "https://dys18.com/"}


def test_outside_capture_and_only_placeholders_are_completely_failed() -> None:
    facts = classify_h5_run_failure(
        CONTEXT,
        [
            {
                "page_capture_id": "outside",
                "url": "https://example.com/",
                "detection_status": "recognized",
            },
        ],
        [
            {"agent_run_id": "run-1", "execution_status": "skipped", "is_bootstrap": True},
            {"agent_run_id": "run-1", "execution_status": "evidence_insufficient"},
            {
                "agent_run_id": "run-1",
                "execution_status": "skipped",
                "execution_reason": "auth_requirement_not_met",
            },
            {"agent_run_id": "run-2", "execution_status": "pass"},
        ],
    )
    assert facts.completely_failed is True
    assert facts.valid_target_capture_count == facts.business_result_count == 0
    assert facts.technical_failure_reason == "COMMAND_HOST_BOUNDARY_VIOLATION"


@pytest.mark.parametrize("status", ["pass", "fail", "warning", "skipped", "manual_review"])
def test_business_results_from_current_capture_run_preserve_partial_results(status: str) -> None:
    facts = classify_h5_run_failure(
        CONTEXT,
        [],
        [
            {
                "agent_run_id": "judge-run",
                "source_agent_run_id": "run-1",
                "execution_status": status,
            }
        ],
        actions=[{"error_code": "BROWSER_CONNECTION_LOST"}],
    )
    assert facts.business_result_count == 1
    assert facts.has_partial_results is True
    assert facts.completely_failed is False


def test_valid_target_page_without_rules_is_partial_and_diagnostics_are_not() -> None:
    pages = [
        {
            "page_capture_id": "target",
            "url": "https://DYS18.com/home",
            "detection_status": "recognized",
        }
    ]
    facts = classify_h5_run_failure(CONTEXT, pages, [], actions=[{"error_code": "BROWSER_LOST"}])
    assert facts.valid_target_capture_count == 1
    assert facts.completely_failed is False
    pages[0]["metadata"] = {
        "diagnostic_capture": True,
        "navigation_scope": {"verified": False, "error_code": "COMMAND_HOST_BOUNDARY_VIOLATION"},
    }
    facts = classify_h5_run_failure(CONTEXT, pages, [])
    assert facts.completely_failed is True


def test_empty_business_scope_without_technical_failure_does_not_become_failed() -> None:
    facts = classify_h5_run_failure(CONTEXT, [], [])
    assert facts.completely_failed is False


def test_platform_manual_review_and_missing_run_identity_are_not_business_results() -> None:
    facts = classify_h5_run_failure(
        CONTEXT,
        [],
        [
            {
                "agent_run_id": "run-1",
                "execution_status": "manual_review",
                "execution_reason": "semantic_provider_http_500",
            },
            {"execution_status": "pass"},
        ],
    )
    assert facts.business_result_count == 0


@pytest.mark.parametrize("location", ["top", "pipeline_metrics", "metrics"])
@pytest.mark.parametrize(
    "verified,final_url,valid",
    [
        (True, "https://login.allowed.test/home", True),
        (False, "https://login.allowed.test/home", False),
        (True, "https://another.test/", False),
    ],
)
def test_trusted_post_navigation_scope_recognizes_only_same_verified_arrival(
    location: str,
    verified: bool,
    final_url: str,
    valid: bool,
) -> None:
    scope = {"verified": verified, "final_url": final_url}
    metadata = (
        {"navigation_scope": scope}
        if location == "top"
        else {location: {"navigation_scope": scope}}
    )
    facts = classify_h5_run_failure(
        CONTEXT,
        [
            {
                "page_capture_id": "allowed-secondary",
                "agent_run_id": "run-1",
                "url": "https://login.allowed.test/home",
                "detection_status": "recognized",
                "metadata": metadata,
            }
        ],
        [],
    )
    assert facts.valid_target_capture_count == int(valid)
    assert facts.completely_failed is not valid


def test_pipeline_diagnostic_capture_on_target_is_not_business_evidence() -> None:
    facts = classify_h5_run_failure(
        CONTEXT,
        [
            {
                "page_capture_id": "diagnostic",
                "url": "https://dys18.com/",
                "detection_status": "recognized",
                "metadata": {
                    "pipeline_metrics": {
                        "diagnostic_capture": True,
                        "navigation_scope": {"verified": False},
                    }
                },
            }
        ],
        [],
    )
    assert facts.valid_target_capture_count == 0
    assert facts.completely_failed is True


@pytest.mark.parametrize("outside", [False, True])
def test_results_from_known_invalid_document_do_not_resurrect_failed_run(outside):
    facts = classify_h5_run_failure(
        CONTEXT,
        [{
            "page_capture_id": "invalid", "url": "https://example.com/" if outside
            else "https://dys18.com/", "detection_status": "recognized",
            "metadata": {"pipeline_metrics": {"navigation_document": {
                "inspectable": False, "error_code": "WEB_NAVIGATION_BLANK_DOCUMENT",
            }}},
        }],
        [{"page_capture_id": "invalid", "source_agent_run_id": "run-1",
          "execution_status": "pass"}],
    )
    assert facts.valid_target_capture_count == facts.business_result_count == 0
    assert facts.completely_failed is True
