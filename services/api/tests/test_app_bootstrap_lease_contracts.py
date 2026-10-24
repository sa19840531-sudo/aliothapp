import pytest

from services.api.app.application.app_bootstrap_state import (
    RenewAppBootstrapLeaseCommand,
)
from services.api.app.domain.interaction.surface_driver import app_primary_surface_ref


def test_renew_command_rejects_non_positive_seconds() -> None:
    with pytest.raises(ValueError, match="lease_seconds"):
        RenewAppBootstrapLeaseCommand(
            task_id="task-1",
            session_id="session-1",
            platform_build_ref="app-build:1",
            lease_owner="worker-a",
            lease_token="00000000-0000-0000-0000-000000000001",
            lease_seconds=0,
        )


def test_app_primary_surface_ref_is_stable_and_non_secret() -> None:
    value = app_primary_surface_ref("session-1")

    assert value == "app-surface:session-1"
    assert "token" not in value and "password" not in value
