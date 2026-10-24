"""Web V2 当前载荷技术需求；与真实业务规则身份及旧 v1 序列化分离。"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from services.api.app.domain.exploration.evidence_exit_requirements import (
    ActiveSessionEvidenceRequirement,
    EvidenceExitRequirementSet,
    EvidenceExitRequirementStatus,
    build_evidence_exit_requirement_id,
)
from services.api.app.domain.protocol import (
    parse_aware_datetime,
    require_aware_datetime,
    stable_protocol_hash,
)

WEB_PAYLOAD_PROTOCOL = "web-payload-requirement-set.v1"
_BINDING_KEYS = frozenset(
    {"task_id", "agent_run_id", "session_id", "finalization_epoch", "capture_id", "policy_hash"}
)


@dataclass(frozen=True, kw_only=True)
class WebPayloadRequirement:
    """不含 rule_id 的技术成员；只允许 pending 或由明确 Manifest 满足。"""

    requirement_id: str
    required_artifact_types: tuple[str, ...]
    required_capture_roles: tuple[str, ...]
    status: EvidenceExitRequirementStatus = EvidenceExitRequirementStatus.PENDING
    resolution_manifest_id: str | None = None

    def __post_init__(self):
        if not self.requirement_id.startswith("web-current-payload:"):
            raise ValueError("WEB_PAYLOAD_ID_INVALID")
        if self.required_artifact_types not in (
            ("dom", "screenshot"),
            ("dom", "screenshot", "visible_text"),
        ):
            raise ValueError("WEB_PAYLOAD_BASELINE_REQUIRED")
        if len(self.required_capture_roles) != 1 or not self.required_capture_roles[0]:
            raise ValueError("WEB_PAYLOAD_ROLE_INVALID")
        if self.status not in (
            EvidenceExitRequirementStatus.PENDING,
            EvidenceExitRequirementStatus.SATISFIED,
        ):
            raise ValueError("WEB_PAYLOAD_STATUS_INVALID")
        if (self.status is EvidenceExitRequirementStatus.SATISFIED) != bool(
            self.resolution_manifest_id
        ):
            raise ValueError("WEB_PAYLOAD_RESOLUTION_INVALID")

    def to_dict(self):
        return {
            "scope": "current_payload",
            "requirement_id": self.requirement_id,
            "required_artifact_types": list(self.required_artifact_types),
            "required_capture_roles": list(self.required_capture_roles),
            "status": self.status.value,
            "resolution_manifest_id": self.resolution_manifest_id,
        }

    @classmethod
    def from_dict(cls, body):
        if (
            set(body)
            != {
                "scope",
                "requirement_id",
                "required_artifact_types",
                "required_capture_roles",
                "status",
                "resolution_manifest_id",
            }
            or body["scope"] != "current_payload"
        ):
            raise ValueError("WEB_PAYLOAD_FIELDS_INVALID")
        return cls(
            requirement_id=body["requirement_id"],
            required_artifact_types=tuple(body["required_artifact_types"]),
            required_capture_roles=tuple(body["required_capture_roles"]),
            status=EvidenceExitRequirementStatus(body["status"]),
            resolution_manifest_id=body["resolution_manifest_id"],
        )


@dataclass(frozen=True, kw_only=True)
class WebPayloadRequirementSet(EvidenceExitRequirementSet):
    """新 Web 协议显式携带可认证绑定；基类旧序列化完全不变。"""

    binding: Mapping
    protocol_version: str = WEB_PAYLOAD_PROTOCOL

    def __post_init__(self):
        if self.protocol_version != WEB_PAYLOAD_PROTOCOL or set(self.binding) != _BINDING_KEYS:
            raise ValueError("WEB_PAYLOAD_BINDING_INVALID")
        binding = dict(self.binding)
        if any(
            not isinstance(binding[key], str) or not binding[key]
            for key in _BINDING_KEYS - {"finalization_epoch"}
        ):
            raise ValueError("WEB_PAYLOAD_BINDING_INVALID")
        epoch = binding["finalization_epoch"]
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("WEB_PAYLOAD_EPOCH_INVALID")
        if len(binding["policy_hash"]) != 64 or any(
            c not in "0123456789abcdef" for c in binding["policy_hash"]
        ):
            raise ValueError("WEB_PAYLOAD_POLICY_INVALID")
        if any(
            binding[key] != getattr(self, key) for key in ("task_id", "session_id", "capture_id")
        ):
            raise ValueError("WEB_PAYLOAD_OWNERSHIP_INVALID")
        if (
            isinstance(self.revision, bool)
            or self.revision < 1
            or not 1 <= len(self.requirements) <= 1001
        ):
            raise ValueError("WEB_PAYLOAD_REVISION_INVALID")
        technical = [item for item in self.requirements if isinstance(item, WebPayloadRequirement)]
        if len(technical) != 1:
            raise ValueError("WEB_PAYLOAD_TECHNICAL_REQUIRED")
        item = technical[0]
        if item.requirement_id != _technical_id(
            binding, item.required_artifact_types, item.required_capture_roles
        ):
            raise ValueError("WEB_PAYLOAD_IDENTITY_MISMATCH")
        for member in self.requirements:
            if isinstance(member, WebPayloadRequirement):
                continue
            if not isinstance(
                member, ActiveSessionEvidenceRequirement
            ) or member.requirement_id != build_evidence_exit_requirement_id(
                task_id=self.task_id,
                capture_id=self.capture_id,
                rule_id=member.rule_id,
                required_artifact_types=member.required_artifact_types,
                required_capture_roles=member.required_capture_roles,
            ):
                raise ValueError("WEB_PAYLOAD_BUSINESS_IDENTITY_INVALID")
        if len({item.requirement_id for item in self.requirements}) != len(self.requirements):
            raise ValueError("WEB_PAYLOAD_DUPLICATE_MEMBER")
        require_aware_datetime(self.created_at, "created_at")
        if self.requirement_set_id != _set_id(
            self.revision, binding, self.matcher_snapshot_hash, self.requirements
        ):
            raise ValueError("WEB_PAYLOAD_SET_HASH_INVALID")
        from types import MappingProxyType

        object.__setattr__(self, "binding", MappingProxyType(binding))

    def to_dict(self):
        return {**super().to_dict(), "binding": dict(self.binding)}

    @classmethod
    def from_dict(cls, body):
        if set(body) != {
            "protocol_version",
            "requirement_set_id",
            "revision",
            "task_id",
            "session_id",
            "capture_id",
            "matcher_snapshot_hash",
            "requirements",
            "created_at",
            "binding",
        }:
            raise ValueError("WEB_PAYLOAD_SET_FIELDS_INVALID")
        return cls(
            **{key: body[key] for key in body if key not in {"requirements", "created_at"}},
            requirements=tuple(
                WebPayloadRequirement.from_dict(item)
                if item.get("scope") == "current_payload"
                else ActiveSessionEvidenceRequirement.from_dict(item)
                for item in body["requirements"]
            ),
            created_at=parse_aware_datetime(body["created_at"], "created_at"),
        )


def _technical_id(binding, artifacts, roles):
    return "web-current-payload:" + stable_protocol_hash(
        {"binding": dict(binding), "artifacts": list(artifacts), "roles": list(roles)}
    )


def _set_id(revision, binding, matcher, members):
    return "web-exit-set:" + stable_protocol_hash(
        {
            "revision": revision,
            "binding": dict(binding),
            "matcher": matcher,
            "requirements": [item.to_dict() for item in members],
        }
    )


def rebuild_payload_requirement_set(
    previous, *, requirements, matcher_snapshot_hash=None, created_at
):
    members = tuple(requirements)
    matcher = matcher_snapshot_hash or previous.matcher_snapshot_hash
    revision = previous.revision + 1
    return replace(
        previous,
        revision=revision,
        requirements=members,
        matcher_snapshot_hash=matcher,
        created_at=created_at,
        requirement_set_id=_set_id(revision, previous.binding, matcher, members),
    )


def create_payload_requirement_set(
    *,
    task_id,
    agent_run_id,
    session_id,
    finalization_epoch,
    capture_id,
    policy_hash,
    capture_role,
    committed_artifact_types,
    created_at,
):
    binding = dict(
        task_id=task_id,
        agent_run_id=agent_run_id,
        session_id=session_id,
        finalization_epoch=finalization_epoch,
        capture_id=capture_id,
        policy_hash=policy_hash,
    )
    artifacts = tuple(
        sorted(
            {"screenshot", "dom"}
            | ({"visible_text"} if "visible_text" in committed_artifact_types else set())
        )
    )
    roles = (capture_role,)
    members = (
        WebPayloadRequirement(
            requirement_id=_technical_id(binding, artifacts, roles),
            required_artifact_types=artifacts,
            required_capture_roles=roles,
        ),
    )
    matcher = stable_protocol_hash(
        {
            "phase": "capture_preservation",
            "binding": binding,
            "business_requirements": "not_evaluated",
        }
    )
    return WebPayloadRequirementSet(
        requirement_set_id=_set_id(1, binding, matcher, members),
        revision=1,
        task_id=task_id,
        session_id=session_id,
        capture_id=capture_id,
        matcher_snapshot_hash=matcher,
        requirements=members,
        binding=binding,
        created_at=created_at,
    )
