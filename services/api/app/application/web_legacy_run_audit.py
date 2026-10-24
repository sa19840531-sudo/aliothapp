"""历史 Web 冻结策略只读展示，不产生可执行运行时对象。"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Literal

from services.api.app.agents.skills.loader import validate_web_explorer_workflow_skill_content
from services.api.app.application.web_readiness_contracts import WebReadinessProfile
from services.api.app.domain.exploration.web_run_policy_snapshot import (
    WebRunPolicyError,
    WebRunPolicySnapshot,
    _freeze,
    mutable_json,
)
from services.api.app.domain.page.web_page_catalog import parse_web_page_type_catalog
from services.api.app.domain.page.web_page_knowledge import WebPageTypeCatalog
from services.api.app.domain.page.web_site_menu_registry import WebSiteMenuRegistry
from services.api.app.domain.page.web_url_identity import WebUrlIdentityPolicy


@dataclass(frozen=True, kw_only=True)
class LegacyWebAuditView:
    menu_registry_version: str | None
    direct_ad_page_enabled: bool | None
    frozen_fields: Mapping[str, object]
    readiness_profile: WebReadinessProfile
    catalog: WebPageTypeCatalog
    identity_policy: WebUrlIdentityPolicy
    execution_allowed: Literal[False] = False

    @property
    def assessment_schema_version(self) -> str:
        if {"activity", "search_page", "age_gate", "unknown"}.issubset(self.catalog.codes):
            return "web-page-type-assessment.v4"
        return (
            "web-page-type-assessment.v3"
            if "home" in self.catalog.codes
            else "web-page-type-assessment.v2"
        )


class LegacyWebRunAuditAdapter:
    @staticmethod
    def load(snapshot: WebRunPolicySnapshot | Mapping[str, object]) -> LegacyWebAuditView:
        """深层冻结历史字段；不向调用方暴露 Registry 导航方法。"""
        try:
            return LegacyWebRunAuditAdapter._load(snapshot)
        except (KeyError, TypeError, ValueError) as exc:
            raise WebRunPolicyError(f"WEB_POLICY_BODY_INVALID: {exc}") from exc

    @staticmethod
    def _load(snapshot: WebRunPolicySnapshot | Mapping[str, object]) -> LegacyWebAuditView:
        body = (
            snapshot.to_payload()
            if isinstance(snapshot, WebRunPolicySnapshot)
            else mutable_json(snapshot)
        )
        catalog = parse_web_page_type_catalog(body["page_catalog"], legacy_frozen=True)
        if catalog.version != body["guide_version"]:
            raise ValueError("guide_version 与正文不一致")
        validate_web_explorer_workflow_skill_content(body["workflow_skill"])
        profile = dict(body["readiness_profile"])
        if set(profile) != {field.name for field in fields(WebReadinessProfile)}:
            raise ValueError("readiness_profile 必须包含完整冻结配置")
        for name in ("critical_resource_types", "critical_url_prefixes"):
            if not isinstance(profile[name], list):
                raise ValueError(f"{name} 必须为完整 JSON 数组")
            profile[name] = tuple(profile[name])
        readiness = WebReadinessProfile(**profile)
        if readiness.version != body["readiness_profile_version"]:
            raise ValueError("readiness_profile_version 与正文不一致")
        menu = body.get("menu_priors", {})
        version = None
        if menu.get("schema_version") == "web-menu-allowlist.v4":
            registry = WebSiteMenuRegistry(payload=menu, legacy_frozen="home" not in catalog.codes)
            version = registry.schema_version
        elif menu != {"schema_version": "web-runtime-navigation.v1"}:
            raise ValueError("历史菜单协议不受支持")
        discovery = body.get("discovery_policy", {})
        flag = body.get("direct_ad_page_enabled", discovery.get("direct_ad_page_enabled"))
        return LegacyWebAuditView(
            menu_registry_version=version,
            direct_ad_page_enabled=flag if type(flag) is bool else None,
            frozen_fields=_freeze(body),
            readiness_profile=readiness,
            catalog=catalog,
            identity_policy=WebUrlIdentityPolicy(version=body["identity_policy_version"]),
        )
