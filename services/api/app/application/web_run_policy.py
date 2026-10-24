"""Web 发布正文读取与冻结策略的可信运行时装配。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path

from services.api.app.agents.skills.loader import (
    WebExplorerWorkflowSkillConfigurationError,
    load_web_explorer_workflow_skill_content,
    validate_web_explorer_workflow_skill_content,
)
from services.api.app.application.web_readiness_contracts import (
    DEFAULT_WEB_READINESS_PROFILE,
    WebReadinessProfile,
)
from services.api.app.core.config import load_web_menu_collection_limit
from services.api.app.domain.exploration.web_discovery_policy import (
    WebDiscoveryPolicy,
    validate_discovery_required_types,
)
from services.api.app.domain.exploration.web_frontier_policy import WebFrontierPolicy
from services.api.app.domain.exploration.web_run_policy_snapshot import (
    WEB_POLICY_VERSIONS,
    WebFullPageSupplementalPolicy,
    WebRunPolicyError,
    WebRunPolicySnapshot,
)
from services.api.app.domain.exploration.web_runtime_contract import (
    CURRENT_WEB_RUNTIME_VERSION,
    WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED,
)
from services.api.app.domain.page.web_page_catalog import parse_web_page_type_catalog
from services.api.app.domain.page.web_page_knowledge import WebPageTypeCatalog
from services.api.app.domain.page.web_runtime_navigation_context import RuntimeWebNavigationContext
from services.api.app.domain.page.web_url_identity import WebUrlIdentityPolicy

_PAGE_CATALOG_PATH = Path(__file__).resolve().parents[1] / "domain/page/web_page_type_catalog.json"


@dataclass(frozen=True, kw_only=True)
class FrozenWebPolicy:
    snapshot: WebRunPolicySnapshot
    catalog: WebPageTypeCatalog
    readiness_profile: WebReadinessProfile
    workflow_skill: str
    deadline: datetime
    identity_policy: WebUrlIdentityPolicy
    discovery_policy: WebDiscoveryPolicy
    required_page_types: tuple[str, ...]
    full_page_supplemental_policy: WebFullPageSupplementalPolicy
    frontier_policy: WebFrontierPolicy
    menu_limit: int | None

    @property
    def assessment_schema_version(self) -> str:
        """v8 九类及 v9 十类目录使用 v4；历史五类与四类冻结协议保持不变。"""
        if {"activity", "search_page", "age_gate", "unknown"}.issubset(self.catalog.codes):
            return "web-page-type-assessment.v4"
        return (
            "web-page-type-assessment.v3"
            if "home" in self.catalog.codes
            else "web-page-type-assessment.v2"
        )


def build_new_web_navigation_context(
    *, target_url: str, policy: FrozenWebPolicy | None = None
) -> RuntimeWebNavigationContext:
    return RuntimeWebNavigationContext(
        target_origin=target_url,
        url_safety_policy=policy.identity_policy if policy else WebUrlIdentityPolicy(),
        discovery_policy=policy.discovery_policy if policy else WebDiscoveryPolicy.for_new_run(),
    )


def load_published_web_policy(
    *,
    now: datetime,
    max_duration_seconds: float,
    exploration_target: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """每个新 Run 读取当前发布源；不复用进程级默认缓存。"""
    catalog = json.loads(_PAGE_CATALOG_PATH.read_text(encoding="utf-8"))
    parse_web_page_type_catalog(catalog)
    menu_payload = {"schema_version": "web-runtime-navigation.v1"}
    discovery = {
        "version": "web-discovery.v2",
        **WebDiscoveryPolicy.for_new_run().to_config(),
        "closeout_reserve_seconds": 15,
        "required_page_types": list(
            validate_discovery_required_types(
                exploration_target.get("required_page_types", [])
                if exploration_target
                else []
            )
        ),
    }
    return {
        **WEB_POLICY_VERSIONS,
        "runtime_version": CURRENT_WEB_RUNTIME_VERSION,
        "guide_version": catalog["version"],
        "page_catalog": catalog,
        "menu_priors": menu_payload,
        "workflow_skill": load_web_explorer_workflow_skill_content(),
        "readiness_profile": asdict(DEFAULT_WEB_READINESS_PROFILE),
        "discovery_policy": discovery,
        "full_page_supplemental_policy": WebFullPageSupplementalPolicy(
            enabled=True
        ).to_payload(),
        "frontier_policy": WebFrontierPolicy.for_new_run(
            max_duration_seconds=max_duration_seconds
        ).to_payload(),
        "budget": {
            "menu_limit": load_web_menu_collection_limit(),
            "max_duration_seconds": max_duration_seconds,
            "started_at": now.isoformat(),
            "deadline": (now + timedelta(seconds=max_duration_seconds)).isoformat(),
        },
    }


def materialize_web_policy(
    snapshot: WebRunPolicySnapshot
) -> FrozenWebPolicy:
    """仅物化完整当前执行合同；历史兼容由只读审计适配器承担。"""
    body = snapshot.to_payload()
    if (
        body.get("runtime_version") != CURRENT_WEB_RUNTIME_VERSION
        or body["menu_priors"] != {"schema_version": "web-runtime-navigation.v1"}
        or body["discovery_policy"].get("enabled") is not True
    ):
        raise WebRunPolicyError(WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED)
    try:
        catalog = parse_web_page_type_catalog(body["page_catalog"])
        if catalog.version != body["guide_version"]:
            raise ValueError("guide_version 与正文不一致")
        identity_policy = WebUrlIdentityPolicy(version=body["identity_policy_version"])
        workflow = validate_web_explorer_workflow_skill_content(body["workflow_skill"])
        profile_values = dict(body["readiness_profile"])
        if set(profile_values) != {field.name for field in fields(WebReadinessProfile)}:
            raise ValueError("readiness_profile 必须包含完整冻结配置")
        for name in ("critical_resource_types", "critical_url_prefixes"):
            if not isinstance(profile_values[name], list):
                raise ValueError(f"{name} 必须为完整 JSON 数组")
            profile_values[name] = tuple(profile_values[name])
        profile = WebReadinessProfile(**profile_values)
        if profile.version != body["readiness_profile_version"]:
            raise ValueError("readiness_profile_version 与正文不一致")
        discovery = body["discovery_policy"]
        if (
            set(discovery)
            != {
                "version",
                "enabled",
                "max_unlisted_menus",
                "same_origin_only",
                "closeout_reserve_seconds",
                "required_page_types",
            }
            or discovery["version"] != "web-discovery.v2"
            or type(discovery["closeout_reserve_seconds"]) is not int
            or discovery["closeout_reserve_seconds"] != 15
        ):
            raise ValueError("discovery_policy 不受支持")
        discovery_policy = WebDiscoveryPolicy.from_config(
            {
                key: discovery[key]
                for key in ("enabled", "max_unlisted_menus", "same_origin_only")
            }
        )
        required_page_types = validate_discovery_required_types(
            discovery["required_page_types"]
        )
        frontier_payload = body.get("frontier_policy")
        if frontier_payload is None:
            raise ValueError("frontier_policy 缺失")
        frontier_policy = WebFrontierPolicy.from_payload(frontier_payload)
        supplemental_payload = body.get("full_page_supplemental_policy")
        if supplemental_payload is None:
            raise ValueError("full_page_supplemental_policy 缺失")
        elif not isinstance(supplemental_payload, Mapping):
            raise ValueError("full_page_supplemental_policy 必须为对象")
        else:
            supplemental_policy = WebFullPageSupplementalPolicy.from_payload(
                supplemental_payload
            )
        budget = body["budget"]
        started = datetime.fromisoformat(budget["started_at"])
        deadline = datetime.fromisoformat(budget["deadline"])
        duration = budget["max_duration_seconds"]
        if (
            started.tzinfo is None
            or deadline.tzinfo is None
            or type(budget["menu_limit"]) is not int
            or not 1 <= budget["menu_limit"] <= 1000
            or type(duration) not in (int, float)
            or duration <= 0
            or (deadline - started).total_seconds() != duration
        ):
            raise ValueError("冻结预算或 deadline 无效")
    except (KeyError, TypeError, ValueError, WebExplorerWorkflowSkillConfigurationError) as exc:
        raise WebRunPolicyError(f"WEB_POLICY_BODY_INVALID: {exc}") from exc
    return FrozenWebPolicy(
        snapshot=snapshot,
        catalog=catalog,
        readiness_profile=profile,
        workflow_skill=workflow,
        deadline=deadline,
        identity_policy=identity_policy,
        discovery_policy=discovery_policy,
        required_page_types=required_page_types,
        full_page_supplemental_policy=supplemental_policy,
        frontier_policy=frontier_policy,
        menu_limit=budget["menu_limit"],
    )
