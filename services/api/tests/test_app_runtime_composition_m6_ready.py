"""P1-09：生产 M6 authority 可使补采 runtime_ready（默认仍关）。"""

from services.api.app.application.app_bounded_exploration_milestones import (
    PRODUCTION_AUTHORITY_ENV_KEYS,
    resolve_production_m6_supplemental_ready,
)
from services.api.app.application.supplemental_evidence_policy import (
    resolve_supplemental_evidence_policy,
)


def test_production_m6_ready_default_false() -> None:
    assert resolve_production_m6_supplemental_ready(environ={}, explicit=None) is False
    policy = resolve_supplemental_evidence_policy(enabled=True, lab_runtime_ready=False)
    assert policy.runtime_ready is False


def test_production_m6_authority_enables_runtime_ready_without_lab() -> None:
    env = {
        PRODUCTION_AUTHORITY_ENV_KEYS["M4"]: "true",
        PRODUCTION_AUTHORITY_ENV_KEYS["M5"]: "true",
        PRODUCTION_AUTHORITY_ENV_KEYS["M6"]: "true",
    }
    assert resolve_production_m6_supplemental_ready(environ=env, explicit=None) is True
    # 组合根 OR 语义：把 production M6 ready 当作 lab_runtime_ready 注入
    policy = resolve_supplemental_evidence_policy(enabled=True, lab_runtime_ready=True)
    assert policy.enabled is True
    assert policy.runtime_ready is True
