"""H5 质检通知模式快照的领域合同。"""

import pytest

from services.api.app.domain.task.h5_inspection_notification_mode import (
    H5InspectionNotificationMode,
    lock_h5_inspection_notification_mode,
    parse_h5_inspection_notification_mode,
)


def test_historical_task_defaults_notification_mode_to_off() -> None:
    assert parse_h5_inspection_notification_mode({}) is H5InspectionNotificationMode.OFF


@pytest.mark.parametrize("raw", ["off", "shadow", "enforced"])
def test_notification_mode_accepts_only_frozen_values(raw: str) -> None:
    assert (
        parse_h5_inspection_notification_mode({"h5_inspection_notification_mode": raw}).value == raw
    )


def test_notification_mode_rejects_invalid_persisted_value() -> None:
    with pytest.raises(ValueError, match="h5_inspection_notification_mode"):
        parse_h5_inspection_notification_mode({"h5_inspection_notification_mode": "on"})


def test_notification_mode_lock_is_idempotent_and_immutable() -> None:
    locked = lock_h5_inspection_notification_mode(
        {"workflow_version": "v2"},
        H5InspectionNotificationMode.ENFORCED,
    )
    assert locked["h5_inspection_notification_mode"] == "enforced"
    assert (
        lock_h5_inspection_notification_mode(
            locked,
            H5InspectionNotificationMode.ENFORCED,
        )
        == locked
    )

    with pytest.raises(ValueError, match="不可改写"):
        lock_h5_inspection_notification_mode(
            locked,
            H5InspectionNotificationMode.SHADOW,
        )
