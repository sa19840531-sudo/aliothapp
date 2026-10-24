"""弹层退场是局部动作证据，不要求或冒充业务页面跳转。"""

from collections.abc import Mapping
from typing import Any


def annotate_h5_obstruction_effect(
    entry: dict[str, Any], *, binding: Any, effect: str,
) -> None:
    """仅接受已验签目标、稳定关闭与原文档范围的合取证明。"""
    if binding is None or not binding.allow_dynamic_state_revalidation:
        return
    audit = entry.get("logical_target")
    diff = entry.get("state_diff")
    if not isinstance(audit, Mapping) or not isinstance(diff, Mapping):
        return
    scope = diff.get("h5_obstruction_document_scope")
    transition = diff.get("logical_surface_transition")
    if not (
        entry.get("dismissal_verified") is True
        and audit.get("target_trust") == "trusted"
        and audit.get("target_signature") == binding.target_signature
        and audit.get("source_state_fingerprint") == binding.source_state_fingerprint
        and isinstance(scope, Mapping)
        and scope.get("schema_version") == "h5-obstruction-document-scope.v1"
        and scope.get("same_document") is True
        and isinstance(transition, Mapping)
        and transition.get("before_identity_fingerprint") == binding.source_identity_fingerprint
        and entry.get("popup_dismissal_outcome") in {
            "original_layer_absent", "original_layer_hidden", "next_popup_layer_visible",
        }
    ):
        return
    entry["state_diff"] = {
        **dict(diff),
        "h5_obstruction_effect": {
            "schema_version": "h5-obstruction-effect.v1",
            "verified": True,
            "effect": effect,
            "source_identity_fingerprint": binding.source_identity_fingerprint,
            "source_state_fingerprint": binding.source_state_fingerprint,
            "target_signature": binding.target_signature,
        },
    }
