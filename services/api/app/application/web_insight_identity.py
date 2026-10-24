"""既有 Web 工作台与报告共用身份读取；未知来源保持待复核。"""

from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicyError


async def load_web_insight_identities(repository, *, task_id, agent_run_id):
    reader = getattr(repository, "read_web_capture_identities", None)
    if not callable(reader) or not agent_run_id:
        return {}, "WEB_LOGICAL_ID_UNTRUSTED"
    try:
        identities = await reader(task_id=task_id, agent_run_id=agent_run_id)
    except WebRunPolicyError as exc:
        return {}, str(exc).split(":", 1)[0]
    failures = sorted(
        {
            str(item.get("logical_page_identity_status"))
            for item in identities.values()
            if item.get("logical_page_identity_status")
            not in {"verified", "WEB_LOGICAL_ID_UNTRUSTED"}
        }
    )
    if not any(
        item.get("logical_page_identity_status") == "verified" for item in identities.values()
    ):
        return identities, failures[0] if failures else "WEB_LOGICAL_ID_UNTRUSTED"
    return identities, failures[0] if failures else None
